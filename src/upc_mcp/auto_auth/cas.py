"""Password CAS login, SSO probe, tickets and the local CAS checkpoint.

Only ``https://cas.upc.edu.cn`` ever receives the password. A ticket is
single-use, memory-only during a service callback and never logged. The CAS
checkpoint is DPAPI-protected and bound to the credential generation/account.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from .credentials import Binding, Credential, _protect, _restrict, _unprotect
from .errors import AutoAuthError
from .locks import FileLock

CAS_HOST = "cas.upc.edu.cn"
CAS_LOGIN = "https://cas.upc.edu.cn/cas/login"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)
MAX_CAS_BYTES = 1_000_000
MAX_CAS_COOKIES = 256
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_CHECKPOINT_META_BYTES = 8 * 1024


def _validate_cas_cookies(cookies: object) -> list[dict]:
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= MAX_CAS_COOKIES:
        raise ValueError
    for item in cookies:
        if not isinstance(item, dict):
            raise ValueError
        for name, maximum in (("name", 128), ("value", 8192), ("path", 1024)):
            value = item.get(name)
            if not isinstance(value, str) or not 1 <= len(value) <= maximum:
                raise ValueError
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError
        if (
            not isinstance(item.get("domain"), str)
            or item["domain"].lstrip(".") != CAS_HOST
        ):
            raise ValueError
        if not item["path"].startswith("/"):
            raise ValueError
        if any(
            key in item and not isinstance(item[key], bool)
            for key in ("secure", "http_only", "host_only")
        ):
            raise ValueError
        expires = item.get("expires")
        if expires is not None and (type(expires) is not int or expires <= 0):
            raise ValueError
    return cookies

SERVICES = {
    "shiguang": "http://dekt.upc.edu.cn/caslogin.jsp",
    "timetable": (
        "https://app.upc.edu.cn/a_upc/api/cas/login"
        "?redirect=https%3A%2F%2Fapp.upc.edu.cn%2Fsite%2Fweekschedule%2Findex"
    ),
    "library": (
        "https://libspace.upc.edu.cn/remote/static/casAuth/"
        "getServiceByVerifyTicket/casLogin"
    ),
    "service_portal": (
        "https://service.upc.edu.cn/site/login/cas-login"
        "?redirect_url=https%3A%2F%2Fservice.upc.edu.cn%2Fv2%2Fsite%2Findex"
    ),
}
SERVICE_RULES = {
    "shiguang": {"host": "dekt.upc.edu.cn", "scheme": "http", "path": "/caslogin.jsp"},
    "timetable": {
        "host": "app.upc.edu.cn",
        "scheme": "https",
        "path": "/a_upc/api/cas/login",
    },
    "library": {
        "host": "libspace.upc.edu.cn",
        "scheme": "https",
        "path": "/remote/static/casAuth/getServiceByVerifyTicket/casLogin",
    },
    "service_portal": {
        "host": "service.upc.edu.cn",
        "scheme": "https",
        "path": "/site/login/cas-login",
    },
}
SERVICE_PORTAL_REDIRECT = "https://service.upc.edu.cn/v2/site/index"
LIBRARY_ENTRY = (
    "https://libspace.upc.edu.cn/remote/static/sso/login"
    "?redirectUrl=https%3A%2F%2Flibspace.upc.edu.cn%2Flibseat%2F%23%2Flogin"
)


@dataclass
class CasForm:
    action: str
    fields: dict[str, str] = field(repr=False)
    captcha_required: bool
    error_text: str | None = field(repr=False)
    has_password: bool
    has_sms_or_qr: bool

    def public(self) -> dict:
        return {
            "action_host": urlsplit(self.action).hostname,
            "field_names": sorted(self.fields),
            "execution_len": len(self.fields.get("execution", "")),
            "captcha_required": self.captcha_required,
            "error_present": bool(self.error_text),
            "error_sha256_12": hashlib.sha256((self.error_text or "").encode()).hexdigest()[:12]
            if self.error_text
            else None,
            "has_password": self.has_password,
            "has_sms_or_qr": self.has_sms_or_qr,
        }


@dataclass
class CasTicket:
    system: str
    url: str = field(repr=False)
    ticket_len: int
    source: str = "sso"
    issued_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        parts = urlsplit(self.url)
        return {
            "system": self.system,
            "host": parts.hostname,
            "path": parts.path,
            "ticket_present": "ticket" in parse_qs(parts.query),
            "ticket_len": self.ticket_len,
            "source": self.source,
        }


@dataclass
class CasRoundTrip:
    ticket: CasTicket
    cookies: list[dict] = field(repr=False)
    elapsed_ms: int


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._current: dict | None = None
        self._in_error = False
        self._error_depth = 0
        self._error_parts: list[str] = []
        self._captcha_visible = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._current = {
                "action": attrs.get("action"),
                "fields": {},
                "has_password": False,
            }
            self.forms.append(self._current)
        elif tag in {"input", "button"} and self._current is not None:
            name = attrs.get("name")
            if name:
                self._current["fields"].setdefault(name, attrs.get("value") or "")
                if (attrs.get("type") or "").lower() == "password":
                    self._current["has_password"] = True
        elif tag == "div" and (attrs.get("id") or "") == "errorDiv":
            self._in_error = True
            self._error_depth = 1
        elif tag == "div" and self._in_error:
            self._error_depth += 1
        elif tag == "div" and (attrs.get("id") or "") == "captchaParent":
            style = (attrs.get("style") or "").replace(" ", "").lower()
            self._captcha_visible = "display:block" in style

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None
        if tag == "div" and self._in_error:
            self._error_depth -= 1
            if self._error_depth <= 0:
                self._in_error = False

    def handle_data(self, data):
        if self._in_error:
            self._error_parts.append(data)


def parse_login_form(page: str, page_url: str) -> CasForm:
    parser = _FormParser()
    parser.feed(page)
    form = next(
        (item for item in parser.forms if item["has_password"]),
        parser.forms[0] if parser.forms else None,
    )
    if form is None:
        return CasForm(
            action=page_url,
            fields={},
            captcha_required=False,
            error_text=" ".join(parser._error_parts).strip() or None,
            has_password=False,
            has_sms_or_qr=(
                "username_smstoken" in page or "扫码登录" in page or "验证码登录" in page
            ),
        )
    fields = dict(form["fields"])
    captcha_required = bool(
        parser._captcha_visible
        or re.search(r"config\.captcha\s*=", page)
        or "captcha" in fields
    )
    return CasForm(
        action=form.get("action") or page_url,
        fields=fields,
        captcha_required=captcha_required,
        error_text=" ".join(parser._error_parts).strip() or None,
        has_password=bool(form["has_password"]),
        has_sms_or_qr=(
            "username_smstoken" in page or "扫码登录" in page or "验证码登录" in page
        ),
    )


def classify_login_page(form: CasForm) -> str:
    text = (form.error_text or "").strip()
    if any(token in text for token in ("用户名或密码错误", "认证信息无效", "密码错误")):
        return "cas_invalid_credentials"
    if "验证码" in text or form.captcha_required:
        return "cas_captcha_required"
    if not form.has_password:
        if form.has_sms_or_qr:
            return "cas_second_factor_required"
        return "cas_login_page_unrecognized"
    if text:
        return "cas_login_rejected"
    return "cas_login_page"


def cookies_from_client(client: httpx.Client) -> list[dict]:
    result: list[dict] = []
    for raw in client.cookies.jar:
        if not raw.name or not raw.value:
            continue
        result.append(
            {
                "name": raw.name,
                "value": raw.value,
                "domain": (raw.domain or "").lstrip("."),
                "path": raw.path or "/",
                "secure": bool(raw.secure),
                "http_only": bool(
                    raw.has_nonstandard_attr("HttpOnly")
                    or raw.has_nonstandard_attr("httponly")
                ),
                "expires": raw.expires if raw.expires and raw.expires > 0 else None,
                "host_only": not bool(raw.domain_specified),
            }
        )
        if len(result) > MAX_CAS_COOKIES:
            raise AutoAuthError("cas_cookie_limit", "CAS 返回的 Cookie 数量超过本地限制。")
    return result


def jar_from_cookies(
    cookies: list[dict], *, domains: set[str] | None = None
) -> httpx.Cookies:
    jar = CookieJar()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "").lstrip(".")
        if domains is not None and domain not in domains:
            continue
        value = c.get("value")
        name = c.get("name")
        if not isinstance(name, str) or not name or not isinstance(value, str) or not value:
            continue
        try:
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=str(c.get("domain") or domain),
                    domain_specified=not bool(c.get("host_only", False)),
                    domain_initial_dot=str(c.get("domain") or "").startswith("."),
                    path=c.get("path") or "/",
                    path_specified=True,
                    secure=bool(c.get("secure")),
                    expires=c.get("expires") if isinstance(c.get("expires"), int) else None,
                    discard=bool(c.get("expires") is None),
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": None} if c.get("http_only") else {},
                    rfc2109=False,
                )
            )
        except (ValueError, TypeError):
            continue
    out = httpx.Cookies()
    out.jar = jar
    return out


def _ticket_from_location(location: str, system: str, source: str) -> CasTicket:
    rule = SERVICE_RULES[system]
    parts = urlsplit(location)
    if (
        parts.hostname != rule["host"]
        or parts.scheme != rule["scheme"]
        or parts.path != rule["path"]
        or parts.port not in (None, 443 if parts.scheme == "https" else 80)
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise AutoAuthError(
            "cas_unexpected_redirect",
            "CAS 返回了非预期的业务回调地址；未跟随。",
            action="auto_auth_unlock",
        )
    query = parse_qs(parts.query, keep_blank_values=True)
    allowed = {"ticket", "redirect", "from"} if system == "timetable" else {"ticket"}
    if system == "service_portal":
        allowed = {"ticket", "redirect_url"}
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise AutoAuthError("cas_unexpected_redirect", "CAS 回调参数不在允许范围。")
    if system == "timetable" and (
        query.get("redirect") not in (None, ["https://app.upc.edu.cn/site/weekschedule/index"])
        or query.get("from") not in (None, ["wap"])
    ):
        raise AutoAuthError("cas_unexpected_redirect", "课表回调重定向目标不在允许范围。")
    if system == "service_portal" and query.get("redirect_url") != [
        SERVICE_PORTAL_REDIRECT
    ]:
        raise AutoAuthError("cas_unexpected_redirect", "e站通回调重定向目标不在允许范围。")
    ticket = query.get("ticket", [""])[0]
    if not ticket.startswith("ST-") or not 8 <= len(ticket) <= 4096:
        raise AutoAuthError("cas_ticket_missing", "CAS 响应没有可用的单次票据。")
    return CasTicket(
        system=system, url=location, ticket_len=len(ticket), source=source
    )


def validate_cas_url(url: str, system: str) -> str:
    """Check the exact CAS origin/path and registered per-system service."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https" or parts.hostname != CAS_HOST
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.path != "/cas/login" or parts.fragment
        ):
            raise ValueError
        query = parse_qs(parts.query, keep_blank_values=True)
        if (
            set(query) - {"service", "from", "noAutoRedirect"}
            or any(len(values) != 1 for values in query.values())
            or query.get("from") not in (None, ["wap"])
            or query.get("noAutoRedirect") not in (None, ["1"])
        ):
            raise ValueError
        service = urlsplit(query.get("service", [""])[0])
        rule = SERVICE_RULES[system]
        if (
            service.scheme != rule["scheme"] or service.hostname != rule["host"]
            or service.path != rule["path"] or service.username or service.password
            or service.port not in (None, 443 if service.scheme == "https" else 80)
            or service.fragment
        ):
            raise ValueError
        target = parse_qs(service.query, keep_blank_values=True)
        if system == "timetable":
            if target != {"redirect": ["https://app.upc.edu.cn/site/weekschedule/index"]}:
                raise ValueError
        elif system == "service_portal":
            if target != {"redirect_url": [SERVICE_PORTAL_REDIRECT]}:
                raise ValueError
        elif target:
            raise ValueError
    except (ValueError, KeyError):
        raise AutoAuthError(
            "cas_unexpected_redirect", "CAS 地址或系统 service 不在允许范围。"
        ) from None
    return url


