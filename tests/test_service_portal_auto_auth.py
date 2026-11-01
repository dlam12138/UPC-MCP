from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from upc_mcp.auto_auth.cas import (
    CasAuth,
    CasTicket,
    _ticket_from_location,
    validate_cas_url,
)
from upc_mcp.auto_auth.coordinator import AutoAuthCoordinator
from upc_mcp.auto_auth.credentials import Binding
from upc_mcp.auto_auth.errors import AutoAuthError
from upc_mcp.auto_auth.providers.base import PreparedService
from upc_mcp.auto_auth.providers.service_portal import ServicePortalProvider
from upc_mcp.service_portal_protocol import SERVICE_HOST

REDIRECT = "https://service.upc.edu.cn/v2/site/index"
CALLBACK = (
    "https://service.upc.edu.cn/site/login/cas-login"
    "?redirect_url=https%3A%2F%2Fservice.upc.edu.cn%2Fv2%2Fsite%2Findex"
    "&ticket=ST-abcdefghijklmnopqrstuvwxyz0123456789"
)


def test_login_url_and_allowlist_are_exact():
    url = CasAuth.login_url("service_portal")
    validate_cas_url(url, "service_portal")
    query = parse_qs(urlsplit(url).query)
    assert query["service"] == [
        "https://service.upc.edu.cn/site/login/cas-login"
        "?redirect_url=https%3A%2F%2Fservice.upc.edu.cn%2Fv2%2Fsite%2Findex"
    ]
    assert "from" not in query
    ticket = _ticket_from_location(CALLBACK, "service_portal", "sso")
    assert ticket.system == "service_portal"
    assert ticket.ticket_len == len("ST-abcdefghijklmnopqrstuvwxyz0123456789")


def test_callback_rejects_wrong_redirect_extra_query_and_missing_ticket():
    with pytest.raises(AutoAuthError) as exc:
        _ticket_from_location(
            CALLBACK.replace("%2Fv2%2Fsite%2Findex", "%2Fv2%2Fother"),
            "service_portal",
            "sso",
        )
    assert exc.value.code == "cas_unexpected_redirect"
    with pytest.raises(AutoAuthError):
        _ticket_from_location(CALLBACK + "&extra=1", "service_portal", "sso")
    with pytest.raises(AutoAuthError):
        _ticket_from_location(
            CALLBACK.replace("&ticket=ST-abcdefghijklmnopqrstuvwxyz0123456789", ""),
            "service_portal",
            "sso",
        )


def test_validate_cas_url_rejects_wrong_service_target():
    with pytest.raises(AutoAuthError):
        validate_cas_url(
            "https://cas.upc.edu.cn/cas/login?service="
            "https%3A%2F%2Fservice.upc.edu.cn%2Fsite%2Flogin%2Fcas-login",
            "service_portal",
        )


def test_sso_only_ticket_path_refuses_password_fallback(tmp_path):
    coordinator = AutoAuthCoordinator(
        tmp_path, providers={"service_portal": ServicePortalProvider()}
    )
    current = Binding(True, 1, "a" * 64)
    coordinator.credentials.snapshot = lambda: current
    coordinator.cas_store.load = lambda _binding: []
    coordinator.state.load = lambda _binding: SimpleNamespace(
        blocked_until=0.0, system_backoff={}, requires_user_action=False
    )
    with pytest.raises(AutoAuthError) as exc:
        coordinator._ticket_for(
            "service_portal",
            current,
            PreparedService(),
            allow_password=False,
        )
    assert exc.value.code == "auth_required"
    assert "密码" in str(exc.value)


def _provider_transport(set_cookie: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/site/login/cas-login":
            assert request.url.params["ticket"].startswith("ST-")
            return httpx.Response(
                302,
                headers={
                    "Location": "/v2/site/index",
                    "Set-Cookie": set_cookie,
                },
            )
        if request.url.path == "/v2/site/index":
            return httpx.Response(200, text="<html>index</html>")
        if request.url.path == "/site/message/count":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps({"e": 0, "d": {"num": 0}, "m": ""}).encode(),
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_provider_establishes_independent_service_session():
    transport = _provider_transport(
        "PHPSESSID=synthetic; Domain=service.upc.edu.cn; Path=/; Secure; HttpOnly"
    )
    binding = Binding(True, 1, "a" * 64)
    session = ServicePortalProvider(transport).establish(
        CasTicket(
            system="service_portal",
            url=CALLBACK,
            ticket_len=40,
            source="sso",
        ),
        binding,
        PreparedService(),
    )
    assert [cookie.name for cookie in session.cookies] == ["PHPSESSID"]
    assert session.cookies[0].domain == SERVICE_HOST
    assert session.cookies[0].http_only is True
    assert session.source == "auto_auth_sso"
    assert session.identity_verified is False
    assert session.account_hash == binding.account_hash


def test_provider_rejects_broad_parent_cookie():
    transport = _provider_transport(
        "PHPSESSID=synthetic; Domain=.upc.edu.cn; Path=/; Secure"
    )
    binding = Binding(True, 1, "a" * 64)
    with pytest.raises(AutoAuthError) as exc:
        ServicePortalProvider(transport).establish(
            CasTicket(
                system="service_portal",
                url=CALLBACK,
                ticket_len=40,
                source="sso",
            ),
            binding,
            PreparedService(),
        )
    assert exc.value.code == "service_auth_failed"


def test_provider_maps_second_factor_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/site/login/cas-login":
            return httpx.Response(
                302,
                headers={
                    "Location": "/v2/site/index",
                    "Set-Cookie": "PHPSESSID=x; Domain=service.upc.edu.cn; Path=/",
                },
            )
        if request.url.path == "/v2/site/index":
            return httpx.Response(200, text="ok")
        if request.url.path == "/site/message/count":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"e":30000,"d":null,"m":"must not cross"}',
            )
        return httpx.Response(404)

    with pytest.raises(AutoAuthError) as exc:
        ServicePortalProvider(httpx.MockTransport(handler)).establish(
            CasTicket(
                system="service_portal",
                url=CALLBACK,
                ticket_len=40,
                source="sso",
            ),
            Binding(True, 1, "a" * 64),
            PreparedService(),
        )
    assert exc.value.code == "service_second_factor_required"
    assert "must not cross" not in str(exc.value)
