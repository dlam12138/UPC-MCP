from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from upc_mcp.auto_auth.errors import AutoAuthError
from upc_mcp.service_portal_protocol import ServicePortalError
from upc_mcp.service_portal_service import ServicePortalService
from upc_mcp.service_portal_store import (
    ServicePortalCookie,
    ServicePortalSession,
    ServicePortalStore,
)


def binding(account_hash="a" * 64, generation=1, configured=True):
    return SimpleNamespace(
        account_hash=account_hash,
        generation=generation,
        configured=configured,
        account_ref=lambda: account_hash[:16],
    )


def session(account_hash="a" * 64, generation=1, state="stored"):
    return ServicePortalSession(
        cookies=[
            ServicePortalCookie(
                name="PHPSESSID",
                value=SecretStr("synthetic"),
                domain="service.upc.edu.cn",
                path="/",
                secure=True,
                http_only=True,
                host_only=True,
            )
        ],
        state=state,
        source="auto_auth_sso",
        credential_generation=generation,
        account_hash=account_hash,
        verified_at="2026-11-01T11:00:00+08:00",
    )


class FakeAuto:
    def __init__(self, current=None, recover=None, error=None):
        self.current = current or binding()
        self.recover_session = recover
        self.error = error
        self.calls = 0
        self.allow_password = None
        self.credentials = self

    @contextmanager
    def guard(self):
        yield

    def snapshot_locked(self):
        return self.current

    def binding(self):
        return self.current

    def recover_sync(self, system, *, allow_password=True):
        self.calls += 1
        self.allow_password = allow_password
        if self.error:
            raise self.error
        if callable(self.recover_session):
            return self.recover_session()
        return self.recover_session or session(
            account_hash=self.current.account_hash,
            generation=self.current.generation,
        )


class FakeClient:
    def __init__(self, *, todo=None, app=None, catalog=None, fail_first=None):
        self.todo = todo or {
            "total": 0,
            "count": 0,
            "empty": True,
            "has_more": False,
            "items": [],
            "status_filter": 1,
            "page": 1,
        }
        self.app = app or {
            "total": 0,
            "count": 0,
            "empty": True,
            "has_more": False,
            "items": [],
            "third_party_count": 0,
            "status_filter": 1,
            "page": 1,
        }
        self.catalog_rows = catalog or []
        self.fail_first = fail_first
        self.calls = []
        self.closed = 0

    def search(self, *, keyword, page, page_size):
        self.calls.append(("search", keyword, page, page_size))
        return {
            "total": 1,
            "page": page,
            "page_size": page_size,
            "count": 1,
            "has_more": False,
            "items": [
                {
                    "id": 1,
                    "name": "synthetic",
                    "department": "部门",
                    "type": 1,
                    "access": "native_form",
                    "external_host": None,
                    "folder": False,
                }
            ],
        }

    def guide(self, service_id, *, department):
        self.calls.append(("guide", service_id, department))
        return {
            "service_id": service_id,
            "name": "synthetic",
            "department": department,
            "description": "synthetic",
            "materials": [],
            "materials_status": "empty",
            "warnings": [],
        }

    def catalog(self):
        self.calls.append(("catalog",))
        return self.catalog_rows

    def todos(self, account_hash):
        self.calls.append(("todos", account_hash))
        if self.fail_first:
            self.fail_first = False
            raise ServicePortalError("auth_required", "expired")
        return self.todo

    def applications(self, account_hash):
        self.calls.append(("applications", account_hash))
        if self.fail_first:
            self.fail_first = False
            raise ServicePortalError("auth_expired", "expired")
        return self.app

    def verify(self):
        self.calls.append(("verify",))
        if self.fail_first:
            self.fail_first = False
            raise ServicePortalError("auth_required", "expired")
        return {"unread_count": 0}

    def close(self):
        self.closed += 1


def make_service(tmp_path, client, auto=None, **kwargs):
    return ServicePortalService(
        ServicePortalStore(tmp_path),
        client_factory=lambda cookies: client,
        auto_auth=auto,
        **kwargs,
    )


@pytest.mark.parametrize("initially_empty", [True, False])
@pytest.mark.parametrize("change", ["clear", "rebind"])
def test_credential_change_after_auth_before_persist_discards_session(
    tmp_path, initially_empty, change
):
    auto = FakeAuto()
    client = FakeClient()
    service = make_service(tmp_path, client, auto)
    if not initially_empty:
        service.store.replace(session(state="auth_expired"))
    baseline = service.store.load()

    def recover():
        result = session()
        auto.current = binding(
            account_hash="b" * 64 if change == "rebind" else None,
            generation=2, configured=change != "clear",
        )
        return result

    auto.recover_session = recover
    with pytest.raises(ServicePortalError) as error:
        asyncio.run(service.my_todos())
    assert error.value.code == "credential_changed"
    saved = service.store.load()
    assert (saved.revision if saved else None) == (baseline.revision if baseline else None)
    assert client.calls == []


