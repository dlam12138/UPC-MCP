import asyncio
import contextlib
import inspect
import json
import threading

import httpx
import pytest

from upc_mcp.card_balance import (
    CardBalanceClient,
    CardBalanceError,
    build_form,
)
from upc_mcp.card_balance_profile import (
    MAX_LEGACY_CANDIDATE_BYTES,
    CardBalanceProfile,
    CardBalanceStore,
    protect,
    unprotect,
)
from upc_mcp.card_balance_service import CardBalanceService

STUDENT = "0" * 10
OTHER = "1" * 10


def response(**changes):
    card = {
        "sno": STUDENT,
        "db_balance": "1234",
        "unsettle_amount": "-1",
    }
    card.update(changes)
    return httpx.Response(
        200,
        text=json.dumps({"retcode": "0", "card": [card]}),
        headers={"Content-Type": "text/plain; charset=UTF-8"},
    )


def service_for(tmp_path, handler):
    store = CardBalanceStore(tmp_path / "state")
    return CardBalanceService(store, CardBalanceClient(httpx.MockTransport(handler)))


def test_status_before_configure_and_after_clear(tmp_path):
    service = service_for(tmp_path, lambda request: response())
    status = service.status()
    assert status["status"] == "not_configured"
    assert status["configured"] is False
    assert status["profile"] is None

    configured = service.configure(STUDENT)
    assert configured["status"] == "ok"
    assert configured["configured"] is True
    assert configured["profile"]["state"] == "configured"
    assert STUDENT not in json.dumps(configured, ensure_ascii=False)

    cleared = service.clear()
    assert cleared["status"] == "ok"
    assert cleared["configured"] is False
    assert cleared["local_profile_removed"] is True
    assert service.status()["status"] == "not_configured"


def test_profile_is_dpapi_encrypted_and_contains_only_the_identity(tmp_path):
    store = CardBalanceStore(tmp_path / "state")
    client = CardBalanceClient(httpx.MockTransport(lambda r: response()))
    service = CardBalanceService(store, client)
    service.configure(STUDENT)

    encrypted = store.path.read_bytes()
    assert STUDENT.encode() not in encrypted
    payload = json.loads(unprotect(encrypted))
    assert payload["student_id"] == STUDENT
    assert payload["source"] == "local_cli"
    assert "cookie" not in payload
    assert "headers" not in payload
    assert "account" not in payload
    assert "revision" in payload


def test_configure_same_id_is_idempotent_but_different_id_needs_clear(tmp_path):
    service = service_for(tmp_path, lambda request: response())
    first = service.configure(STUDENT)
    first_revision = service.store.load().revision
    second = service.configure(STUDENT)
    assert second["status"] == "ok"
    assert service.store.load().revision == first_revision

    with pytest.raises(CardBalanceError) as error:
        service.configure(OTHER)
    assert error.value.code == "already_configured"
    assert service.store.load().student_id.get_secret_value() == STUDENT
    assert first["configured"] is True


@pytest.mark.parametrize("value", [True, 12345678, "1234567", "12345678901234567", "abc"])
def test_configure_rejects_non_contract_identity(tmp_path, value):
    service = service_for(tmp_path, lambda request: response())
    with pytest.raises(CardBalanceError) as error:
        service.configure(value)
    assert error.value.code == "invalid_input"
    assert service.status()["status"] == "not_configured"


def test_query_dispatches_all_store_locks_off_the_event_loop(tmp_path):
    loop_thread = threading.get_ident()

    class RecordingStore(CardBalanceStore):
        def __init__(self, directory):
            super().__init__(directory)
            self.thread_ids = {
                "snapshot": [],
                "is_current": [],
                "mark_verified": [],
            }

        def snapshot(self):
            self.thread_ids["snapshot"].append(threading.get_ident())
            return super().snapshot()

        def is_current(self, revision):
            self.thread_ids["is_current"].append(threading.get_ident())
            return super().is_current(revision)

        def mark_verified(self, revision, expected_generation):
            self.thread_ids["mark_verified"].append(threading.get_ident())
            return super().mark_verified(revision, expected_generation)

    store = RecordingStore(tmp_path / "state")
    client = CardBalanceClient(httpx.MockTransport(lambda request: response()))
    service = CardBalanceService(store, client)
    service.configure(STUDENT)
    store.thread_ids = {key: [] for key in store.thread_ids}

    result = asyncio.run(service.query())

    assert result["status"] == "ok"
    for name, thread_ids in store.thread_ids.items():
        assert thread_ids, f"{name} was not called"
        assert all(thread_id != loop_thread for thread_id in thread_ids), name


