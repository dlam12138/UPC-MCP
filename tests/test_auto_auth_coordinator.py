"""Real local locks/DPAPI and synthetic CAS; no school requests."""

import asyncio
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from upc_mcp.auto_auth.cas import CasRoundTrip, CasTicket
from upc_mcp.auto_auth.coordinator import AutoAuthCoordinator
from upc_mcp.auto_auth.credentials import CredentialStore
from upc_mcp.auto_auth.errors import AutoAuthError
from upc_mcp.auto_auth.locks import FileLock
from upc_mcp.auto_auth.providers.base import PreparedService


class FakeCas:
    def __init__(self, error=None):
        self.posts = 0
        self.probes = 0
        self.error = error
        self.entered = threading.Event()
        self.release = None

    def login_url(self, system):
        return "https://cas.upc.edu.cn/cas/login"

    def ticket(self, system, source="sso"):
        return CasTicket(system, "https://synthetic.invalid/?ticket=ST-test", 7, source)

    def get_ticket_at(self, url, system, cookies):
        self.probes += 1
        return self.ticket(system)

    def password_login(self, system, credential):
        self.posts += 1
        self.entered.set()
        if self.release:
            assert self.release.wait(5)
        if self.error:
            raise AutoAuthError(self.error, "Synthetic CAS failure")
        return CasRoundTrip(
            self.ticket(system, "password"),
            [{"name": "CASTGC", "value": "synthetic-only", "domain": "cas.upc.edu.cn",
              "path": "/cas/", "secure": True, "hostOnly": True}],
            1,
        )


class FakeProvider:
    def __init__(self, error=None):
        self.entered = threading.Event()
        self.release = None
        self.error = error
        self.calls = 0

    def prepare(self):
        return PreparedService()

    def establish(self, ticket, binding, prepared):
        self.calls += 1
        self.entered.set()
        if self.release:
            assert self.release.wait(5)
        if self.error:
            raise AutoAuthError(self.error, "Synthetic provider failure")
        return SimpleNamespace(account_hash=binding.account_hash,
                               credential_generation=binding.generation)


def configured(tmp_path, cas=None, providers=None):
    credential = CredentialStore(tmp_path)
    credential.store("synthetic-student", "synthetic-password")
    return AutoAuthCoordinator(
        tmp_path, credentials=credential, cas=cas or FakeCas(),
        providers=providers or {name: FakeProvider()
                               for name in ("shiguang", "timetable", "library")},
    )


def test_three_systems_and_independent_coordinators_share_one_password_login(tmp_path):
    cas = FakeCas()
    first = configured(tmp_path, cas)
    other = AutoAuthCoordinator(tmp_path, cas=cas, providers=first.providers)
    with ThreadPoolExecutor(3) as pool:
        tasks = [pool.submit(coord.recover_sync, name)
                 for coord, name in ((first, "library"), (other, "timetable"),
                                     (first, "shiguang"))]
        assert all(task.result(10).credential_generation == 1 for task in tasks)
    assert cas.posts == 1
    assert cas.probes == 2


@pytest.mark.parametrize("code", ["cas_invalid_credentials", "cas_captcha_required",
                                  "cas_second_factor_required", "cas_submit_timeout",
                                  "cas_network_error", "cas_upstream_error"])
def test_failed_login_blocks_other_systems_and_process_instances(tmp_path, code):
    cas = FakeCas(code)
    first = configured(tmp_path, cas)
    with pytest.raises(AutoAuthError):
        first.recover_sync("library")
    second = AutoAuthCoordinator(tmp_path, cas=cas, providers=first.providers)
    with pytest.raises(AutoAuthError):
        second.recover_sync("timetable")
    assert cas.posts == 1


def test_intent_is_persistent_before_password_is_submitted(tmp_path):
    cas = FakeCas()
    coordinator = configured(tmp_path, cas)
    original = cas.password_login

    def inspect_intent(system, credential):
        state = coordinator.state.load(credential.binding)
        assert state.blocked_code == "cas_login_in_flight"
        assert state.blocked_until > coordinator.clock()
        return original(system, credential)

    cas.password_login = inspect_intent
    coordinator.recover_sync("library")
    assert coordinator.state.load(coordinator.binding()).blocked_until == 0


def test_clear_while_password_in_flight_cannot_resurrect_checkpoint(tmp_path):
    cas = FakeCas()
    cas.release = threading.Event()
    coordinator = configured(tmp_path, cas)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(coordinator.recover_sync, "library")
        assert cas.entered.wait(3)
        coordinator.clear_credential()
        cas.release.set()
        with pytest.raises(AutoAuthError, match="凭据"):
            future.result(5)
    assert not coordinator.binding().configured
    assert not coordinator.cas_store.path.exists()
    assert not coordinator.state.path.exists()


