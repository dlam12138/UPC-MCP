"""Unified-auth service integration regressions; synthetic accounts, no network."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from upc_mcp.auto_auth.credentials import CredentialStore
from upc_mcp.library_seat import LibraryError
from upc_mcp.library_service import LibraryService
from upc_mcp.library_session import LibrarySession, LibraryStore
from upc_mcp.shiguang import ShiguangError
from upc_mcp.shiguang_service import ShiguangService
from upc_mcp.shiguang_session import Cookie, ShiguangSession, ShiguangStore
from upc_mcp.timetable_protocol import TimetableCookie, TimetableError
from upc_mcp.timetable_service import TimetableService
from upc_mcp.timetable_store import TimetableSession, TimetableStore

SYSTEMS = ("shiguang", "library", "timetable")
ERRORS = {"shiguang": ShiguangError, "library": LibraryError, "timetable": TimetableError}
STORES = {"shiguang": ShiguangStore, "library": LibraryStore, "timetable": TimetableStore}
SERVICES = {
    "shiguang": ShiguangService, "library": LibraryService, "timetable": TimetableService,
}


def make_session(system, binding, **changes):
    values = dict(
        source="auto_auth_sso", state="stored", credential_generation=binding.generation,
        account_hash=binding.account_hash,
    )
    if system == "shiguang":
        session = ShiguangSession(
            cookies=[Cookie(name="SESSION", value="synthetic-session", domain="dekt.upc.edu.cn")],
            **values,
        )
    elif system == "library":
        session = LibrarySession(
            token="synthetic-token", user_id="synthetic-user", username="synthetic-user",
            identity_hash="a" * 64, identity_stable=True, **values,
        )
    else:
        session = TimetableSession(
            cookies=[TimetableCookie(
                name="eai-sess", value="synthetic-session", domain="app.upc.edu.cn",
            )], **values,
        )
    return session.model_copy(update=changes)


class Auto:
    def __init__(self, directory, system):
        self.credentials = CredentialStore(directory / "auto-auth")
        self.credentials.store("synthetic-user", "synthetic-password")
        self.system = system
        self.calls = 0
        self.hook = None

    def binding(self):
        return self.credentials.snapshot()

    def recover_sync(self, system):
        assert system == self.system
        self.calls += 1
        result = make_session(system, self.binding())
        if self.hook:
            self.hook()
        return result


def setup(tmp_path, system):
    auto = Auto(tmp_path, system)
    store = STORES[system](tmp_path)
    service = SERVICES[system](store, auto_auth=auto)
    return SimpleNamespace(auto=auto, store=store, service=service, system=system)


async def read(case, operation=None):
    if operation is None:
        def operation(_session):
            return "ok"
    if case.system == "shiguang":
        case.service._execute = lambda session, _operation: operation(session)
        return await case.service._read(lambda _client: None)

    async def async_operation(session, _client):
        return operation(session)

    if case.system == "library":
        return await case.service._read(async_operation)
    return await case.service._run(async_operation)


@pytest.mark.parametrize("system", SYSTEMS)
def test_first_read_bootstraps_once_and_reuses_session(tmp_path, system):
    case = setup(tmp_path, system)

    async def scenario():
        assert await read(case) == "ok"
        assert await read(case) == "ok"
        assert case.auto.calls == 1
        saved = case.store.load()
        assert saved.account_hash == case.auto.binding().account_hash
        assert saved.credential_generation == case.auto.binding().generation

    asyncio.run(scenario())


@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("initially_empty", [True, False])
def test_clear_during_recovery_cannot_resurrect_session(tmp_path, system, initially_empty):
    case = setup(tmp_path, system)
    if not initially_empty:
        case.store.replace(make_session(system, case.auto.binding(), state="auth_expired"))
    entered, release = threading.Event(), threading.Event()
    case.auto.hook = lambda: (entered.set(), release.wait(3))

    async def scenario():
        task = asyncio.create_task(read(case))
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.to_thread(case.store.clear)
        release.set()
        with pytest.raises(ERRORS[system]):
            await task
        assert case.store.load() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("system", SYSTEMS)
def test_credential_clear_during_recovery_discards_result(tmp_path, system):
    case = setup(tmp_path, system)
    case.auto.hook = case.auto.credentials.clear
    with pytest.raises(ERRORS[system]) as error:
        asyncio.run(read(case))
    assert error.value.code == "credential_changed"
    assert case.store.load() is None


@pytest.mark.parametrize("system", SYSTEMS)
def test_different_existing_account_requires_explicit_clear(tmp_path, system):
    case = setup(tmp_path, system)
    original = make_session(system, case.auto.binding(), account_hash="b" * 64)
    case.store.replace(original)
    with pytest.raises(ERRORS[system]) as error:
        asyncio.run(read(case))
    assert error.value.code == "identity_mismatch"
    assert case.auto.calls == 0
    assert case.store.load().revision == original.revision


@pytest.mark.parametrize("system", SYSTEMS)
def test_same_system_concurrent_services_reuse_one_recovery(tmp_path, system):
    case = setup(tmp_path, system)
    other = SimpleNamespace(
        auto=case.auto, store=STORES[system](tmp_path), system=system,
        service=SERVICES[system](STORES[system](tmp_path), auto_auth=case.auto),
    )
    entered, release = threading.Event(), threading.Event()
    case.auto.hook = lambda: (entered.set(), release.wait(3))

    async def scenario():
        first = asyncio.create_task(read(case))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(read(other))
        # A blocked file-lock waiter must not block unrelated event-loop work.
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert not first.done()
        assert not second.done()
        release.set()
        assert await asyncio.gather(first, second) == ["ok", "ok"]
        assert case.auto.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("initially_empty", [True, False])
def test_a_call_cannot_recover_twice(tmp_path, system, initially_empty):
    case = setup(tmp_path, system)
    if not initially_empty:
        case.store.replace(make_session(system, case.auto.binding()))
    reads = 0

    def expired(_session):
        nonlocal reads
        reads += 1
        raise ERRORS[system]("auth_required", "synthetic expiry")

    with pytest.raises(ERRORS[system]):
        asyncio.run(read(case, expired))
    assert case.auto.calls == 1
    assert reads == (1 if initially_empty else 2)


@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("failure", ["access_denied", "upstream_error", "schema_changed"])
def test_non_auth_errors_do_not_trigger_login(tmp_path, system, failure):
    case = setup(tmp_path, system)
    case.store.replace(make_session(system, case.auto.binding()))

    def failed(_session):
        raise ERRORS[system](failure, "synthetic failure")

    with pytest.raises(ERRORS[system]) as error:
        asyncio.run(read(case, failed))
    assert error.value.code == failure
    assert case.auto.calls == 0


@pytest.mark.parametrize("system", SYSTEMS)
def test_clear_credential_prevents_old_session_use(tmp_path, system):
    case = setup(tmp_path, system)
    case.store.replace(make_session(system, case.auto.binding()))
    case.auto.credentials.clear()
    with pytest.raises(ERRORS[system]) as error:
        asyncio.run(read(case))
    assert error.value.code == "auth_required"
    assert case.auto.calls == 0


@pytest.mark.parametrize("system", SYSTEMS)
def test_rebind_during_successful_read_discards_old_identity_result(tmp_path, system):
    case = setup(tmp_path, system)
    case.store.replace(make_session(system, case.auto.binding()))

    def switch(_session):
        case.auto.credentials.store("other-synthetic-user", "other-synthetic-password")
        return "must not escape"

    with pytest.raises(ERRORS[system]) as error:
        asyncio.run(read(case, switch))
    assert error.value.code == "credential_changed"


def test_library_legacy_verified_identity_is_not_silently_replaced(tmp_path):
    case = setup(tmp_path, "library")
    existing = make_session(
        "library", case.auto.binding(), account_hash=None, credential_generation=None,
        identity_hash="b" * 64,
    )
    case.store.replace(existing)
    with pytest.raises(LibraryError) as error:
        asyncio.run(read(case))
    assert error.value.code == "identity_mismatch"
    assert case.store.load().revision == existing.revision


@pytest.mark.parametrize("system", ["shiguang", "library"])
def test_renewal_keeps_receipt_binding_stable(tmp_path, system):
    case = setup(tmp_path, system)
    original = make_session(system, case.auto.binding(), state="auth_expired", binding_id="stable")
    case.store.replace(original)
    assert asyncio.run(read(case)) == "ok"
    assert case.store.load().effective_binding_id() == "stable"


def test_library_post_auth_error_does_not_trigger_recovery(tmp_path, monkeypatch):
    case = setup(tmp_path, "library")
    case.store.replace(make_session("library", case.auto.binding()))
    posts = 0

    async def post(**arguments):
        nonlocal posts
        posts += 1
        arguments["state"]["receipt"] = True
        raise LibraryError("auth_expired", "synthetic POST auth error")

    monkeypatch.setattr(case.service, "_book_attempt", post)
    with pytest.raises(LibraryError):
        asyncio.run(case.service._book_locked(
            room_id="r1", seat_id="s1", day="2026-11-01", start_id="1", end_id="2",
        ))
    assert posts == 1
    assert case.auto.calls == 0

@pytest.mark.parametrize("system", SYSTEMS)
def test_cancelled_recovery_keeps_business_lock_until_worker_finishes(tmp_path, system):
    case = setup(tmp_path, system)
    other = SimpleNamespace(
        auto=case.auto, store=STORES[system](tmp_path), system=system,
        service=SERVICES[system](STORES[system](tmp_path), auto_auth=case.auto),
    )
    entered, release = threading.Event(), threading.Event()
    case.auto.hook = lambda: (entered.set(), release.wait(3))

    async def scenario():
        first = asyncio.create_task(read(case))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(read(other))
        await asyncio.sleep(0.05)
        assert not second.done()
        assert case.auto.calls == 1
        release.set()
        assert await second == "ok"
        assert case.auto.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("system", SYSTEMS)
def test_rebind_during_business_bootstrap_preserves_new_binding(tmp_path, system):
    case = setup(tmp_path, system)
    replacement = make_session(system, case.auto.binding(), account_hash="b" * 64)
    case.auto.hook = lambda: case.store.replace(replacement)
    with pytest.raises(ERRORS[system]):
        asyncio.run(read(case))
    assert case.store.load().revision == replacement.revision


def test_shiguang_write_auth_error_does_not_recover_or_repeat(tmp_path, monkeypatch):
    case = setup(tmp_path, "shiguang")
    original = make_session("shiguang", case.auto.binding())
    case.store.replace(original)
    posts = 0

    def preview(session, generation, _activity, _kind, **_options):
        return {
            "title": "synthetic activity",
            "registration": {
                "status": "open", "normal_supported": True, "invite_code_supported": False,
            },
        }, session, generation

    def submit(_session, _operation):
        nonlocal posts
        posts += 1
        raise ShiguangError("auth_required", "synthetic POST auth failure")

    monkeypatch.setattr(case.service, "_preview_registration_sync", preview)
    monkeypatch.setattr(case.service, "_execute", submit)
    with pytest.raises(ShiguangError):
        asyncio.run(case.service.register_activity("synthetic-activity", "activity"))
    assert posts == 1
    assert case.auto.calls == 0


def test_shiguang_legacy_conditional_sso_survives_without_unified_credentials(
    tmp_path, monkeypatch,
):
    case = setup(tmp_path, "shiguang")
    legacy = make_session(
        "shiguang", case.auto.binding(), account_hash=None,
        credential_generation=None, state="auth_expired",
    )
    legacy.cookies.append(Cookie(
        name="CASTGC", value="synthetic-cas-cookie", domain="cas.upc.edu.cn", path="/cas/",
    ))
    case.store.replace(legacy)
    case.auto.credentials.clear()
    requests = 0
    recoveries = 0

    def execute(_session, _operation):
        nonlocal requests
        requests += 1
        if requests == 1:
            raise ShiguangError("session_expired", "synthetic expiry")
        return "ok"

    def recover(session, _generation):
        nonlocal recoveries
        recoveries += 1
        updated = session.model_copy(update={"state": "stored"})
        case.store.replace(updated)
        return updated, False

    monkeypatch.setattr(case.service, "_execute", execute)
    monkeypatch.setattr(case.service.recovery, "recover_or_reuse", recover)
    assert asyncio.run(case.service._read(lambda _client: None)) == "ok"
    assert requests == 2
    assert recoveries == 1
    assert case.auto.calls == 0