def test_contended_store_lock_does_not_block_event_loop(tmp_path):
    async def scenario():
        store = CardBalanceStore(tmp_path / "state")
        client = CardBalanceClient(httpx.MockTransport(lambda request: response()))
        service = CardBalanceService(store, client)
        service.configure(STUDENT)

        acquired = threading.Event()
        release = threading.Event()
        holder_done = threading.Event()

        def holder():
            try:
                with store.locked():
                    acquired.set()
                    release.wait(5)
            finally:
                holder_done.set()

        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        assert await asyncio.to_thread(acquired.wait, 2)

        other_tool_ticks = {"count": 0}
        stop = asyncio.Event()

        async def other_tool():
            # Stand-in for a concurrent MCP tool on the same event loop.
            while not stop.is_set():
                other_tool_ticks["count"] += 1
                await asyncio.sleep(0.01)

        # Never depend on the event loop to release the lock: a regression that
        # blocks the loop must still let this test finish and report the stall.
        release_timer = threading.Timer(0.5, release.set)
        release_timer.start()
        query_task = asyncio.create_task(service.query())
        heartbeat = asyncio.create_task(other_tool())
        result = None
        try:
            await asyncio.sleep(0.2)
            observed_ticks = other_tool_ticks["count"]
            assert not query_task.done()
            assert observed_ticks >= 5
            result = await asyncio.wait_for(query_task, 3)
        finally:
            release.set()
            release_timer.cancel()
            stop.set()
            await heartbeat
            if not query_task.done():
                query_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await query_task
            holder_thread.join(timeout=2)
        assert result is not None
        assert result["status"] == "ok"
        assert holder_done.is_set()

    asyncio.run(scenario())


def test_query_takes_no_identity_from_the_caller():
    parameters = list(inspect.signature(CardBalanceService.query).parameters)
    assert parameters == ["self"]


def test_query_not_configured_sends_no_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return response()

    service = service_for(tmp_path, handler)
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "not_configured"
    assert calls == []


def test_query_uses_only_the_local_profile_and_returns_exact_amounts(tmp_path):
    seen = {}

    def handler(request):
        seen["content"] = request.content.decode("ascii")
        return response(db_balance="1234", unsettle_amount="120")

    service = service_for(tmp_path, handler)
    service.configure(STUDENT)
    result = asyncio.run(service.query())

    assert seen["content"] == build_form(STUDENT)
    assert result["status"] == "ok"
    assert result["source_system"] == "card_balance"
    assert result["query"] == {"subject": "self", "student_scope": "local_profile"}
    assert result["from_cache"] is False
    assert result["stale"] is False
    assert result["data"]["card_count"] == 1
    assert result["data"]["balance"] == "12.34"
    assert result["data"]["unsettle_amount"] == "1.20"
    assert result["data"]["currency"] == "CNY"
    assert result["data"]["upstream_amount_unit"] == "CNY_cent"
    assert result["data"]["validated_fields"] == [
        "retcode=0",
        "card.count=1",
        "card[0].sno",
        "card[0].db_balance",
        "card[0].unsettle_amount",
    ]
    serialized = json.dumps(result, ensure_ascii=False)
    assert STUDENT not in serialized
    assert "account" not in serialized

    status = service.status()
    assert status["profile"]["state"] == "verified"
    assert status["profile"]["identity_checked"] is True
    assert STUDENT not in json.dumps(status, ensure_ascii=False)