def test_clear_during_business_bootstrap_discards_result(tmp_path):
    provider = FakeProvider()
    provider.release = threading.Event()
    coordinator = configured(tmp_path, providers={"library": provider})
    with ThreadPoolExecutor() as pool:
        future = pool.submit(coordinator.recover_sync, "library")
        assert provider.entered.wait(3)
        coordinator.clear_credential()
        provider.release.set()
        with pytest.raises(AutoAuthError) as error:
            future.result(5)
        assert error.value.code == "credential_changed"


def test_slow_library_does_not_hold_global_cas_lock(tmp_path):
    library = FakeProvider()
    library.release = threading.Event()
    timetable = FakeProvider()
    cas = FakeCas()
    coordinator = configured(tmp_path, cas, {"library": library, "timetable": timetable})
    with ThreadPoolExecutor() as pool:
        slow = pool.submit(coordinator.recover_sync, "library")
        assert library.entered.wait(3)
        try:
            assert pool.submit(coordinator.recover_sync, "timetable").result(3)
            assert timetable.calls == 1
        finally:
            library.release.set()
        slow.result(5)
    assert cas.posts == 1


def test_provider_backoff_applies_even_with_valid_sso_and_is_system_local(tmp_path):
    cas = FakeCas()
    bad = FakeProvider("service_upstream_error")
    coordinator = configured(tmp_path, cas, {"library": bad, "timetable": FakeProvider()})
    with pytest.raises(AutoAuthError):
        coordinator.recover_sync("library")
    with pytest.raises(AutoAuthError) as error:
        coordinator.recover_sync("library")
    assert error.value.code == "recovery_backoff"
    assert bad.calls == 1
    assert coordinator.recover_sync("timetable")
    assert cas.posts == 1


def test_corrupt_local_state_never_triggers_password(tmp_path):
    cas = FakeCas()
    coordinator = configured(tmp_path, cas)
    coordinator.state.path.write_text('{"broken":true}', encoding="utf-8")
    with pytest.raises(AutoAuthError):
        coordinator.recover_sync("library")
    assert cas.posts == 0


def test_cancelled_await_keeps_real_login_locked_until_worker_finishes(tmp_path):
    cas = FakeCas()
    cas.release = threading.Event()
    coordinator = configured(tmp_path, cas)

    async def scenario():
        first = asyncio.create_task(coordinator.recover("library"))
        assert await asyncio.to_thread(cas.entered.wait, 3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(coordinator.recover("timetable"))
        await asyncio.sleep(0.1)
        assert not second.done()
        assert cas.posts == 1
        cas.release.set()
        await asyncio.wait_for(second, 5)

    try:
        asyncio.run(scenario())
    finally:
        cas.release.set()
    assert cas.posts == 1


def test_real_cross_process_cas_file_lock(tmp_path):
    lock_path = tmp_path / "cas-login.lock"
    script = (
        "from pathlib import Path; import sys; "
        "from upc_mcp.auto_auth.locks import FileLock; "
        "from upc_mcp.auto_auth.errors import AutoAuthError; "
        "lock=FileLock(Path(sys.argv[1]),timeout=0.1)\n"
        "try:\n lock.acquire()\nexcept AutoAuthError:\n sys.exit(42)\n"
        "lock.release()\n"
    )
    with FileLock(lock_path).held():
        result = subprocess.run([sys.executable, "-c", script, str(lock_path)],
                                capture_output=True, timeout=10)
        assert result.returncode == 42
    result = subprocess.run([sys.executable, "-c", script, str(lock_path)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0


def test_corrupt_credential_does_not_break_tools_or_anonymous_features(tmp_path, monkeypatch):
    import httpx
    from mcp import Client

    from upc_mcp.server import create_server

    coordinator = configured(tmp_path / "auto-auth")
    coordinator.credentials.path.write_bytes(b"not-dpapi")
    coordinator.credentials.meta_path.write_text(json.dumps({"version": 0}), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("local discovery must not use network")

    monkeypatch.setattr(httpx.Client, "send", forbidden)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            assert len((await client.list_tools()).tools) == 23
            assert not (await client.call_tool("upc_capabilities", {})).is_error
            classrooms = await client.call_tool("upc_free_classrooms", {
                "campus": "invalid", "date": "2026-11-01", "periods": [1],
            })
            assert classrooms.structured_content["status"] == "invalid_campus"

    asyncio.run(scenario())
