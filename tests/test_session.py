import asyncio
import json

import httpx
import pytest
from conftest import ACCOUNT, STUDENT

from upc_mcp.auth import adopt, from_recording
from upc_mcp.errors import UPCError
from upc_mcp.jhzf import BillService, JhzfClient
from upc_mcp.storage import SessionStore, protect


def test_dpapi_round_trip_and_clear_does_not_restore_old_session(tmp_path, session):
    store = SessionStore(tmp_path)
    store.replace(session)
    encrypted = store.path.read_bytes()
    assert session.cookie.get_secret_value().encode() not in encrypted
    assert STUDENT.encode() not in encrypted
    assert store.load().cookie.get_secret_value() == session.cookie.get_secret_value()
    store.clear()
    with pytest.raises(UPCError):
        store.finish(session.revision)
    assert store.load() is None


def test_rebinding_rejects_old_revision(tmp_path, session):
    store = SessionStore(tmp_path)
    store.replace(session)
    replacement = session.model_copy(update={"revision": "new-binding"})
    store.replace(replacement)
    with pytest.raises(UPCError):
        store.finish(session.revision)
    assert store.load().revision == "new-binding"


def test_logout_during_network_request_discards_response(tmp_path, session, bill):
    async def scenario():
        store = SessionStore(tmp_path)
        store.replace(session)
        started, release = asyncio.Event(), asyncio.Event()

        async def respond(request):
            started.set()
            await release.wait()
            return httpx.Response(200, text=json.dumps(bill))

        service = BillService(store, JhzfClient(httpx.MockTransport(respond)))
        task = asyncio.create_task(service.query("2026-01"))
        await started.wait()
        store.clear()
        release.set()
        with pytest.raises(UPCError) as error:
            await task
        assert error.value.code == "auth_required"
        assert store.load() is None

    asyncio.run(scenario())


def test_unknown_500_preserves_credentials_but_not_claimed_valid(tmp_path, session):
    store = SessionStore(tmp_path)
    store.replace(session)
    client = JhzfClient(httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(UPCError) as error:
        asyncio.run(BillService(store, client).query("2026-01"))
    assert error.value.code == "upstream_error"
    assert store.load().state == "unverified"
    assert store.load().cookie.get_secret_value() == session.cookie.get_secret_value()


def test_explicit_denial_requires_new_auth_without_retry(tmp_path, session):
    store = SessionStore(tmp_path)
    store.replace(session)
    calls = []

    def deny(request):
        calls.append(request)
        return httpx.Response(401)

    service = BillService(store, JhzfClient(httpx.MockTransport(deny)))
    for _ in range(2):
        with pytest.raises(UPCError) as error:
            asyncio.run(service.query("2026-01"))
        assert error.value.code == "auth_expired"
    assert len(calls) == 1


def test_recording_import_checks_owner_before_replacing(tmp_path, session, capture_record):
    source = tmp_path / "synthetic-capture.dpapi"
    source.write_bytes(protect(json.dumps(capture_record).encode()))
    imported = from_recording(source)
    store = SessionStore(tmp_path / "state")
    store.replace(session)
    client = JhzfClient(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"retcode": "0", "card": [{"sno": STUDENT, "account": "not-the-bound-card"}]},
            )
        )
    )
    with pytest.raises(UPCError) as error:
        asyncio.run(adopt(imported, store, client))
    assert error.value.code == "identity_mismatch"
    assert store.load().revision == session.revision


def test_normal_recording_can_be_adopted_without_model_credentials(tmp_path, capture_record, bill):
    source = tmp_path / "synthetic-capture.dpapi"
    source.write_bytes(protect(json.dumps(capture_record).encode()))
    imported = from_recording(source)
    store = SessionStore(tmp_path / "state")

    def respond(request):
        if request.url.path.endswith("getCardInfo.html"):
            return httpx.Response(
                200, json={"retcode": "0", "card": [{"sno": STUDENT, "account": ACCOUNT}]}
            )
        return httpx.Response(200, json=bill)

    result = asyncio.run(adopt(imported, store, JhzfClient(httpx.MockTransport(respond))))
    assert result["status"] == "ok"
    assert "cookie" not in json.dumps(result).lower()
    assert STUDENT not in json.dumps(result)
