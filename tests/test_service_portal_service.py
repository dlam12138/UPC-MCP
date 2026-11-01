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
    def __init__(self, current=None, recover=None, error=None, binding_error=None):
        self.current = current or binding()
        self.recover_session = recover
        self.error = error
        self.binding_error = binding_error
        self.calls = 0
        self.allow_password = None
        self.credentials = self

    @contextmanager
    def guard(self):
        yield

    def snapshot_locked(self):
        return self.current

    def binding(self):
        if self.binding_error is not None:
            raise self.binding_error
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
            "empty_page": True,
            "has_more": False,
            "next_page": None,
            "pages_total": 1,
            "page_in_range": True,
            "complete_for_page": True,
            "complete_for_scope": True,
            "complete_for_scope_reason": "empty_scope",
            "remaining_after_page": 0,
            "items": [],
            "status_filter": 1,
            "page": 1,
        }
        self.app = app or {
            "total": 0,
            "count": 0,
            "empty": True,
            "empty_page": True,
            "has_more": False,
            "next_page": None,
            "pages_total": 1,
            "page_in_range": True,
            "complete_for_page": True,
            "complete_for_scope": True,
            "complete_for_scope_reason": "empty_scope",
            "remaining_after_page": 0,
            "items": [],
            "third_party_count": 0,
            "status_filter": 1,
            "page": 1,
        }
        self.catalog_rows = catalog or []
        self.fail_first = fail_first
        self.calls = []
        self.closed = 0
        self.catalog_error = None
        self.guide_error = None
        self.guide_result = None

    def search(self, *, keyword, page, page_size):
        raise AssertionError("the legacy server search endpoint must not be used")

    def catalog_search(self, *, keyword, page, page_size):
        self.calls.append(("catalog_search", keyword, page, page_size))
        if self.catalog_error is not None:
            raise self.catalog_error
        terms = (keyword or "").casefold().split()
        matched = []
        for row in self.catalog_rows:
            if row.get("folder"):
                continue
            haystack = " ".join(
                value for value in (row.get("name"), row.get("department")) if value
            ).casefold()
            if all(term in haystack for term in terms):
                matched.append(row)
        start = (page - 1) * page_size
        items = matched[start : start + page_size]
        pages_total = max(1, (len(matched) + page_size - 1) // page_size)
        page_in_range = page <= pages_total
        expected = min(page_size, max(0, len(matched) - start))
        return {
            "total": len(matched),
            "page": page,
            "page_size": page_size,
            "count": len(items),
            "returned": len(items),
            "empty": not matched,
            "empty_page": not items,
            "pages_total": pages_total,
            "page_in_range": page_in_range,
            "complete_for_page": page_in_range and len(items) == expected,
            "complete_for_scope": True,
            "complete_for_scope_reason": "full_catalog_fetched",
            "has_more": start + len(items) < len(matched),
            "next_page": page + 1 if start + len(items) < len(matched) else None,
            "remaining_after_page": max(0, len(matched) - start - len(items)),
            "items": items,
            "catalog": {
                "total": len(self.catalog_rows),
                "searchable_total": sum(
                    1 for row in self.catalog_rows if not row.get("folder")
                ),
                "folder_count": sum(
                    1 for row in self.catalog_rows if row.get("folder")
                ),
                "duplicate_id_count": 0,
                "duplicate_service_id_count": 0,
                "complete": True,
                "matched_total": len(matched),
            },
            "warnings": [],
        }

    def guide(self, service_id, *, department):
        self.calls.append(("guide", service_id, department))
        if self.guide_error is not None:
            raise self.guide_error
        if self.guide_result is not None:
            return self.guide_result
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

    def todos(self, account_hash, *, page=1, page_size=20):
        self.calls.append(("todos", account_hash, page, page_size))
        if self.fail_first:
            self.fail_first = False
            raise ServicePortalError("auth_required", "expired")
        return self.todo

    def applications(self, account_hash, *, page=1, page_size=20):
        self.calls.append(("applications", account_hash, page, page_size))
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

    def query(_account, *, page=1, page_size=20):
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
    with httpx.Client(cookies=jar, trust_env=False) as client:
        assert "synthetic=" in client.build_request(
            "GET", "https://service.upc.edu.cn/site/my-task/todo"
        ).headers["cookie"]
        assert "cookie" not in client.build_request(
            "GET", "http://service.upc.edu.cn/site/my-task/todo"
        ).headers
        assert "cookie" not in client.build_request(
            "GET", "https://service.upc.edu.cn/other/path"
        ).headers


def test_public_search_without_auto_auth_uses_full_public_catalog_and_no_login(tmp_path):
    async def _run():
        client = FakeClient(
            catalog=[
                {"id": 1, "name": "公开事项", "department": "部门"},
                {"id": 2, "name": "本科生请销假", "department": "学生工作部门"},
            ]
        )
        service = make_service(tmp_path, client, auto=None)
        search = await service.search(keyword=" 本科生 请销假 ", page=1, page_size=10)
        assert search["status"] == "ok"
        assert search["session_ref"] == "anonymous"
        assert search["query"] == {
            "keyword": "本科生 请销假",
            "page": 1,
            "page_size": 10,
        }
        assert search["total"] == 1
        assert search["items"][0]["name"] == "本科生请销假"
        assert search["public_catalog_only"] is True
        assert search["coverage"]["scope"] == "public_catalog"
        assert search["coverage"]["identity_visible_catalog"] is False
        assert search["coverage"]["identity_scope_complete"] is False
        assert search["next_action"] == "configure_account_or_restore_session"
        assert any(
            warning.startswith("public_catalog_only") for warning in search["warnings"]
        )
        assert client.calls[0] == ("catalog_search", "本科生 请销假", 1, 10)

        guide = await service.guide(1)
        assert guide["status"] == "ok"
        assert guide["department"] is None
        assert "department_not_cached" in guide["warnings"]
        assert guide["coverage"]["scope"] == "public_guide"
        assert guide["public_guide_only"] is True
    asyncio.run(_run())


def test_saved_account_search_uses_authenticated_catalog_scope(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        client = FakeClient(
            catalog=[
                {"id": 1, "name": "公开事项", "department": "部门"},
                {"id": 198, "name": "本科生请销假", "department": "学生工作部门"},
            ]
        )
        auto = FakeAuto()
        service = make_service(tmp_path, client, auto=auto)
        search = await service.search(keyword="本科生请销假")
        assert search["status"] == "ok"
        assert search["session_ref"] == "self"
        assert search["total"] == 1
        assert search["items"][0]["id"] == 198
        assert search["public_catalog_only"] is False
        assert search["coverage"]["scope"] == "authenticated_catalog"
        assert search["coverage"]["identity_visible_catalog"] is True
        assert search["coverage"]["identity_scope_complete"] is True
        assert search["coverage"]["all_identities"] is False
        assert search["coverage"]["catalog_total"] == 2
        assert "next_action" not in search
        assert client.calls[0] == ("catalog_search", "本科生请销假", 1, 20)
    asyncio.run(_run())


def test_authenticated_empty_match_is_not_reported_as_school_wide_absence(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        client = FakeClient(
            catalog=[{"id": 1, "name": "公开事项", "department": "部门"}]
        )
        service = make_service(tmp_path, client, auto=FakeAuto())
        search = await service.search(keyword="本科生请销假")
        assert search["total"] == 0
        assert search["public_catalog_only"] is False
        assert search["coverage"]["scope"] == "authenticated_catalog"
        assert any(
            warning.startswith("not_found_in_current_identity_catalog")
            for warning in search["warnings"]
        )
        assert "next_action" not in search
    asyncio.run(_run())


def test_public_search_survives_bad_credential_or_no_auto_auth(tmp_path):
    async def _run():
        client = FakeClient(
            catalog=[{"id": 1, "name": "公开事项", "department": "部门"}]
        )
        service = make_service(tmp_path, client, auto=None)
        assert (await service.search(keyword=None))["status"] == "ok"

        no_credential = FakeAuto(
            binding_error=AutoAuthError(
                "credential_missing",
                "raw credential text must not cross",
                action="credential_configure",
            )
        )
        service = make_service(tmp_path, client, auto=no_credential)
        fallback = await service.search(keyword="请销假")
        assert fallback["public_catalog_only"] is True
        assert fallback["next_action"] == "credential_configure"
        assert fallback["coverage"]["auth_error"] == "auth_required"
        assert "raw credential" not in str(fallback)

        bad_session = FakeAuto(
            error=ServicePortalError("auth_required", "expired"),
        )
        service = make_service(tmp_path, client, auto=bad_session)
        fallback = await service.search(keyword="请销假")
        assert fallback["public_catalog_only"] is True
        assert fallback["next_action"] == "service_portal_auto_auth"
        assert fallback["coverage"]["auth_error"] == "auth_required"

        rejected = FakeAuto(
            error=AutoAuthError(
                "cas_invalid_credentials",
                "raw credential text must not cross",
            )
        )
        service = make_service(tmp_path, client, auto=rejected)
        fallback = await service.search(keyword="请销假")
        assert fallback["public_catalog_only"] is True
        assert fallback["next_action"] == "credential_configure"
        assert fallback["coverage"]["auth_error"] == "auth_required"
        assert "raw credential" not in str(fallback)
    asyncio.run(_run())


def test_authenticated_upstream_or_schema_failure_is_not_hidden_by_public_fallback(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        for code in ("service_upstream_error", "schema_changed"):
            client = FakeClient()
            client.catalog_error = ServicePortalError(code, "synthetic failure")
            service = make_service(tmp_path, client, auto=FakeAuto())
            with pytest.raises(ServicePortalError) as caught:
                await service.search(keyword="请销假")
            assert caught.value.code == code
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
        assert client.calls == [("todos", "a" * 64, 1, 20)]
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
            def todos(self, account_hash, *, page=1, page_size=20):
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
            def todos(self, account_hash, *, page=1, page_size=20):
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
            def todos(self, account_hash, *, page=1, page_size=20):
                raise ServicePortalError("service_upstream_error", "503")

        service = make_service(tmp_path, Upstream(), auto=auto)
        with pytest.raises(ServicePortalError) as exc:
            await service.my_todos()
        assert exc.value.code == "service_upstream_error"
        assert auto.calls == 0

        class Schema(FakeClient):
            def todos(self, account_hash, *, page=1, page_size=20):
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

def test_search_auth_expiry_recovers_once_and_retries_once(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        auto = FakeAuto(recover=session())

        class ExpiringCatalog(FakeClient):
            def catalog_search(self, *, keyword, page, page_size):
                if self.fail_first:
                    self.fail_first = False
                    self.calls.append(("catalog_search", keyword, page, page_size))
                    raise ServicePortalError("auth_expired", "expired")
                return super().catalog_search(
                    keyword=keyword, page=page, page_size=page_size
                )

        client = ExpiringCatalog(
            catalog=[{"id": 198, "name": "本科生请销假", "department": "学生工作部门"}],
            fail_first=True,
        )
        service = make_service(tmp_path, client, auto=auto)
        result = await service.search(keyword="本科生请销假")
        assert result["status"] == "ok"
        assert result["session_ref"] == "self"
        assert result["total"] == 1
        assert auto.calls == 1
        assert [call[0] for call in client.calls] == [
            "catalog_search",
            "catalog_search",
        ]
    asyncio.run(_run())


def test_search_clear_during_catalog_read_discards_self_result(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        auto = FakeAuto(error=AssertionError("must not recover"))

        class ClearingCatalog(FakeClient):
            def catalog_search(self, *, keyword, page, page_size):
                if not self.calls:
                    self.calls.append(
                        ("catalog_search", keyword, page, page_size)
                    )
                    store.clear()
                    return {
                        "total": 1,
                        "items": [{"id": 198, "name": "本科生请销假"}],
                        "catalog": {"total": 1, "complete": True},
                        "warnings": [],
                    }
                return super().catalog_search(
                    keyword=keyword, page=page, page_size=page_size
                )

        service = make_service(
            tmp_path,
            ClearingCatalog(
                catalog=[{"id": 198, "name": "本科生请销假"}]
            ),
            auto=auto,
        )
        result = await service.search(keyword="本科生请销假")
        assert result["session_ref"] == "anonymous"
        assert result["public_catalog_only"] is True
        assert result["coverage"]["auth_error"] == "auth_required"
        assert auto.calls == 0
    asyncio.run(_run())


def _guide_router(anonymous, authenticated):
    def factory(cookies):
        return authenticated if cookies is not None else anonymous

    return factory


def test_login_only_guide_uses_same_authenticated_session(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        anonymous = FakeClient()
        anonymous.guide_error = ServicePortalError("auth_required", "login")
        authenticated = FakeClient()
        authenticated.guide_result = {
            "service_id": 198,
            "name": "本科生请销假",
            "department": "学生工作部门",
            "description": "登录后可见指南",
            "materials": [],
            "materials_status": "empty",
            "warnings": [],
        }
        service = ServicePortalService(
            store,
            client_factory=_guide_router(anonymous, authenticated),
            auto_auth=FakeAuto(),
        )
        result = await service.guide(198)
        assert result["status"] == "ok"
        assert result["session_ref"] == "self"
        assert result["description"] == "登录后可见指南"
        assert result["coverage"]["scope"] == "authenticated_guide"
        assert result["coverage"]["identity_visible_catalog"] is True
        assert result["coverage"]["identity_scope_complete"] is True
        assert result["public_guide_only"] is False
        assert "public_guide_only" not in " ".join(result["warnings"])
        assert authenticated.calls == [("guide", 198, None)]
        assert anonymous.calls == []
    asyncio.run(_run())


def test_public_guide_falls_back_anonymously_after_auth_failure(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        anonymous = FakeClient()
        anonymous.guide_result = {
            "service_id": 603,
            "name": "公开事项",
            "department": "部门",
            "description": "公开指南",
            "materials": [],
            "materials_status": "empty",
            "warnings": [],
        }
        authenticated = FakeClient()
        authenticated.guide_error = ServicePortalError(
            "auth_expired", "expired", action="service_portal_auto_auth"
        )
        service = ServicePortalService(
            store,
            client_factory=_guide_router(anonymous, authenticated),
            auto_auth=FakeAuto(recover=session()),
        )
        result = await service.guide(603)
        assert result["session_ref"] == "anonymous"
        assert result["public_guide_only"] is True
        assert result["coverage"]["scope"] == "public_guide"
        assert result["coverage"]["auth_error"] == "auth_expired"
        assert result["next_action"] == "service_portal_auto_auth"
        assert any(
            warning.startswith("public_guide_only") for warning in result["warnings"]
        )
        assert authenticated.calls == [("guide", 603, None), ("guide", 603, None)]
        assert anonymous.calls == [("guide", 603, None)]
    asyncio.run(_run())


def test_guide_upstream_failure_is_not_hidden_by_public_fallback(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        anonymous = FakeClient()
        authenticated = FakeClient()
        authenticated.guide_error = ServicePortalError(
            "service_upstream_error", "503"
        )
        service = ServicePortalService(
            store,
            client_factory=_guide_router(anonymous, authenticated),
            auto_auth=FakeAuto(),
        )
        with pytest.raises(ServicePortalError) as caught:
            await service.guide(603)
        assert caught.value.code == "service_upstream_error"
        assert anonymous.calls == []
    asyncio.run(_run())


def test_todo_and_application_multi_page_coverage_is_explicit(tmp_path):
    async def _run():
        store = ServicePortalStore(tmp_path)
        store.replace(session())
        client = FakeClient()
        client.todo = {
            "total": 45,
            "page": 2,
            "page_size": 20,
            "count": 20,
            "returned": 20,
            "empty": False,
            "empty_page": False,
            "pages_total": 3,
            "page_in_range": True,
            "complete_for_page": True,
            "complete_for_scope": False,
            "complete_for_scope_reason": "other_pages_not_included",
            "has_more": True,
            "next_page": 3,
            "remaining_after_page": 5,
            "items": [],
            "status_filter": 1,
        }
        client.app = {
            "total": 2,
            "page": 1,
            "page_size": 20,
            "count": 2,
            "returned": 2,
            "empty": False,
            "empty_page": False,
            "pages_total": 1,
            "page_in_range": True,
            "complete_for_page": True,
            "complete_for_scope": True,
            "complete_for_scope_reason": "single_page_covers_all",
            "has_more": False,
            "next_page": None,
            "remaining_after_page": 0,
            "items": [],
            "third_party_count": 0,
            "status_filter": 1,
        }
        service = make_service(tmp_path, client, auto=FakeAuto())
        todos = await service.my_todos(page=2, page_size=20)
        assert todos["coverage"]["scope"] == "pending_at_me"
        assert todos["coverage"]["complete_for_scope"] is False
        assert todos["coverage"]["has_more"] is True
        assert todos["coverage"]["next_page"] == 3
        assert todos["coverage"]["unread_messages_included"] is False
        assert any(
            warning.startswith("scope_not_fully_read") for warning in todos["warnings"]
        )
        apps = await service.my_applications(page=1, page_size=20)
        assert apps["coverage"]["scope"] == "my_applications_in_progress"
        assert apps["coverage"]["pending_at_me_queue"] is False
        assert apps["coverage"]["waiting_on_others_possible"] is True
        assert apps["coverage"]["complete_for_scope"] is True
        assert apps["coverage"]["third_party_records_marked"] is True
        assert client.calls == [
            ("todos", "a" * 64, 2, 20),
            ("applications", "a" * 64, 1, 20),
        ]
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

