"""e站通 provider: CAS ticket -> exact service callback -> session verification."""

from __future__ import annotations

import httpx

from ...service_portal_protocol import (
    COUNT_PATH,
    SERVICE_HOST,
    ServicePortalError,
    parse_envelope,
)
from ...service_portal_store import (
    ServicePortalCookie,
    ServicePortalSession,
    now,
)
from ..cas import CasTicket
from ..credentials import Binding
from ..errors import AutoAuthError
from .base import PreparedService
from .http import client, follow, protocol_errors, request

SERVICE_BASE = "https://service.upc.edu.cn"


class ServicePortalProvider:
    system = "service_portal"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def prepare(self) -> PreparedService:
        return PreparedService()

    @protocol_errors
    def establish(
        self, ticket: CasTicket, binding: Binding, prepared: PreparedService
    ) -> ServicePortalSession:
        source = "auto_auth_password" if ticket.source == "password" else "auto_auth_sso"
        with client(transport=self.transport) as http:
            response, _visited = follow(http, ticket.url, SERVICE_HOST, max_hops=4)
            if response.status_code != 200:
                raise AutoAuthError(
                    "service_auth_failed", "e站通回调没有建立可用会话。"
                )
            try:
                verify = request(
                    http,
                    "GET",
                    SERVICE_BASE + COUNT_PATH,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": SERVICE_BASE + "/v2/site/index",
                    },
                )
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "service_timeout", "e站通会话校验超时。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "service_network_error",
                    "e站通会话校验网络失败。",
                    details={"retryable": True},
                ) from None
            if verify.status_code in {301, 302, 303, 401, 403}:
                raise AutoAuthError("service_auth_failed", "e站通校验仍要求登录。")
            if verify.status_code != 200:
                raise AutoAuthError(
                    "service_upstream_error",
                    f"e站通会话校验返回 HTTP {verify.status_code}。",
                )
            try:
                parse_envelope(verify.content)
            except ServicePortalError as exc:
                if exc.code in {"auth_required", "auth_expired"}:
                    raise AutoAuthError(
                        "service_auth_failed", "e站通会话校验未通过。"
                    ) from None
                if exc.code == "user_action_required":
                    raise AutoAuthError(
                        "service_second_factor_required",
                        "e站通要求二次验证。",
                        action="complete_second_factor",
                        details={"kind": "second_factor"},
                    ) from None
                raise AutoAuthError(
                    "service_protocol_error", "e站通会话校验响应不符合契约。"
                ) from None
            cookies: list[ServicePortalCookie] = []
            for item in http.cookies.jar:
                domain = (item.domain or "").lstrip(".").lower()
                if domain != SERVICE_HOST or not item.name or not item.value:
                    continue
                try:
                    cookies.append(
                        ServicePortalCookie(
                            name=item.name,
                            value=item.value,
                            domain=SERVICE_HOST,
                            path=item.path or "/",
                            secure=bool(item.secure),
                            http_only=bool(
                                item.has_nonstandard_attr("HttpOnly")
                                or item.has_nonstandard_attr("httponly")
                            ),
                            host_only=not item.domain_specified,
                            expires_at=float(item.expires)
                            if item.expires and item.expires > 0
                            else None,
                        )
                    )
                except ValueError:
                    raise AutoAuthError(
                        "service_protocol_error", "e站通返回了不合规的业务 Cookie。"
                    ) from None
        if not cookies:
            raise AutoAuthError("service_auth_failed", "e站通没有建立业务 Cookie。")
        return ServicePortalSession(
            cookies=cookies,
            state="stored",
            verified_at=now(),
            identity_verified=False,
            identity_stable=False,
            credential_generation=binding.generation,
            account_hash=binding.account_hash,
            source=source,  # type: ignore[arg-type]
        )