@pytest.mark.parametrize("change", ["credential_clear", "credential_rebind", "business_clear"])
def test_late_personal_result_is_discarded_without_recovery_or_read_retry(tmp_path, change):
    auto = FakeAuto()
    client = FakeClient()
    service = make_service(tmp_path, client, auto)
    service.store.replace(session())
    calls = []

    def query(_account):
        calls.append("query")
        if change == "business_clear":
            service.store.clear()
        else:
            auto.current = binding(
                account_hash="b" * 64 if change == "credential_rebind" else None,
                generation=2, configured=change != "credential_clear",
            )
        return {"total": 1, "items": [{"title": "synthetic-private-record"}]}

    client.todos = query
    with pytest.raises(ServicePortalError):
        asyncio.run(service.my_todos())
    assert calls == ["query"]
    assert auto.calls == 0


@pytest.mark.parametrize("kind", ["session", "generation"])
def test_oversized_local_state_is_rejected_before_decryption(tmp_path, monkeypatch, kind):
    import upc_mcp.service_portal_store as store_module

    store = ServicePortalStore(tmp_path)
    limit = (store_module.MAX_SESSION_BYTES if kind == "session"
             else store_module.MAX_GENERATION_BYTES)
    target = store.path if kind == "session" else store.generation_path
    target.write_bytes(b"x" * (limit + 1))

    def forbidden(_data):
        raise AssertionError("oversized state must not reach DPAPI")

    monkeypatch.setattr(store_module, "unprotect", forbidden)
    with pytest.raises(ServicePortalError):
        store.snapshot()


def test_cookie_transport_preserves_secure_path_expiry_and_host_only():
    import httpx

    from upc_mcp.service_portal_store import jar_from_cookies

    cookie = ServicePortalCookie(
        name="synthetic", value="synthetic-private-cookie", domain="service.upc.edu.cn",
        path="/site/", secure=True, http_only=True, host_only=True,
        expires_at=time.time() + 3600,
    )
    jar = jar_from_cookies([cookie])
    stored = next(iter(jar.jar))
    assert stored.secure and not stored.domain_specified
    assert stored.has_nonstandard_attr("HttpOnly")
    assert stored.expires == int(cookie.expires_at)
    with httpx.Client(cookies=jar) as client:
        assert "synthetic=" in client.build_request(
            "GET", "https://service.upc.edu.cn/site/my-task/todo"
        ).headers["cookie"]
        assert "cookie" not in client.build_request(
            "GET", "http://service.upc.edu.cn/site/my-task/todo"
        ).headers
        assert "cookie" not in client.build_request(
            "GET", "https://service.upc.edu.cn/other/path"
        ).headers


def test_anonymous_search_and_guide_do_not_touch_auto_auth(tmp_path):
    async def _run():
        client = FakeClient(catalog=[{"id": 1, "departName": "部门"}])
        auto = FakeAuto(error=AssertionError("must not be called"))
        service = make_service(tmp_path, client, auto=auto)
        search = await service.search(keyword=" 事项 ", page=2, page_size=10)
        assert search["status"] == "ok"
        assert search["session_ref"] == "anonymous"
        assert search["query"] == {"keyword": "事项", "page": 2, "page_size": 10}
        guide = await service.guide(1)
        assert guide["status"] == "ok"
        assert guide["department"] == "部门"
        uncached = await service.guide(999)
        assert uncached["department"] is None
        assert "department_not_cached" in uncached["warnings"]
        assert auto.calls == 0
        assert client.calls[0] == ("search", "事项", 2, 10)
    asyncio.run(_run())

def test_public_search_survives_bad_credential_or_no_auto_auth(tmp_path):
    async def _run():
        client = FakeClient()
        service = make_service(tmp_path, client, auto=None)
        assert (await service.search(keyword=None))["status"] == "ok"
    asyncio.run(_run())

def test_personal_without_auto_auth_is_auth_required(tmp_path):
    async def _run():
        service = make_service(tmp_path, FakeClient(), auto=None)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "auth_required"
        assert exc.value.action == "credential_configure"
    asyncio.run(_run())