def test_query_network_error_does_not_claim_verified(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    service = service_for(tmp_path, handler)
    service.configure(STUDENT)
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "network_error"
    assert service.status()["profile"]["last_verified_at"] is None
    assert service.status()["profile"]["state"] == "configured"


def test_query_business_failure_does_not_claim_verified(tmp_path):
    def handler(request):
        return httpx.Response(200, text=json.dumps({"retcode": "60006", "card": []}))

    service = service_for(tmp_path, handler)
    service.configure(STUDENT)
    with pytest.raises(CardBalanceError) as error:
        asyncio.run(service.query())
    assert error.value.code == "upstream_error"
    assert service.status()["profile"]["last_verified_at"] is None


class GatedTransport(httpx.AsyncBaseTransport):
    def __init__(self, started, release):
        self.started = started
        self.release = release

    async def handle_async_request(self, request):
        self.started.set()
        await self.release.wait()
        return response()


def test_clear_during_query_discards_the_late_result(tmp_path):
    async def scenario():
        store = CardBalanceStore(tmp_path / "state")
        started, release = asyncio.Event(), asyncio.Event()
        service = CardBalanceService(store, CardBalanceClient(GatedTransport(started, release)))
        service.configure(STUDENT)

        task = asyncio.create_task(service.query())
        await started.wait()
        service.clear()
        release.set()
        with pytest.raises(CardBalanceError) as error:
            await task
        assert error.value.code == "profile_changed"
        assert service.status()["status"] == "not_configured"

    asyncio.run(scenario())


def test_legacy_candidate_migrates_only_the_student_id(tmp_path):
    store = CardBalanceStore(tmp_path / "state")
    client = CardBalanceClient(httpx.MockTransport(lambda r: response()))
    service = CardBalanceService(store, client)
    candidate = tmp_path / "retired-jhzf.dpapi"
    original = json.dumps(
        {
            "student_id": STUDENT,
            "account": "9" * 6,
            "cookie": "SYNTHETIC_COOKIE",
            "headers": {"X-Synthetic": "1"},
        }
    ).encode("utf-8")
    candidate.write_bytes(protect(original))
    candidate_bytes = candidate.read_bytes()

    result = service.configure_from_legacy_candidate(candidate)
    assert result["configured"] is True
    assert result["profile"]["source"] == "legacy_candidate"
    assert candidate.read_bytes() == candidate_bytes
    decrypted = json.loads(unprotect(store.path.read_bytes()))
    assert decrypted["student_id"] == STUDENT
    assert "SYNTHETIC_COOKIE" not in json.dumps(decrypted)
    assert "account" not in decrypted
    assert "headers" not in decrypted
    assert STUDENT not in json.dumps(result, ensure_ascii=False)


def test_legacy_candidate_cannot_overwrite_an_existing_binding(tmp_path):
    service = service_for(tmp_path, lambda request: response())
    service.configure(OTHER)
    candidate = tmp_path / "retired-jhzf.dpapi"
    # Deliberately not a DPAPI file: an existing binding must be rejected
    # before the candidate is touched or decrypted.
    candidate.write_bytes(b"not-a-dpapi-file")

    with pytest.raises(CardBalanceError) as error:
        service.configure_from_legacy_candidate(candidate)
    assert error.value.code == "already_configured"
    assert service.store.load().student_id.get_secret_value() == OTHER


def test_invalid_legacy_candidates_are_rejected_without_configuring(tmp_path):
    service = service_for(tmp_path, lambda request: response())

    missing = tmp_path / "missing.dpapi"
    missing.write_bytes(protect(b'{"account": "SYNTHETIC_ACCOUNT"}'))
    with pytest.raises(CardBalanceError) as error:
        service.configure_from_legacy_candidate(missing)
    assert error.value.code == "invalid_input"

    invalid = tmp_path / "invalid.dpapi"
    invalid.write_bytes(protect(b'{"student_id": "123"}'))
    with pytest.raises(CardBalanceError) as error:
        service.configure_from_legacy_candidate(invalid)
    assert error.value.code == "invalid_input"

    wrong_suffix = tmp_path / "candidate.json"
    wrong_suffix.write_bytes(b"x")
    with pytest.raises(CardBalanceError) as error:
        service.configure_from_legacy_candidate(wrong_suffix)
    assert error.value.code == "invalid_input"

    oversized = tmp_path / "oversized.dpapi"
    oversized.write_bytes(b"x" * (MAX_LEGACY_CANDIDATE_BYTES + 1))
    with pytest.raises(CardBalanceError) as error:
        service.configure_from_legacy_candidate(oversized)
    assert error.value.code == "invalid_input"

    assert service.status()["status"] == "not_configured"


def test_store_never_replaces_an_existing_profile(tmp_path):
    store = CardBalanceStore(tmp_path / "state")
    first = CardBalanceProfile.new(STUDENT)
    store.create(first)
    with pytest.raises(CardBalanceError) as error:
        store.create(CardBalanceProfile.new(OTHER))
    assert error.value.code == "already_configured"
    assert store.load().student_id.get_secret_value() == STUDENT


def test_clear_invalidates_old_verification_writes(tmp_path):
    store = CardBalanceStore(tmp_path / "state")
    profile = CardBalanceProfile.new(STUDENT)
    store.create(profile)
    _current, generation = store.snapshot()
    store.mark_verified(profile.revision, generation)
    store.clear()
    with pytest.raises(CardBalanceError) as error:
        store.mark_verified(profile.revision, generation)
    assert error.value.code == "profile_changed"


def test_corrupt_profile_is_a_local_error(tmp_path):
    store = CardBalanceStore(tmp_path / "state")
    store.directory.mkdir(parents=True)
    store.path.write_bytes(b"not-dpapi")
    with pytest.raises(CardBalanceError) as error:
        store.load()
    assert error.value.code == "local_error"


def test_balance_files_are_separate_from_old_jhzf_and_shiguang(tmp_path):
    old_jhzf = tmp_path / "jhzf-self.dpapi"
    old_shiguang = tmp_path / "shiguang-self.dpapi"
    old_jhzf.write_bytes(b"old-jhzf-bytes")
    old_shiguang.write_bytes(b"old-shiguang-bytes")
    before = (old_jhzf.read_bytes(), old_shiguang.read_bytes())

    service = service_for(tmp_path, lambda request: response())
    service.configure(STUDENT)
    asyncio.run(service.query())
    service.clear()

    assert (old_jhzf.read_bytes(), old_shiguang.read_bytes()) == before
    assert (tmp_path / "state" / "card-balance-self.dpapi").exists() is False
    assert (tmp_path / "state" / "card-balance.lock").exists() is True