def bounded_request(
    http: httpx.Client, method: str, url: str, *, limit: int = MAX_CAS_BYTES,
    error_prefix: str = "cas", **kwargs,
) -> httpx.Response:
    """Read at most the configured decompressed size, preserving cookie handling."""
    with http.stream(method, url, **kwargs) as response:
        content = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > limit:
                raise AutoAuthError(
                    f"{error_prefix}_response_too_large", "认证响应超过本地大小限制。"
                )
            content.extend(chunk)
        headers = response.headers.copy()
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            headers.pop(name, None)
        return httpx.Response(
            response.status_code, headers=headers, content=bytes(content), request=response.request
        )


def prepare_library_entry(
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, list[dict]]:
    """Bootstrap the library JSESSIONID and return its exact CAS URL."""
    kwargs = {
        "follow_redirects": False,
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "trust_env": False,
        "headers": {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    }
    if transport is not None:
        kwargs["transport"] = transport
    with httpx.Client(**kwargs) as client:
        try:
            response = bounded_request(client, "GET", LIBRARY_ENTRY, error_prefix="service")
        except httpx.TimeoutException:
            raise AutoAuthError(
                "service_timeout", "图书馆 SSO 入口请求超时。", details={"retryable": True}
            ) from None
        except httpx.HTTPError:
            raise AutoAuthError(
                "service_network_error",
                "图书馆 SSO 入口网络请求失败。",
                details={"retryable": True},
            ) from None
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise AutoAuthError("service_unexpected_redirect", "图书馆 SSO 入口未返回跳转。")
        location = str(response.headers.get("location", ""))
        parts = urlsplit(location)
        if (
            parts.scheme != "https"
            or parts.hostname != CAS_HOST
            or parts.path != "/cas/login"
        ):
            raise AutoAuthError(
                "service_unexpected_redirect", "图书馆 SSO 入口未指向官方 CAS。"
            )
        validate_cas_url(location, "library")
        return location, cookies_from_client(client)


class CasAuth:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self.transport = transport
        self.timeout = timeout or httpx.Timeout(20.0, connect=10.0)

    def _client(self, cookies: httpx.Cookies | None = None) -> httpx.Client:
        kwargs = {
            "cookies": cookies if cookies is not None else httpx.Cookies(),
            "follow_redirects": False,
            "timeout": self.timeout,
            "trust_env": False,
            "headers": {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)

    @staticmethod
    def login_url(system: str) -> str:
        service = SERVICES[system]
        if system == "timetable":
            # Exact deployed shape: service is not encoded and from=wap /
            # noAutoRedirect are outer CAS query parameters. Encoding from=wap
            # into service makes the app reject the ticket.
            return f"{CAS_LOGIN}?service={service}&from=wap&noAutoRedirect=1"
        return CAS_LOGIN + "?" + urlencode({"service": service}, quote_via=quote)

    def get_ticket_at(
        self,
        cas_url: str,
        system: str,
        cas_cookies: list[dict],
        *,
        source: str = "sso",
    ) -> CasTicket | None:
        parts = urlsplit(cas_url)
        if (
            parts.scheme != "https"
            or parts.hostname != CAS_HOST
            or parts.port not in (None, 443)
            or parts.username
            or parts.password
            or parts.path != "/cas/login"
        ):
            raise AutoAuthError("cas_unexpected_redirect", "CAS 入口不是官方 HTTPS 地址。")
        validate_cas_url(cas_url, system)
        jar = jar_from_cookies(cas_cookies, domains={CAS_HOST})
        with self._client(jar) as client:
            try:
                response = bounded_request(client, "GET", cas_url)
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "cas_network_error", "CAS 请求超时。", details={"retryable": True}
                ) from None
            except httpx.ConnectError as exc:
                text = str(exc).lower()
                if "ssl" in text or "certificate" in text or "tls" in text:
                    raise AutoAuthError("cas_tls_error", "CAS TLS 连接失败；未降级。") from None
                raise AutoAuthError(
                    "cas_network_error", "无法连接 CAS。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "cas_network_error", "CAS 网络请求失败。", details={"retryable": True}
                ) from None
            if response.status_code in {301, 302, 303, 307, 308}:
                return _ticket_from_location(
                    str(response.headers.get("location", "")), system, source
                )
            if response.status_code == 200:
                form = parse_login_form(response.text, str(response.url))
                if form.has_password and not form.error_text:
                    return None
                raise AutoAuthError(
                    classify_login_page(form),
                    "CAS 未直接签发业务票据。",
                    details={"login_page": form.public()},
                )
            if response.status_code in {401, 403, 429}:
                raise AutoAuthError("cas_access_denied", "CAS 拒绝了当前请求。")
            raise AutoAuthError(
                "cas_upstream_error",
                f"CAS 返回 HTTP {response.status_code}。",
                details={"retryable": response.status_code >= 500},
            )

    def probe(self, system: str, cas_cookies: list[dict]) -> CasTicket | None:
        return self.get_ticket_at(self.login_url(system), system, cas_cookies)

    def password_login_at(
        self,
        cas_url: str,
        system: str,
        credential: Credential,
        *,
        source: str = "password",
    ) -> CasRoundTrip:
        parts = urlsplit(cas_url)
        if (
            parts.scheme != "https"
            or parts.hostname != CAS_HOST
            or parts.port not in (None, 443)
            or parts.username
            or parts.password
            or parts.path != "/cas/login"
        ):
            raise AutoAuthError("cas_unexpected_redirect", "CAS 入口不是官方 HTTPS 地址。")
        validate_cas_url(cas_url, system)
        started = time.monotonic()
        with self._client() as client:
            try:
                page = bounded_request(client, "GET", cas_url)
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "cas_network_error", "CAS 登录页请求超时。", details={"retryable": True}
                ) from None
            except httpx.ConnectError as exc:
                text = str(exc).lower()
                if "ssl" in text or "certificate" in text or "tls" in text:
                    raise AutoAuthError("cas_tls_error", "CAS TLS 连接失败；未降级。") from None
                raise AutoAuthError(
                    "cas_network_error", "无法连接 CAS。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "cas_network_error", "CAS 网络请求失败。", details={"retryable": True}
                ) from None
            if page.status_code in {301, 302, 303, 307, 308}:
                raise AutoAuthError("cas_unexpected_redirect", "CAS 登录页返回了非预期跳转。")
            if page.status_code != 200:
                raise AutoAuthError(
                    "cas_upstream_error",
                    f"CAS 登录页返回 HTTP {page.status_code}。",
                    details={"retryable": page.status_code >= 500},
                )
            form = parse_login_form(page.text, str(page.url))
            if form.captcha_required:
                raise AutoAuthError(
                    "cas_captcha_required",
                    "CAS 当前要求验证码；未提交密码，请先完成官方验证。",
                    action="auto_auth_unlock",
                    details={"login_page": form.public()},
                )
            if not form.has_password or "execution" not in form.fields:
                code = classify_login_page(form)
                raise AutoAuthError(
                    code,
                    "CAS 登录页不包含可自动提交的账号密码表单。",
                    action="auto_auth_unlock" if code == "cas_second_factor_required" else None,
                    details={"login_page": form.public()},
                )
            form.action = str(httpx.URL(str(page.url)).join(form.action))
            action = urlsplit(form.action)
            if (
                action.scheme != "https"
                or action.hostname != CAS_HOST
                or action.port not in (None, 443)
                or action.username
                or action.password
                or action.path != "/cas/login"
                or action.fragment
            ):
                raise AutoAuthError(
                    "cas_unexpected_form_action",
                    "CAS 登录表单提交地址不是验证 TLS 的官方 CAS HTTPS 地址。",
                )
            validate_cas_url(form.action, system)
            data = {
                "username": credential.username,
                "password": credential.password,
                "type": form.fields.get("type") or "username_password",
                "execution": form.fields["execution"],
                "_eventId": form.fields.get("_eventId") or "submit",
                "submit": form.fields.get("submit") or "登录",
            }
            try:
                response = bounded_request(
                    client, "POST", form.action,
                    data=data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://cas.upc.edu.cn",
                        "Referer": str(page.url),
                    },
                )
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "cas_submit_timeout", "CAS 凭据提交超时，结果未知；本轮不重试。"
                ) from None
            except httpx.ConnectError as exc:
                text = str(exc).lower()
                if "ssl" in text or "certificate" in text or "tls" in text:
                    raise AutoAuthError("cas_tls_error", "CAS TLS 连接失败；未降级。") from None
                raise AutoAuthError(
                    "cas_network_error",
                    "CAS 凭据提交网络失败。",
                    details={"retryable": True},
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "cas_network_error",
                    "CAS 凭据提交网络失败。",
                    details={"retryable": True},
                ) from None
            elapsed = int((time.monotonic() - started) * 1000)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = str(response.headers.get("location", ""))
                if urlsplit(location).hostname == CAS_HOST:
                    with self._client() as follow:
                        try:
                            error_url = str(httpx.URL(str(page.url)).join(location))
                            validate_cas_url(error_url, system)
                            error_page = bounded_request(follow, "GET", error_url)
                        except httpx.HTTPError:
                            raise AutoAuthError(
                                "cas_upstream_error", "CAS 登录结果无法确认。"
                            ) from None
                    form = parse_login_form(error_page.text, str(error_page.url))
                    raise AutoAuthError(
                        classify_login_page(form),
                        "CAS 未签发业务票据。",
                        details={"login_page": form.public()},
                    )
                return CasRoundTrip(
                    ticket=_ticket_from_location(location, system, source),
                    cookies=cookies_from_client(client),
                    elapsed_ms=elapsed,
                )
            if response.status_code == 200:
                form = parse_login_form(response.text, str(response.url))
                raise AutoAuthError(
                    classify_login_page(form),
                    "CAS 未签发业务票据。",
                    details={"login_page": form.public()},
                )
            if response.status_code in {401, 403, 429}:
                raise AutoAuthError(
                    "cas_access_denied",
                    f"CAS 拒绝了登录请求（HTTP {response.status_code}）。",
                )
            raise AutoAuthError(
                "cas_upstream_error",
                f"CAS 登录返回 HTTP {response.status_code}。",
                details={"retryable": response.status_code >= 500},
            )

    def password_login(
        self, system: str, credential: Credential
    ) -> CasRoundTrip:
        return self.password_login_at(self.login_url(system), system, credential)


