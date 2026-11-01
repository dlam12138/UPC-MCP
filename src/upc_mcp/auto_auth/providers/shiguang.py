"""Shiguang provider: CAS ticket -> dekt transition -> activity list."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ...shiguang import LIST_PATH, ShiguangError, parse_activity_list
from ...shiguang_session import Cookie, ShiguangSession, now
from ..cas import CasTicket
from ..credentials import Binding
from ..errors import AutoAuthError
from .base import PreparedService
from .http import client, follow, protocol_errors, request

DEKT = "dekt.upc.edu.cn"
SECOND_STAGE = "/sysmng/login!loginByUserNoForCas.action"


class ShiguangProvider:
    system = "shiguang"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def prepare(self) -> PreparedService:
        return PreparedService()

    @protocol_errors
    def establish(
        self, ticket: CasTicket, binding: Binding, prepared: PreparedService
    ) -> ShiguangSession:
        source = "auto_auth_password" if ticket.source == "password" else "auto_auth_sso"
        with client(transport=self.transport) as http:
            response, _visited = follow(
                http, ticket.url, DEKT, allow_http=True, allow_5xx=True
            )
            if response.status_code == 200:
                match = re.search(
                    r"var\s+pcRedirectUrl\s*=\s*[\"']([^\"']+)[\"']", response.text
                )
                if match:
                    target = str(
                        httpx.URL(str(response.url)).join(
                            match.group(1).replace("&amp;", "&")
                        )
                    )
                    if urlsplit(target).path != SECOND_STAGE:
                        raise AutoAuthError(
                            "service_unexpected_redirect", "石光二级登录路径不在允许范围。"
                        )
                    response, _visited = follow(
                        http, target, DEKT, allow_http=True, max_hops=3
                    )
            try:
                listing = request(
                    http, "GET", "http://dekt.upc.edu.cn" + LIST_PATH,
                    params={"page": "1", "oper": "1"},
                )
            except httpx.TimeoutException:
                raise AutoAuthError(
                    "service_timeout", "石光活动列表请求超时。", details={"retryable": True}
                ) from None
            except httpx.HTTPError:
                raise AutoAuthError(
                    "service_network_error",
                    "石光活动列表网络请求失败。",
                    details={"retryable": True},
                ) from None
            if listing.status_code in {301, 302, 303, 307, 308, 401}:
                raise AutoAuthError("service_auth_failed", "石光新会话仍被要求登录。")
            if listing.status_code != 200:
                raise AutoAuthError(
                    "service_upstream_error",
                    f"石光活动列表返回 HTTP {listing.status_code}。",
                )
            try:
                parse_activity_list(listing.text, 1)
            except ShiguangError as exc:
                raise AutoAuthError(
                    "service_protocol_error", f"石光活动列表解析失败（{exc.code}）。"
                ) from None
            cookies = []
            for item in http.cookies.jar:
                if (item.domain or "").lstrip(".") != DEKT or not item.name or not item.value:
                    continue
                cookies.append(
                    Cookie(
                        name=item.name,
                        value=SecretStr(item.value),
                        domain=DEKT,
                        path=item.path or "/",
                        secure=bool(item.secure),
                        http_only=bool(
                            item.has_nonstandard_attr("HttpOnly")
                            or item.has_nonstandard_attr("httponly")
                        ),
                        expires=item.expires if item.expires and item.expires > 0 else None,
                        session=not bool(item.expires and item.expires > 0),
                    )
                )
        if not any(c.name == "SESSION" for c in cookies):
            raise AutoAuthError("service_auth_failed", "石光没有建立业务 SESSION。")
        return ShiguangSession(
            binding_id=binding.account_hash,
            cookies=cookies,
            state="stored",
            verified_at=now(),
            identity_verified=False,
            credential_generation=binding.generation,
            account_hash=binding.account_hash,
            source=source,  # type: ignore[arg-type]
        )
