"""Synthetic accounts only; the transport never accesses school services."""

import asyncio
import json

import httpx
import pytest

from upc_mcp.auto_auth.credentials import CredentialStore
from upc_mcp.card_balance import CardBalanceClient, CardBalanceError, build_form
from upc_mcp.card_balance_profile import CardBalanceStore
from upc_mcp.card_balance_service import CardBalanceService
from upc_mcp.settings_ui import SettingsApplication

A = "0000000000"
B = "1111111111"
PASSWORD = "synthetic-password-only"


def make_service(tmp_path, handler):
    credentials = CredentialStore(tmp_path / "auto-auth")
    service = CardBalanceService(
        CardBalanceStore(tmp_path), CardBalanceClient(httpx.MockTransport(handler)),
        credentials=credentials,
    )
    return service, credentials


def response(student):
    return httpx.Response(200, json={"retcode": "0", "card": [{
        "sno": student, "db_balance": "1200", "unsettle_amount": "0",
    }]})


def test_settings_account_queries_balance_without_separate_profile(tmp_path):
    app = SettingsApplication(tmp_path)
    app.save({"username": A, "password": PASSWORD, "password_confirm": PASSWORD,
              "expected_generation": 0})
    seen = []

    def handler(request):
        seen.append(request)
        assert request.content.decode() == build_form(A)
        assert PASSWORD not in request.content.decode()
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return response(A)

    service, credentials = make_service(tmp_path, handler)
    result = asyncio.run(service.query())
    assert result["query"]["student_scope"] == "unified_account"
    assert service.status()["profile"]["source"] == "unified_account"
    assert A not in json.dumps(result)
    assert PASSWORD not in json.dumps(service.status())
    assert len(seen) == 1
    assert not service.store.path.exists()
    assert credentials.snapshot().configured


def test_rebind_overrides_old_standalone_profile_and_clear_never_restores_it(tmp_path):
    requested = []

    def handler(request):
        requested.append(request.content.decode())
        return response(B)

    service, credentials = make_service(tmp_path, handler)
    service.configure(A)
    credentials.store(B, PASSWORD)
    asyncio.run(service.query())
    assert requested == [build_form(B)]
    with pytest.raises(CardBalanceError, match="余额学号跟随统一账号"):
        service.configure(A)
    credentials.clear()
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "not_configured"
    assert len(requested) == 1


@pytest.mark.parametrize("change", ["clear", "rebind", "password"])
def test_credential_change_during_query_discards_result(tmp_path, change):
    service, credentials = make_service(tmp_path, None)
    credentials.store(A, PASSWORD)

    def handler(request):
        if change == "clear":
            credentials.clear()
        else:
            credentials.store(B if change == "rebind" else A, PASSWORD + "2")
        return response(A)

    service.client = CardBalanceClient(httpx.MockTransport(handler))
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "profile_changed"


def test_invalid_unified_account_does_not_fallback_or_request(tmp_path):
    def handler(request):
        pytest.fail("Invalid identity must not query the school")

    service, credentials = make_service(tmp_path, handler)
    service.configure(A)
    credentials.store("synthetic-nonstudent", PASSWORD)
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "invalid_student_id"


def test_new_unified_account_during_legacy_query_discards_old_result(tmp_path):
    service, credentials = make_service(tmp_path, None)
    service.configure(A)

    def handler(request):
        credentials.store(B, PASSWORD)
        return response(A)

    service.client = CardBalanceClient(httpx.MockTransport(handler))
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "profile_changed"


def test_unreadable_unified_credentials_never_fallback(tmp_path):
    service, credentials = make_service(tmp_path, None)
    service.configure(A)
    credentials.store(B, PASSWORD)
    credentials.path.write_bytes(b"invalid encrypted credential")
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "local_error"