class CasSessionStore:
    """DPAPI CAS checkpoint bound to one credential generation/account."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "cas-session.dpapi"
        self.meta_path = directory / "cas-session-meta.json"
        self.lock_path = directory / "cas-session.lock"

    def _read_meta(self) -> dict | None:
        if not self.meta_path.exists():
            return None
        try:
            if self.meta_path.stat().st_size > MAX_CHECKPOINT_META_BYTES:
                raise ValueError
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError
            generation = data.get("generation")
            account = data.get("account_hash")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError
            if not isinstance(account, str) or len(account) != 64:
                raise ValueError
            return data
        except (OSError, ValueError, TypeError):
            raise AutoAuthError(
                "cas_checkpoint_corrupt",
                "本机 CAS 检查点元数据损坏；未尝试登录，请重新配置或采用。",
                action="credential_clear_cas",
            ) from None

    def load(self, binding: Binding) -> list[dict] | None:
        with FileLock(self.lock_path, timeout=5.0).held():
            meta = self._read_meta()
            has_file = self.path.is_file()
            if meta is None and not has_file:
                return None
            if meta is None or not has_file:
                raise AutoAuthError(
                    "cas_checkpoint_corrupt",
                    "本机 CAS 检查点不完整；未尝试登录。",
                    action="credential_clear_cas",
                )
            if (
                meta["generation"] != binding.generation
                or meta["account_hash"] != binding.account_hash
            ):
                # Stale checkpoint from another generation/account is never
                # reused; it is not corruption and does not authorize a login.
                return None
            try:
                if self.path.stat().st_size > MAX_CHECKPOINT_BYTES:
                    raise ValueError
                payload = json.loads(_unprotect(self.path.read_bytes()))
                if not isinstance(payload, dict) or payload.get("version") != 1:
                    raise ValueError
                if (
                    payload.get("credential_generation") != binding.generation
                    or payload.get("account_hash") != binding.account_hash
                ):
                    raise ValueError
                cookies = _validate_cas_cookies(payload.get("cookies"))
                return cookies
            except AutoAuthError:
                raise
            except (OSError, ValueError, TypeError):
                raise AutoAuthError(
                    "cas_checkpoint_corrupt",
                    "本机 CAS 检查点无法读取；未尝试登录。",
                    action="credential_clear_cas",
                ) from None

    def save(
        self, cookies: list[dict], binding: Binding, *, require_absent: bool = False
    ) -> None:
        if not binding.configured or not binding.account_hash:
            raise AutoAuthError("credential_missing", "没有可用于保存 CAS 检查点的账户绑定。")
        try:
            _validate_cas_cookies(cookies)
        except (ValueError, TypeError, AttributeError):
            raise AutoAuthError(
                "cas_checkpoint_corrupt", "CAS Cookie 数据格式无效；未保存。"
            ) from None
        payload = json.dumps({
            "version": 1, "cookies": cookies, "account_hash": binding.account_hash,
            "credential_generation": binding.generation,
        }, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_CHECKPOINT_BYTES:
            raise AutoAuthError("cas_checkpoint_corrupt", "CAS 检查点超过本地大小限制。")
        encrypted = _protect(payload)
        if len(encrypted) > MAX_CHECKPOINT_BYTES:
            raise AutoAuthError("cas_checkpoint_corrupt", "CAS 检查点超过本地大小限制。")
        with FileLock(self.lock_path, timeout=5.0).held():
            if require_absent and (self.path.exists() or self.meta_path.exists()):
                raise AutoAuthError("already_configured", "已有 CAS 检查点；未覆盖。")
            _restrict(self.directory, directory=True)
            fd, name = tempfile.mkstemp(prefix=".cas-session-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encrypted)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
            finally:
                Path(name).unlink(missing_ok=True)
            _restrict(self.path)
            meta = {
                "version": 1,
                "generation": binding.generation,
                "account_hash": binding.account_hash,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            fd, name = tempfile.mkstemp(prefix=".cas-session-meta-", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(meta, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.meta_path)
            finally:
                Path(name).unlink(missing_ok=True)
            _restrict(self.meta_path)

    def clear(self) -> None:
        with FileLock(self.lock_path, timeout=5.0).held():
            self.path.unlink(missing_ok=True)
            self.meta_path.unlink(missing_ok=True)

    def adopt_from(self, source: Path, binding: Binding) -> None:
        if self.path.exists() or self.meta_path.exists():
            raise AutoAuthError(
                "already_configured",
                "生产状态目录已有 CAS 检查点；未覆盖，请先显式清除。",
                action="credential_clear_cas",
            )
        source = source.resolve()
        try:
            valid = (
                source.is_file()
                and source.suffix.lower() == ".dpapi"
                and source.stat().st_size <= MAX_CHECKPOINT_BYTES
            )
        except OSError:
            valid = False
        if not valid:
            raise AutoAuthError("invalid_input", "请选择本机 DPAPI 研究 CAS 检查点。")
        try:
            payload = json.loads(_unprotect(source.read_bytes()))
            if not isinstance(payload, dict) or payload.get("version") not in (None, 1):
                raise ValueError
            cookies = payload.get("cookies")
            if not isinstance(cookies, list) or not cookies:
                raise ValueError
        except AutoAuthError:
            raise
        except (OSError, ValueError, TypeError):
            raise AutoAuthError("invalid_input", "研究 CAS 检查点格式无效。") from None
        source_account = payload.get("account_hash")
        if source_account != binding.account_hash:
            raise AutoAuthError(
                "candidate_mismatch",
                "研究 CAS 检查点属于另一账户；未采用。",
            )
        self.save(cookies, binding, require_absent=True)
