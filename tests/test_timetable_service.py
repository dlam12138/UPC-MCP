from __future__ import annotations

import asyncio
import json
import threading
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from upc_mcp.timetable_protocol import (
    TimetableClient,
    TimetableCookie,
    TimetableError,
    TimetableTerm,
    TimetableWeek,
    parse_index,
    parse_week,
)
from upc_mcp.timetable_service import TimetableService
from upc_mcp.timetable_store import TimetableAuthLock, TimetableSession, TimetableStore

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def cookie() -> TimetableCookie:
    return TimetableCookie(
        name="eai-sess",
        value=SecretStr("synthetic-session"),
        domain="app.upc.edu.cn",
        path="/",
        secure=True,
        http_only=True,
        same_site="Lax",
    )


def seed(store: TimetableStore, *, state: str = "stored") -> TimetableSession:
    session = TimetableSession(cookies=[cookie()], state=state, source="adopted_candidate")
    store.replace(session)
    return session


def make_service(tmp_path, handler):
    store = TimetableStore(tmp_path)
    seed(store)
    return TimetableService(
        store,
        client_factory=lambda cookies: TimetableClient(
            cookies, transport=httpx.MockTransport(handler)
        ),
    )


def run(coro):
    return asyncio.run(coro)


def test_terms_and_week_payload(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_08.json"))

    service = make_service(tmp_path, handler)
    terms = run(service.terms())
    assert terms["status"] == "ok"
    assert len(terms["terms"]) == 2
    assert terms["current"]["week"] == 8
    week = run(service.week("2026-2027", "1", 8))
    assert week["status"] == "ok"
    assert week["term"]["startday"] == "2026-09-06"
    assert len(week["weekdays"]) == 7
    assert week["counts"]["courses"] == 6
    assert week["counts"]["unique_courses"] == 5
    assert week["courses"][0]["course_name"] == "示例课程日"
    assert week["session"]["identity_verified"] is False


def test_week_dates_follow_iso_weekday_not_sunday_array_index(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_08.json"))

    service = make_service(tmp_path, handler)
    week = run(service.week("2026-2027", "1", 8))
    weekdays = [date.fromisoformat(value) for value in week["weekdays"]]
    assert week["week_start"] == "2026-10-25"
    assert week["week_end"] == "2026-10-31"
    assert week["counts"]["total"] == week["counts"]["returned"] == 6
    assert week["counts"]["complete"] is True
    assert week["complete"] is True
    assert week["calendar"]["week_starts_on"] == "sunday"
    keys = []
    for course in week["courses"]:
        expected = next(
            day for day in weekdays if day.isoweekday() == course["weekday"]
        )
        assert course["date"] == expected.isoformat()
        assert course["weekday_label"] == {
            1: "周一",
            2: "周二",
            3: "周三",
            4: "周四",
            5: "周五",
            6: "周六",
            7: "周日",
        }[course["weekday"]]
        assert course["teaching_day_index"] == course["weekday"] % 7 + 1
        assert len(course["instance_id"]) == 16
        keys.append((course["date"], course["start_period"], course["end_period"]))
    assert keys == sorted(keys)


def test_range_crosses_weeks_and_returns_every_instance(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        week = int(parse_qs(request.content.decode())["week"][0])
        name = "timetable_week_08.json" if week == 8 else "timetable_week_09_empty.json"
        return httpx.Response(200, json=fixture(name))

    service = make_service(tmp_path, handler)
    payload = run(
        service.range(
            "2026-2027", "1", "2026-10-25", "2026-11-01"
        )
    )
    assert payload["status"] == "ok"
    assert payload["range"]["weeks"] == [8, 9]
    assert payload["range"]["complete"] is True
    assert payload["range"]["days"] == 8
    assert len(payload["days"]) == 8
    assert payload["courses"]
    assert payload["counts"]["total"] == payload["counts"]["returned"] == 6
    assert payload["counts"]["complete"] is True
    assert payload["complete"] is True
    assert len({course["instance_id"] for course in payload["courses"]}) == 6
    for course in payload["courses"]:
        assert "2026-10-25" <= course["date"] <= "2026-11-01"
        assert course["date"] == next(
            day["date"]
            for day in payload["days"]
            if date.fromisoformat(day["date"]).isoweekday() == course["weekday"]
        )


def test_range_empty_week_is_complete_empty(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_09_empty.json"))

    payload = run(
        make_service(tmp_path, handler).range(
            "2026-2027", "1", "2026-11-01", "2026-11-07"
        )
    )
    assert payload["status"] == "empty"
    assert payload["courses"] == []
    assert payload["counts"]["total"] == payload["counts"]["returned"] == 0
    assert payload["counts"]["complete"] is True
    assert payload["range"]["complete"] is True


def test_range_fails_when_required_week_fails(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        week = int(parse_qs(request.content.decode())["week"][0])
        if week == 8:
            return httpx.Response(500, text="upstream failure")
        return httpx.Response(200, json=fixture("timetable_week_09_empty.json"))

    service = make_service(tmp_path, handler)
    with pytest.raises(TimetableError) as caught:
        run(service.range("2026-2027", "1", "2026-10-25", "2026-11-01"))
    assert caught.value.code == "upstream_error"


def test_range_rejects_unbounded_or_invalid_dates(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=fixture("timetable_index.json"))

    service = make_service(tmp_path, handler)
    with pytest.raises(TimetableError) as caught:
        run(service.range("2026-2027", "1", "2026-10-25", "2026-11-08"))
    assert caught.value.code == "invalid_input"
    with pytest.raises(TimetableError) as caught:
        run(service.range("2026-2027", "1", "2026/10/25", "2026-10-31"))
    assert caught.value.code == "invalid_input"
    assert calls == []


def test_range_real_case_shape_returns_all_instances(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_10_case.json"))

    payload = run(
        make_service(tmp_path, handler).range(
            "2026-2027", "1", "2026-11-08", "2026-11-14"
        )
    )
    assert payload["status"] == "ok"
    assert payload["range"]["weeks"] == [10]
    assert payload["range"]["complete"] is True
    assert payload["counts"]["total"] == payload["counts"]["returned"] == 7
    assert payload["counts"]["unique_courses"] == 6
    assert [course["date"] for course in payload["courses"]] == [
        "2026-11-09",
        "2026-11-10",
        "2026-11-10",
        "2026-11-12",
        "2026-11-12",
        "2026-11-13",
        "2026-11-13",
    ]
    assert len({course["instance_id"] for course in payload["courses"]}) == 7
    repeated = [course for course in payload["courses"] if course["course_name"] == "合成课程乙"]
    assert len(repeated) == 2
    assert repeated[0]["date"] != repeated[1]["date"]
    assert repeated[0]["instance_id"] != repeated[1]["instance_id"]


def test_empty_week_is_empty_status(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_09_empty.json"))

    service = make_service(tmp_path, handler)
    payload = run(service.week("2026-2027", "1", 9))
    assert payload["status"] == "empty"
    assert payload["courses"] == []


def test_unknown_term_and_week_are_invalid_input(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=fixture("timetable_index.json"))

    service = make_service(tmp_path, handler)
    with pytest.raises(TimetableError) as caught:
        run(service.week("2099-2100", "1", 1))
    assert caught.value.code == "invalid_input"
    with pytest.raises(TimetableError) as caught:
        run(service.week("2026-2027", "1", 20))
    assert caught.value.code == "invalid_input"
    assert calls == ["GET", "GET"]


def test_wrong_week_dates_are_schema_changed_and_session_stays_stored(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_08.json"))

    service = make_service(tmp_path, handler)
    with pytest.raises(TimetableError) as caught:
        run(service.week("2026-2027", "1", 9))
    assert caught.value.code == "schema_changed"
    assert run(service.status())["session"]["state"] == "stored"


def test_10013_marks_expired(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"e": 10013, "m": "expired", "d": {}})

    service = make_service(tmp_path, handler)
    with pytest.raises(TimetableError) as caught:
        run(service.terms())
    assert caught.value.code == "auth_required"
    status = run(service.status())
    assert status["status"] == "auth_expired"
    assert status["session"]["state"] == "auth_expired"


def test_403_does_not_mark_expired(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    service = make_service(tmp_path, handler)
    with pytest.raises(TimetableError) as caught:
        run(service.terms())
    assert caught.value.code == "upstream_error"
    assert run(service.status())["session"]["state"] == "stored"


def test_clear_during_network_discards_result(tmp_path):
    index = parse_index(fixture("timetable_index.json"))
    week_payload = fixture("timetable_week_08.json")
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowClient:
        async def get_index(self):
            return index

        async def get_week(self, selected: TimetableTerm, week: int) -> TimetableWeek:
            started.set()
            await release.wait()
            return parse_week(week_payload, selected, week)

    store = TimetableStore(tmp_path)
    seed(store)
    service = TimetableService(store, client_factory=lambda _cookies: SlowClient())

    async def scenario():
        task = asyncio.create_task(service.week("2026-2027", "1", 8))
        await asyncio.wait_for(started.wait(), 2)
        await service.clear()
        release.set()
        with pytest.raises(TimetableError) as caught:
            await task
        return caught.value

    error = asyncio.run(scenario())
    assert error.code == "auth_required"
    assert run(service.status())["session"] is None


def test_no_session_is_auth_required_and_status_does_not_network(tmp_path):
    store = TimetableStore(tmp_path)
    service = TimetableService(store)
    assert run(service.status())["status"] == "auth_required"
    with pytest.raises(TimetableError) as caught:
        run(service.terms())
    assert caught.value.code == "auth_required"


def test_unverified_session_requires_check(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture("timetable_index.json"))

    store = TimetableStore(tmp_path)
    seed(store, state="unverified")
    service = TimetableService(
        store,
        client_factory=lambda cookies: TimetableClient(
            cookies, transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(TimetableError) as caught:
        run(service.terms())
    assert caught.value.code == "auth_required"
    checked = run(service.check())
    assert checked["status"] == "ok"
    assert checked["session"]["state"] == "stored"


def test_adopt_candidate_verifies_and_saves(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture("timetable_index.json"))

    store = TimetableStore(tmp_path)
    candidate = tmp_path / "candidate.dpapi"
    TimetableStore.write_candidate(candidate, [cookie()])
    service = TimetableService(
        store,
        client_factory=lambda cookies: TimetableClient(
            cookies, transport=httpx.MockTransport(handler)
        ),
    )
    payload = run(service.adopt_candidate(candidate))
    assert payload["status"] == "ok"
    assert payload["session"]["cookie_count"] == 1
    assert run(service.status())["status"] == "ok"


def test_clear_payload(tmp_path):
    store = TimetableStore(tmp_path)
    seed(store)
    service = TimetableService(store)
    payload = run(service.clear())
    assert payload["status"] == "ok"
    assert payload["local_session_removed"] is True
    assert payload["remote_session_revoked"] is False
    assert store.load() is None


def test_slow_week_does_not_block_terms_or_status(tmp_path):
    index = parse_index(fixture("timetable_index.json"))
    week_payload = fixture("timetable_week_08.json")
    started = asyncio.Event()
    release = asyncio.Event()

    class GateClient:
        async def get_index(self):
            return index

        async def get_week(self, selected: TimetableTerm, week: int) -> TimetableWeek:
            started.set()
            await release.wait()
            return parse_week(week_payload, selected, week)

    store = TimetableStore(tmp_path)
    seed(store)
    service = TimetableService(store, client_factory=lambda _cookies: GateClient())

    async def scenario():
        week_task = asyncio.create_task(service.week("2026-2027", "1", 8))
        await asyncio.wait_for(started.wait(), 2)
        # A slow week holds only one bounded read slot.  Terms and local status
        # must still complete without waiting for that week.
        terms = await asyncio.wait_for(service.terms(), 2)
        assert terms["status"] == "ok"
        status = await asyncio.wait_for(service.status(), 1)
        assert status["status"] == "ok"
        release.set()
        result = await asyncio.wait_for(week_task, 2)
        assert result["status"] == "ok"

    asyncio.run(scenario())


def test_status_without_session_does_not_construct_network_client(tmp_path):
    def factory(_cookies):
        raise AssertionError("status must not construct a network client")

    service = TimetableService(TimetableStore(tmp_path), client_factory=factory)
    assert run(service.status())["status"] == "auth_required"


def test_reads_and_clear_do_not_wait_for_auth_file_lock(tmp_path):
    index = parse_index(fixture("timetable_index.json"))

    class IndexClient:
        async def get_index(self):
            return index

    store = TimetableStore(tmp_path)
    seed(store)
    service = TimetableService(
        store, client_factory=lambda _cookies: IndexClient()
    )
    held = TimetableAuthLock(tmp_path).acquire()
    try:
        async def scenario():
            terms = await asyncio.wait_for(service.terms(), 2)
            assert terms["status"] == "ok"
            cleared = await asyncio.wait_for(service.clear(), 1)
            assert cleared["local_session_removed"] is True

        asyncio.run(scenario())
    finally:
        held.release()


def test_initial_adoption_fails_if_cleared_during_verification(tmp_path):
    index = parse_index(fixture("timetable_index.json"))
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowIndexClient:
        async def get_index(self):
            started.set()
            await release.wait()
            return index

    store = TimetableStore(tmp_path)
    candidate = tmp_path / "candidate.dpapi"
    TimetableStore.write_candidate(candidate, [cookie()])
    service = TimetableService(
        store, client_factory=lambda _cookies: SlowIndexClient()
    )

    async def scenario():
        task = asyncio.create_task(service.adopt_candidate(candidate))
        await asyncio.wait_for(started.wait(), 2)
        await service.clear()
        release.set()
        with pytest.raises(TimetableError) as caught:
            await task
        return caught.value

    error = asyncio.run(scenario())
    assert error.code == "auth_required"
    assert run(service.status())["session"] is None
    assert store.generation() == 1


def test_late_adoption_loses_to_another_process_rebind(tmp_path):
    index = parse_index(fixture("timetable_index.json"))
    week_payload = fixture("timetable_week_08.json")
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowIndexClient:
        async def get_index(self):
            started.set()
            await release.wait()
            return index

        async def get_week(self, selected: TimetableTerm, week: int) -> TimetableWeek:
            return parse_week(week_payload, selected, week)

    store = TimetableStore(tmp_path)
    original = seed(store)
    baseline_generation = store.generation()
    candidate = tmp_path / "candidate.dpapi"
    TimetableStore.write_candidate(candidate, [cookie()])
    service = TimetableService(
        store, client_factory=lambda _cookies: SlowIndexClient()
    )

    async def scenario():
        task = asyncio.create_task(service.adopt_candidate(candidate, rebind=True))
        await asyncio.wait_for(started.wait(), 2)
        replacement = TimetableSession(
            cookies=[
                TimetableCookie(
                    name="eai-sess",
                    value=SecretStr("replacement-session"),
                    domain="app.upc.edu.cn",
                    path="/",
                    secure=True,
                    http_only=True,
                    same_site="Lax",
                )
            ],
            state="stored",
            source="adopted_candidate",
        )
        other_process = TimetableStore(tmp_path)
        await asyncio.to_thread(
            other_process.replace,
            replacement,
            expected_revision=original.revision,
            expected_generation=baseline_generation,
        )
        release.set()
        with pytest.raises(TimetableError) as caught:
            await task
        return caught.value, replacement

    error, replacement = asyncio.run(scenario())
    assert error.code == "auth_required"
    loaded = run(service.status())
    assert loaded["session"] is not None
    assert loaded["session"]["state"] == "stored"
    current = store.load()
    assert current is not None
    assert current.revision == replacement.revision
    assert current.cookies[0].value.get_secret_value() == "replacement-session"


def test_clear_during_browser_wait_invalidates_login(tmp_path, monkeypatch):
    index = parse_index(fixture("timetable_index.json"))
    started = asyncio.Event()
    release = asyncio.Event()

    class IndexClient:
        async def get_index(self):
            return index

    async def fake_browser_login(profile_dir, *, wait_seconds):
        started.set()
        await release.wait()
        return TimetableSession(
            cookies=[cookie()],
            state="unverified",
            source="normal_browser_login",
        )

    monkeypatch.setattr(
        "upc_mcp.timetable_service.browser_login", fake_browser_login
    )
    store = TimetableStore(tmp_path)
    service = TimetableService(
        store, client_factory=lambda _cookies: IndexClient()
    )

    async def scenario():
        task = asyncio.create_task(service.login(tmp_path / "profile"))
        await asyncio.wait_for(started.wait(), 2)
        await service.clear()
        release.set()
        with pytest.raises(TimetableError) as caught:
            await task
        return caught.value

    error = asyncio.run(scenario())
    assert error.code == "auth_required"
    assert store.load() is None


def test_existing_session_requires_rebind_before_browser_wait(tmp_path, monkeypatch):
    called = False

    async def fake_browser_login(profile_dir, *, wait_seconds):
        nonlocal called
        called = True
        raise AssertionError("browser must not start")

    monkeypatch.setattr(
        "upc_mcp.timetable_service.browser_login", fake_browser_login
    )
    store = TimetableStore(tmp_path)
    seed(store)
    service = TimetableService(store)

    async def scenario():
        with pytest.raises(TimetableError) as caught:
            await service.login(tmp_path / "profile")
        return caught.value

    error = asyncio.run(scenario())
    assert error.code == "already_configured"
    assert called is False


def test_expired_session_can_be_refreshed_without_rebind(tmp_path, monkeypatch):
    index = parse_index(fixture("timetable_index.json"))
    store = TimetableStore(tmp_path)
    old = seed(store, state="auth_expired")

    class IndexClient:
        async def get_index(self):
            return index

    async def fake_browser_login(profile_dir, *, wait_seconds):
        return TimetableSession(
            cookies=[cookie()],
            state="unverified",
            source="normal_browser_login",
        )

    monkeypatch.setattr(
        "upc_mcp.timetable_service.browser_login", fake_browser_login
    )
    service = TimetableService(
        store, client_factory=lambda _cookies: IndexClient()
    )
    payload = asyncio.run(service.login(tmp_path / "profile"))
    assert payload["status"] == "ok"
    current = store.load()
    assert current is not None
    assert current.state == "stored"
    assert current.revision != old.revision


class _FakeHeldLock:
    def __init__(self, lock):
        self.lock = lock

    def release(self):
        self.lock.released.set()


class _BlockingAuthLock:
    def __init__(self):
        self.started = threading.Event()
        self.unblock = threading.Event()
        self.released = threading.Event()

    def acquire(self):
        self.started.set()
        self.unblock.wait(5)
        return _FakeHeldLock(self)


class _ImmediateAuthLock:
    def __init__(self):
        self.released = threading.Event()

    def acquire(self):
        return _FakeHeldLock(self)


def test_cancel_while_waiting_for_auth_lock_releases_late_handle(tmp_path, monkeypatch):
    service = TimetableService(TimetableStore(tmp_path))
    fake_lock = _BlockingAuthLock()
    service._auth_lock_handle = fake_lock

    async def unexpected_login(profile_dir, *, wait_seconds):
        raise AssertionError("login must not start before the auth lock")

    monkeypatch.setattr(
        "upc_mcp.timetable_service.browser_login", unexpected_login
    )

    async def scenario():
        task = asyncio.create_task(service.login(tmp_path / "profile"))
        assert await asyncio.to_thread(fake_lock.started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        fake_lock.unblock.set()
        for _ in range(100):
            if fake_lock.released.is_set():
                break
            await asyncio.sleep(0.02)
        assert fake_lock.released.is_set()

    asyncio.run(scenario())


def test_cancel_after_auth_lock_acquired_releases_it(tmp_path, monkeypatch):
    service = TimetableService(TimetableStore(tmp_path))
    fake_lock = _ImmediateAuthLock()
    service._auth_lock_handle = fake_lock
    entered = asyncio.Event()

    async def hanging_login(profile_dir, *, wait_seconds):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("upc_mcp.timetable_service.browser_login", hanging_login)

    async def scenario():
        task = asyncio.create_task(service.login(tmp_path / "profile"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_lock.released.is_set()

    asyncio.run(scenario())
