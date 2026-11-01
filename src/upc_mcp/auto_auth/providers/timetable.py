"""Timetable provider: CAS ticket -> exact app callback -> get-index."""

from __future__ import annotations

import httpx

from ...timetable_protocol import (
    INDEX_PATH,
    TimetableCookie,
    TimetableError,
    parse_index,
)
from ...timetable_store import TimetableSession, now
from ..cas import CasTicket
from ..credentials import Binding
from ..errors import AutoAuthError
from .base import PreparedService
from .http import client, follow, protocol_errors, request

APP = "app.upc.edu.cn"


class TimetableProvider:
    system = "timetable"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def prepare(self) -> PreparedService:
        return PreparedService()

    @protocol_errors
    def establish(
        self, ticket: CasTicket, binding: Binding, prepared: PreparedService
    ) -> TimetableSession:
        source = "auto_auth_password" if ticket.source == "password" else "auto_auth_sso"
        with client(transport=self.transport) as http:
            follow(http, ticket.url, APP, max_hops=8)
            try:
                response = request(
                    http, "GET", "https://app.upc.edu.cn" + INDEX_PATH,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/plain, */*",
                        "Referer": "https://app.upc.edu.cn/site/weekschedule/index",
                    },
                )
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "service_timeout", "课表接口请求超时。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "service_network_error",
                    "课表接口网络请求失败。",
                    details={"retryable": True},
                ) from None
            if response.status_code in {301, 302, 303, 401}:
                raise AutoAuthError("service_auth_failed", "课表新会话未被接受。")
            if response.status_code != 200:
                raise AutoAuthError(
                    "service_upstream_error",
                    f"课表接口返回 HTTP {response.status_code}。",
                )
            try:
                payload = response.json()
                parse_index(payload)
            except (ValueError, TimetableError) as exc:
                code = getattr(exc, "code", None)
                if code == "auth_required":
                    raise AutoAuthError("service_auth_failed", "课表仍返回 10013。") from None
                raise AutoAuthError("service_protocol_error", "课表接口解析失败。") from None
            cookies = []
            for item in http.cookies.jar:
                if (item.domain or "").lstrip(".") != APP or not item.name or not item.value:
                    continue
                cookies.append(
                    TimetableCookie(
                        name=item.name,
                        value=item.value,
                        domain=APP,
                        path=item.path or "/",
                        secure=bool(item.secure),
                        http_only=bool(
                            item.has_nonstandard_attr("HttpOnly")
                            or item.has_nonstandard_attr("httponly")
                        ),
                        expires_at=float(item.expires)
                        if item.expires and item.expires > 0
                        else None,
                    )
                )
        if not cookies:
            raise AutoAuthError("service_auth_failed", "课表没有建立业务 Cookie。")
        return TimetableSession(
            cookies=cookies,
            state="stored",
            verified_at=now(),
            identity_verified=False,
            credential_generation=binding.generation,
            account_hash=binding.account_hash,
            source=source,  # type: ignore[arg-type]
        )
