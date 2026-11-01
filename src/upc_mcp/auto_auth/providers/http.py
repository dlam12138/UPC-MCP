"""Fixed-host HTTP helpers for the three official service callbacks."""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlsplit

import httpx

from ..cas import bounded_request, cookies_from_client
from ..errors import AutoAuthError

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)
MAX_SERVICE_BYTES = 2 * 1024 * 1024
CALLBACK_PATHS = {
    "dekt.upc.edu.cn": {
        "/caslogin.jsp", "/sysmng/login!loginByUserNoForCas.action", "/web/index.html",
    },
    "app.upc.edu.cn": {"/a_upc/api/cas/login", "/site/weekschedule/index"},
    "libspace.upc.edu.cn": {
        "/remote/static/casAuth/getServiceByVerifyTicket/casLogin", "/libseat/",
    },
    "service.upc.edu.cn": {"/site/login/cas-login", "/v2/site/index"},
}


def protocol_errors(function):
    """Prevent validation errors from exposing raw upstream cookie/token input."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except AutoAuthError:
            raise
        except (ValueError, TypeError, KeyError):
            raise AutoAuthError(
                "service_protocol_error", "业务认证响应结构不符合已验证契约。"
            ) from None
    return wrapped


def client(
    cookies: list[dict] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    from ..cas import jar_from_cookies

    kwargs = {
        "cookies": jar_from_cookies(cookies) if cookies else httpx.Cookies(),
        "follow_redirects": False,
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "trust_env": False,
        "headers": {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def check_location(url: str, host: str, *, allow_http: bool = False) -> str:
    parts = urlsplit(url)
    if (
        parts.hostname != host
        or parts.port not in (None, 443 if parts.scheme == "https" else 80)
        or parts.username
        or parts.password
        or (parts.scheme != "https" and not (allow_http and parts.scheme == "http"))
        or parts.path not in CALLBACK_PATHS.get(host, set())
    ):
        raise AutoAuthError("service_unexpected_redirect", "业务服务返回了非预期跳转。")
    return url


def request(http: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    return bounded_request(
        http, method, url, limit=MAX_SERVICE_BYTES, error_prefix="service", **kwargs
    )


def follow(
    http: httpx.Client,
    url: str,
    host: str,
    *,
    max_hops: int = 8,
    allow_http: bool = False,
    allow_5xx: bool = False,
) -> tuple[httpx.Response, list[str]]:
    visited: list[str] = []
    for _ in range(max_hops):
        check_location(url, host, allow_http=allow_http)
        visited.append(url)
        try:
            response = request(http, "GET", url)
        except httpx.TimeoutException:
            raise AutoAuthError(
                "service_timeout", "业务服务请求超时。", details={"retryable": True}
            ) from None
        except httpx.HTTPError:
            raise AutoAuthError(
                "service_network_error",
                "业务服务网络请求失败。",
                details={"retryable": True},
            ) from None
        if response.status_code in {301, 302, 303, 307, 308}:
            location = str(response.headers.get("location", ""))
            if not location:
                return response, visited
            url = str(httpx.URL(url).join(location))
            continue
        if response.status_code in {401, 403}:
            raise AutoAuthError("service_auth_failed", "业务服务拒绝了新会话。")
        if response.status_code >= 500:
            if allow_5xx:
                return response, visited
            raise AutoAuthError(
                "service_upstream_error", f"业务服务返回 HTTP {response.status_code}。"
            )
        return response, visited
    raise AutoAuthError("service_redirect_limit", "业务服务跳转次数超过限制。")


def service_cookies(http: httpx.Client, domain: str) -> list[dict]:
    return [c for c in cookies_from_client(http) if c["domain"] == domain]
