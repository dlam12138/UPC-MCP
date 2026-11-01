"""Library provider: entry JSESSIONID -> ticket -> app token -> seat token."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from ...library_seat import (
    LibraryError,
    parse_exchange_token,
    parse_user,
    signed_headers,
)
from ...library_session import LibrarySession, now
from ..cas import CasTicket, prepare_library_entry
from ..credentials import Binding, account_hash
from ..errors import AutoAuthError
from .base import PreparedService
from .http import client, follow, protocol_errors, request

LIBRARY = "libspace.upc.edu.cn"
SSO_AUTH = "https://libspace.upc.edu.cn/rest/ssoAuth"
USER = "https://libspace.upc.edu.cn/rest/v2/user"


def _token_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    candidates = [parts.query]
    if parts.fragment:
        candidates.append(parts.fragment.split("?", 1)[-1])
    for candidate in candidates:
        values = parse_qs(candidate, keep_blank_values=False).get("token", [])
        for value in values:
            if value.strip() and len(value) <= 4096:
                return value.strip()
    return None


class LibraryProvider:
    system = "library"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    @protocol_errors
    def prepare(self) -> PreparedService:
        cas_url, cookies = prepare_library_entry(self.transport)
        return PreparedService(cas_url=cas_url, cookies=cookies)

    @protocol_errors
    def establish(
        self, ticket: CasTicket, binding: Binding, prepared: PreparedService
    ) -> LibrarySession:
        source = "auto_auth_password" if ticket.source == "password" else "auto_auth_sso"
        with client(prepared.cookies, transport=self.transport) as http:
            response, visited = follow(http, ticket.url, LIBRARY, max_hops=8)
            app_token = None
            for url in visited + [str(response.url)]:
                app_token = _token_from_url(url)
                if app_token:
                    break
            if not app_token:
                raise AutoAuthError(
                    "service_token_missing", "图书馆回调没有返回可兑换的 app token。"
                )
            try:
                exchange = request(
                    http, "POST", SSO_AUTH,
                    params={"token": app_token},
                    data={"token": app_token},
                    headers=signed_headers(app_token, "POST"),
                )
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "service_timeout", "图书馆令牌兑换超时。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "service_network_error",
                    "图书馆令牌兑换网络失败。",
                    details={"retryable": True},
                ) from None
            if exchange.status_code in {401, 403}:
                raise AutoAuthError("service_auth_failed", "图书馆拒绝了 app token。")
            if exchange.status_code != 200:
                raise AutoAuthError(
                    "service_upstream_error",
                    f"图书馆令牌兑换返回 HTTP {exchange.status_code}。",
                )
            try:
                seat_token = parse_exchange_token(exchange.text)
            except LibraryError as exc:
                raise AutoAuthError(
                    "service_protocol_error",
                    f"图书馆令牌兑换解析失败（{exc.code}）。",
                ) from None
            try:
                user_response = request(
                    http, "GET", USER,
                    params={"token": seat_token},
                    headers=signed_headers(seat_token, "GET"),
                )
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "service_timeout", "图书馆本人校验超时。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "service_network_error",
                    "图书馆本人校验网络失败。",
                    details={"retryable": True},
                ) from None
            if user_response.status_code in {401, 403}:
                raise AutoAuthError("service_auth_failed", "图书馆座位令牌未被接受。")
            if user_response.status_code != 200:
                raise AutoAuthError(
                    "service_upstream_error",
                    f"图书馆本人校验返回 HTTP {user_response.status_code}。",
                )
            try:
                user = parse_user(user_response.text)
            except LibraryError as exc:
                if exc.code == "auth_expired":
                    raise AutoAuthError("service_auth_failed", "图书馆座位令牌已失效。") from None
                raise AutoAuthError(
                    "service_protocol_error",
                    f"图书馆本人校验解析失败（{exc.code}）。",
                ) from None
        if account_hash(user.username) != binding.account_hash:
            raise AutoAuthError(
                "identity_mismatch",
                "图书馆返回的本人账号与本地统一凭据不一致；未保存该会话。",
            )
        identity = hashlib.sha256(
            f"libseat\0{user.id}\0{user.username}".encode()
        ).hexdigest()
        return LibrarySession(
            binding_id=binding.account_hash,
            token=SecretStr(seat_token),
            user_id=user.id,
            username=user.username,
            name=user.name,
            identity_hash=identity,
            identity_stable=True,
            state="stored",
            verified_at=now(),
            source=source,  # type: ignore[arg-type]
            credential_generation=binding.generation,
            account_hash=binding.account_hash,
        )