def test_todo_uses_binding_account_and_reports_empty_state(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        client = FakeClient()
        auto = FakeAuto(error=AssertionError("must not recover"))
        service = make_service(tmp_path, client, auto=auto)
        result = await service.my_todos()
        assert result["status"] == "ok"
        assert result["session_ref"] == "self"
        assert result["empty"] is True
        assert result["coverage"]["status_filter"] == 1
        assert client.calls == [("todos", "a" * 64)]
    asyncio.run(_run())

def test_one_auth_recovery_then_one_retry(tmp_path):
    async def _run():
        auto = FakeAuto(recover=session())
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        client = FakeClient(fail_first=True)
        service = make_service(tmp_path, client, auto=auto)
        result = await service.my_applications()
        assert result["status"] == "ok"
        assert auto.calls == 1
        assert [call[0] for call in client.calls] == ["applications", "applications"]
        assert (await asyncio.to_thread(service.store.load)).credential_generation == 1
    asyncio.run(_run())

def test_second_auth_failure_does_not_loop(tmp_path):
    async def _run():
        class AlwaysFail(FakeClient):
            def todos(self, account_hash):
                self.calls.append(("todos", account_hash))
                raise ServicePortalError("auth_required", "expired")

        auto = FakeAuto(recover=session())
        service = make_service(tmp_path, AlwaysFail(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "auth_required"
        assert auto.calls == 1
    asyncio.run(_run())

def test_second_factor_is_not_retried_or_recovered(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())

        class Totp(FakeClient):
            def todos(self, account_hash):
                raise ServicePortalError(
                    "user_action_required",
                    "second factor",
                    action="service_portal_unlock",
                    details={"kind": "second_factor"},
                )

        auto = FakeAuto(error=AssertionError("must not recover"))
        service = make_service(tmp_path, Totp(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "user_action_required"
        assert auto.calls == 0
    asyncio.run(_run())

def test_upstream_403_and_schema_do_not_trigger_login(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        auto = FakeAuto(error=AssertionError("must not recover"))

        class Upstream(FakeClient):
            def todos(self, account_hash):
                raise ServicePortalError("service_upstream_error", "503")

        service = make_service(tmp_path, Upstream(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "service_upstream_error"
        assert auto.calls == 0

        class Schema(FakeClient):
            def todos(self, account_hash):
                raise ServicePortalError("schema_changed", "bad")

        service = make_service(tmp_path, Schema(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "schema_changed"
        assert auto.calls == 0
    asyncio.run(_run())

def test_same_account_generation_change_recovers_and_rebinds(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session(generation=1))
        auto = FakeAuto(current=binding(generation=2), recover=session(generation=2))
        service = make_service(tmp_path, FakeClient(), auto=auto)
        result = await service.my_todos()
        assert result["status"] == "ok"
        assert auto.calls == 1
        assert (await asyncio.to_thread(store.load)).credential_generation == 2
    asyncio.run(_run())

def test_different_account_is_identity_mismatch_and_not_overwritten(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        old = session(account_hash="a" * 64)
        store.replace(old)
        auto = FakeAuto(current=binding(account_hash="b" * 64))
        service = make_service(tmp_path, FakeClient(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "identity_mismatch"
        assert auto.calls == 0
        assert (await asyncio.to_thread(store.load)).account_hash == "a" * 64
    asyncio.run(_run())

def test_clear_during_recovery_discards_late_result(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)

        def clear_then_return():
            store.clear()
            return session()

        auto = FakeAuto(recover=clear_then_return)
        service = make_service(tmp_path, FakeClient(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "auth_required"
        assert auto.calls == 1
        assert (await asyncio.to_thread(store.load)) is None
    asyncio.run(_run())

def test_sso_only_never_allows_password_fallback(tmp_path):
    async def _run():
        auto = FakeAuto(recover=session())
        service = make_service(tmp_path, FakeClient(), auto=auto, sso_only=True)
        await service.my_todos()
        assert auto.allow_password is False
    asyncio.run(_run())

def test_concurrent_personal_reads_share_one_recovery(tmp_path):
    async def _run():
        def slow_recover():
            time.sleep(0.08)
            return session()

        auto = FakeAuto(recover=slow_recover)
        service = make_service(tmp_path, FakeClient(), auto=auto)
        first, second = await asyncio.gather(service.my_todos(), service.my_todos())
        assert first["status"] == second["status"] == "ok"
        assert auto.calls == 1
    asyncio.run(_run())

def test_cancellation_does_not_leak_auth_lock(tmp_path):
    async def _run():
        import threading

        release = threading.Event()

        def blocked_recover():
            release.wait(2)
            return session()

        auto = FakeAuto(recover=blocked_recover)
        service = make_service(tmp_path, FakeClient(), auto=auto)
        task = asyncio.create_task(service.my_todos())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.2)
        result = await asyncio.wait_for(service.my_todos(), timeout=3)
        assert result["status"] == "ok"

    asyncio.run(_run())


def test_check_uses_message_count_and_keeps_identity_false(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        service = make_service(tmp_path, FakeClient(), auto=FakeAuto())
        result = await service.check()
        assert result["status"] == "ok"
        assert result["verification"] == "message_count"
        assert result["unread_count"] == 0
        assert result["session"]["identity_verified"] is False

    asyncio.run(_run())


def test_status_never_claims_identity_and_clear_increments_generation(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        service = make_service(tmp_path, FakeClient(), auto=FakeAuto())
        status = await service.status()
        assert status["identity_verified"] is False
        assert status["session"]["identity_verified"] is False
        assert status["credential"]["configured"] is True
        cleared = await service.clear()
        assert cleared["cleared"] is True
        assert cleared["generation"] == 2
        assert (await service.status())["status"] == "auth_required"
    asyncio.run(_run())

def test_auto_auth_error_is_fixed_and_does_not_echo_upstream(tmp_path):
    async def _run():
        auto = FakeAuto(
            error=AutoAuthError(
                "credential_missing",
                "raw upstream text must not cross",
                action="credential_configure",
            )
        )
        service = make_service(tmp_path, FakeClient(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "auth_required"
        assert "raw upstream" not in str(exc.value)
    asyncio.run(_run())

