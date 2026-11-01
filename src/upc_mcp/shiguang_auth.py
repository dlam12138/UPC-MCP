"""CAS SSO recovery for Shiguang using only saved CAS cookies.

The isolated jar starts with CAS cookies and a fresh anonymous dekt SESSION; it
never reuses the authenticated dekt SESSION and never posts a password.
Only the observed HTTPS CAS login, the registered service, the CAS callback and
the observed second-stage dekt login are followed. HTTP 500 alone never triggers
a refresh.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx

from .shiguang import (
    LIST_PATH,
    ShiguangClient,
    ShiguangError,
    parse_activity_list,
)
from .shiguang_session import (
    CAS_DOMAIN,
    DEKT_DOMAIN,
    ShiguangSession,
    cookies_from_jar,
    identity_hash_from_html,
    jar_from_cookies,
    now,
)

CAS_LOGIN = "https://cas.upc.edu.cn/cas/login?service=http%3A%2F%2Fdekt.upc.edu.cn%2Fcaslogin.jsp"
DEKT_ENTRY = "http://dekt.upc.edu.cn/caslogin.jsp"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_AUTH_BYTES = 1_000_000
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
    ),
}
ALLOWED_HOSTS = {CAS_DOMAIN, DEKT_DOMAIN}
SECOND_STAGE_PATH = "/sysmng/login!loginByUserNoForCas.action"


def _allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.hostname not in ALLOWED_HOSTS:
        return False
    if parts.port not in (None, 80, 443):
        return False
    if parts.hostname == CAS_DOMAIN and parts.scheme != "https":
        return False
    if parts.hostname == DEKT_DOMAIN and parts.scheme not in {"http", "https"}:
        return False
    return True


class ShiguangAuth:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self.transport = transport
        self.timeout = timeout or HTTP_TIMEOUT

    def _client(self, jar: httpx.Cookies) -> httpx.Client:
        kwargs = {
            "cookies": jar,
            "follow_redirects": False,
            "timeout": self.timeout,
            "trust_env": False,
            "headers": HEADERS,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)

    def recover(self, session: ShiguangSession) -> ShiguangSession:
        if not session.has_cas_sso():
            raise ShiguangError(
                "auth_required",
                "没有可用的 CAS SSO cookie，无法后台恢复石光会话。",
            )
        jar = jar_from_cookies(session.cookies, {CAS_DOMAIN})
        with self._client(jar) as client:
            self._bootstrap_anonymous(client)
            callback = self._cas_roundtrip(client)
            if callback.status_code == 200:
                self._second_stage(client, callback.text)
            # The callback may be 500 while the dekt session was still established;
            # only a successful business list distinguishes the two.
            html = ShiguangClient(client, timeout=self.timeout)._get(
                LIST_PATH, {"page": "1", "oper": "1"}
            )
            parse_activity_list(html, 1)
            recovered = cookies_from_jar(client.cookies)
            if not any(c.name == "SESSION" and c.domain == DEKT_DOMAIN for c in recovered):
                raise ShiguangError("auth_required", "SSO 未建立石光 SESSION。")
            identity = identity_hash_from_html(html)
            identity_verified = False
            if identity and session.identity_hash and session.identity_verified:
                if identity != session.identity_hash:
                    raise ShiguangError(
                        "auth_required",
                        "恢复页面的身份标识与已有绑定不一致，已拒绝写回。",
                    )
                identity_verified = True
            return ShiguangSession(
                cookies=recovered,
                binding_id=session.effective_binding_id(),
                state="stored",
                verified_at=now(),
                identity_hash=identity,
                identity_verified=identity_verified,
                source="cas_sso_recovery",
            )

    def _bootstrap_anonymous(self, client: httpx.Client) -> None:
        self._get(client, DEKT_ENTRY)

    def _cas_roundtrip(self, client: httpx.Client) -> httpx.Response:
        url = CAS_LOGIN
        for _ in range(7):
            response = self._get(client, url)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                nxt = urljoin(url, location)
                if not _allowed(nxt):
                    raise ShiguangError("upstream_error", "CAS 跳转到了非预期主机。")
                if urlsplit(nxt).hostname == DEKT_DOMAIN and urlsplit(nxt).path == "/caslogin.jsp":
                    return self._get(client, nxt)
                url = nxt
                continue
            if response.status_code == 200:
                if urlsplit(url).hostname == CAS_DOMAIN:
                    raise ShiguangError("auth_required", "CAS SSO 已失效，需要一次正常登录。")
                return response
            if response.status_code == 500 and urlsplit(url).hostname == DEKT_DOMAIN:
                return response
            raise ShiguangError("upstream_error", f"CAS 往返返回 HTTP {response.status_code}。")
        raise ShiguangError("upstream_error", "CAS 跳转次数超过限制。")

    def _second_stage(self, client: httpx.Client, html: str) -> None:
        match = re.search(r"var\s+pcRedirectUrl\s*=\s*[\"']([^\"']+)[\"']", html)
        if not match:
            return
        target = urljoin("http://dekt.upc.edu.cn/", match.group(1).replace("&amp;", "&"))
        parts = urlsplit(target)
        if (
            parts.hostname != DEKT_DOMAIN
            or parts.port not in (None, 80, 443)
            or parts.path != SECOND_STAGE_PATH
        ):
            return
        for _ in range(3):
            response = self._get(client, target)
            if response.status_code in (301, 302, 303, 307, 308):
                nxt = urljoin(target, response.headers.get("location", ""))
                if urlsplit(nxt).hostname != DEKT_DOMAIN:
                    return
                target = nxt
                continue
            return

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        try:
            with client.stream("GET", url, timeout=self.timeout) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    return httpx.Response(
                        response.status_code,
                        headers=self._body_headers(response.headers),
                    )
                if response.status_code != 200:
                    return httpx.Response(
                        response.status_code,
                        headers=self._body_headers(response.headers),
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_AUTH_BYTES:
                    raise ShiguangError("upstream_error", "CAS/石光响应超过本地大小限制。")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_AUTH_BYTES:
                        raise ShiguangError("upstream_error", "CAS/石光响应超过本地大小限制。")
                    content.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=self._body_headers(response.headers),
                    content=bytes(content),
                )
        except ShiguangError:
            raise
        except httpx.TimeoutException:
            raise ShiguangError("network_error", "CAS/石光请求超时。") from None
        except httpx.HTTPError:
            raise ShiguangError("network_error", "CAS/石光网络请求失败。") from None

    @staticmethod
    def _body_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
        # iter_bytes() already decoded the body; forwarding these headers would
        # make httpx try to decode it a second time.
        drop = {"content-encoding", "content-length", "transfer-encoding"}
        return [(key, value) for key, value in headers.multi_items() if key.lower() not in drop]
