"""Read-only timetable service: terms, one week, and local session lifecycle.

No write endpoint, no full-semester tool and no automatic retry/keepalive are
implemented.  A missing or corrupt local session affects only this service.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import ValidationError

from .auto_auth.errors import AutoAuthError
from .auto_auth.locks import FileLock
from .timetable_auth import adopt_from_cdp, browser_login
from .timetable_protocol import (
    TimetableClient,
    TimetableError,
    TimetableIndex,
    TimetableMeeting,
    TimetableTerm,
    TimetableWeek,
)
from .timetable_store import TimetableAuthLock, TimetableSession, TimetableStore

if TYPE_CHECKING:
    from .auto_auth.coordinator import AutoAuthCoordinator

T = TypeVar("T")
ACTIVE_STATES = {"stored", "unverified"}
# Reads may overlap, but stay bounded; a slow week must not serialize every
# other timetable request behind one global lock.  Four matches the other
# independent read adapters and leaves headroom for a slow week plus terms.
NETWORK_CONCURRENCY = 4
MAX_RANGE_DAYS = 14
MAX_RANGE_WEEKS = 3
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
ISO_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TimetableService:
    def __init__(
        self,
        store: TimetableStore,
        *,
        client_factory: Callable[[tuple], TimetableClient] | None = None,
        auth_lock_timeout: float = 30.0,
        auto_auth: AutoAuthCoordinator | None = None,
    ):
        self.store = store
        self.auto_auth = auto_auth
        self.client_factory = client_factory or (
            lambda cookies: TimetableClient(cookies)
        )
        self._auth_lock = asyncio.Lock()
        self._network_gate = asyncio.Semaphore(NETWORK_CONCURRENCY)
        self._auth_lock_handle = TimetableAuthLock(store.directory, timeout=auth_lock_timeout)

    @staticmethod
    def _base(status: str) -> dict:
        return {
            "status": status,
            "source_system": "timetable",
            "session_ref": "self",
            "fetched_at": now(),
            "warnings": [],
        }

    async def _snapshot(self) -> tuple[TimetableSession | None, int]:
        return await asyncio.to_thread(self.store.snapshot)

    @staticmethod
    def _require(
        session: TimetableSession | None, *, allow_unverified: bool = False
    ) -> TimetableSession:
        if session is None:
            raise TimetableError(
                "auth_required",
                "本机没有可用的课表业务会话，请先完成本地正常登录。",
                action="complete_official_login",
            )
        if session.state == "auth_expired":
            raise TimetableError(
                "auth_required",
                "课表业务会话已失效，请重新完成本地正常登录。",
                action="complete_official_login",
            )
        if session.state == "unverified" and not allow_unverified:
            raise TimetableError(
                "auth_required",
                "课表业务会话尚未验证，请先运行本地 check。",
                action="check_local_session",
            )
        if session.state not in ACTIVE_STATES:
            raise TimetableError("auth_required", "课表业务会话不可用。")
        return session

    def _client(self, session: TimetableSession) -> TimetableClient:
        return self.client_factory(tuple(session.cookies))

    @staticmethod
    def _auto_error(error: AutoAuthError) -> TimetableError:
        details = dict(error.details)
        if error.action:
            details["action"] = error.action
        return TimetableError(
            error.code,
            str(error),
            action=details.get("action"),
        )

    def _binding(self):
        if self.auto_auth is None:
            return None
        try:
            return self.auto_auth.binding()
        except AutoAuthError as error:
            raise self._auto_error(error) from None

    def _check_binding_sync(self, session: TimetableSession) -> None:
        if self.auto_auth is None:
            return
        binding = self.auto_auth.binding()
        if not binding.configured:
            if session.account_hash or session.credential_generation is not None:
                raise TimetableError(
                    "auth_required",
                    "统一凭据已清除；旧课表会话不再使用。",
                    action="credential_configure",
                )
            return
        if session.account_hash and session.account_hash != binding.account_hash:
            raise TimetableError("identity_mismatch", "既有业务会话属于另一账户；请先显式清除。")
        if (
            session.credential_generation != binding.generation
            or session.account_hash != binding.account_hash
        ):
            raise TimetableError(
                "auth_required",
                "课表会话未绑定当前统一凭据；将重新建立本人会话。",
                action="recover_with_unified_credential",
            )

    def _recover_auto_sync(self, binding, baseline, generation) -> TimetableSession:
        """Recover once against the snapshot captured before any network wait.

        The independent business lock includes persistence, so waiting services
        reuse the first caller's saved session rather than minting another one.
        Credential generation and business generation are separate CAS tokens.
        """
        assert self.auto_auth is not None
        try:
            with FileLock(
                self.store.directory / "timetable-auto-recovery.lock", timeout=45.0
            ).held():
                current_binding = self.auto_auth.binding()
                if not current_binding.matches(binding):
                    raise TimetableError("credential_changed", "认证等待期间统一凭据已变化。")
                current, current_generation = self.store.snapshot()
                original_revision = baseline.revision if baseline else None
                current_revision = current.revision if current else None
                if current_generation != generation or current_revision != original_revision:
                    if (
                        current is not None
                        and current.state == "stored"
                        and current.account_hash == binding.account_hash
                        and current.credential_generation == binding.generation
                        and current_generation == generation + 1
                        and current.source in {"auto_auth_sso", "auto_auth_password"}
                    ):
                        return current
                    raise TimetableError("profile_changed", "认证等待期间业务会话已清除或换绑。")
                if current is not None and (
                    current.account_hash is not None
                    and current.account_hash != binding.account_hash
                ):
                    raise TimetableError(
                        "identity_mismatch", "现有业务会话属于另一账户；请先显式清除。"
                    )
                recovered = self.auto_auth.recover_sync("timetable")
                if (
                    recovered.account_hash != binding.account_hash
                    or recovered.credential_generation != binding.generation
                ):
                    raise TimetableError("credential_changed", "恢复结果与当前凭据绑定不一致。")
                with self.auto_auth.credentials.guard():
                    current_binding = self.auto_auth.credentials.snapshot_locked()
                    if not current_binding.matches(binding):
                        raise TimetableError("credential_changed", "业务恢复完成前统一凭据已变化。")
                    self.store.replace(
                        recovered,
                        expected_revision=original_revision,
                        expected_generation=generation,
                    )
                return recovered
        except AutoAuthError as error:
            raise self._auto_error(error) from None

    def _finish_sync(
        self, session: TimetableSession, generation: int, binding
    ) -> None:
        if self.auto_auth is None or binding is None:
            self.store.finish(session.revision, generation, "stored")
            return
        with self.auto_auth.credentials.guard():
            current = self.auto_auth.credentials.snapshot_locked()
            if not current.matches(binding):
                raise TimetableError(
                    "credential_changed",
                    "读取完成后统一凭据已更换或清除；本次结果已丢弃。",
                )
            self.store.finish(session.revision, generation, "stored")

    async def _recover_auto(self, binding, baseline, generation) -> TimetableSession:
        return await asyncio.to_thread(
            self._recover_auto_sync, binding, baseline, generation
        )

    async def _run(
        self,
        operation: Callable[[TimetableSession, TimetableClient], Awaitable[T]],
        *,
        allow_unverified: bool = False,
    ) -> T:
        session, generation = await self._snapshot()
        binding = await asyncio.to_thread(self._binding)
        recovered = False
        try:
            session = self._require(session, allow_unverified=allow_unverified)
            await asyncio.to_thread(self._check_binding_sync, session)
        except TimetableError as error:
            if error.code != "auth_required" or binding is None or not binding.configured:
                raise
            session = await self._recover_auto(binding, session, generation)
            _current, generation = await self._snapshot()
            recovered = True
        try:
            async with self._network_gate:
                result = await operation(session, self._client(session))
        except TimetableError as error:
            if error.code not in {"auth_required", "auth_expired"} or recovered:
                raise
            await asyncio.to_thread(self.store.mark_expired, session.revision, generation)
            if binding is None or not binding.configured:
                raise
            session = await self._recover_auto(binding, session, generation)
            _current, generation = await self._snapshot()
            async with self._network_gate:
                result = await operation(session, self._client(session))
        await asyncio.to_thread(self._finish_sync, session, generation, binding)
        return result

    async def _auth_baseline(
        self, *, rebind: bool
    ) -> tuple[TimetableSession | None, int]:
        """Capture the binding before a browser wait or network verification.

        The returned generation is used by ``replace`` as a compare-and-swap
        token.  A clear or another process's rebind during the wait therefore
        invalidates this adoption instead of becoming a new baseline.
        """

        existing, generation = await self._snapshot()
        if existing is not None and not rebind and existing.state != "auth_expired":
            raise TimetableError(
                "already_configured",
                "本机已有课表会话；如需更换请显式使用 rebind。",
                action="use_rebind",
            )
        return existing, generation

    async def _acquire_auth_handle(self) -> Any:
        """Acquire the cross-process auth lock without leaking a late acquire.

        ``asyncio.to_thread`` cannot be cancelled.  If this coroutine is
        cancelled while the worker is still waiting, the worker may acquire the
        file lock later; the done callback releases exactly that late handle.
        """

        task = asyncio.create_task(
            asyncio.to_thread(self._auth_lock_handle.acquire)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            def _release_late(done: asyncio.Task) -> None:
                try:
                    late = done.result()
                except BaseException:
                    return
                late.release()

            task.add_done_callback(_release_late)
            raise

    async def status(self) -> dict:
        session = await asyncio.to_thread(self.store.load)
        binding = await asyncio.to_thread(self._binding)
        bound = True
        if session is not None and binding is not None:
            bound = bool(
                session.credential_generation == binding.generation
                and session.account_hash == binding.account_hash
            ) if binding.configured else not bool(
                session.account_hash or session.credential_generation is not None
            )
        if session is None:
            payload = self._base("auth_required")
        elif not bound:
            payload = self._base("auth_required")
        elif session.state == "auth_expired":
            payload = self._base("auth_expired")
        elif session.state == "unverified":
            payload = self._base("auth_required")
        else:
            payload = self._base("ok")
        payload["session"] = session.public() if session else None
        if binding is not None:
            payload["credential"] = {
                "configured": binding.configured,
                "generation": binding.generation,
                "account_ref": binding.account_ref(),
            }
        return payload

    async def check(self) -> dict:
        async def operation(
            _session: TimetableSession, client: TimetableClient
        ) -> TimetableIndex:
            return await client.get_index()

        index = await self._run(operation, allow_unverified=True)
        session = await asyncio.to_thread(self.store.load)
        payload = self._base("ok")
        payload.update(
            {
                "session": session.public() if session else None,
                "terms": len(index.terms),
                "current": _current_public(index),
            }
        )
        return payload

    async def terms(self) -> dict:
        async def operation(
            _session: TimetableSession, client: TimetableClient
        ) -> TimetableIndex:
            return await client.get_index()

        index = await self._run(operation)
        session = await asyncio.to_thread(self.store.load)
        payload = self._base("ok")
        payload.update(
            {
                "session": session.public() if session else None,
                "terms": [term.public() for term in index.terms],
                "current": _current_public(index),
            }
        )
        return payload

    async def week(self, year: str, term: str, week: int) -> dict:
        year = year.strip()
        term = term.strip()
        if not year or not term:
            raise TimetableError("invalid_input", "year 和 term 必须是学期列表中的非空值。")
        if not isinstance(week, int) or isinstance(week, bool):
            raise TimetableError("invalid_input", "week 必须是整数。")
        if not 1 <= week <= 30:
            raise TimetableError("invalid_input", "week 超出允许范围。")

        async def operation(
            _session: TimetableSession, client: TimetableClient
        ) -> tuple[TimetableIndex, TimetableTerm, TimetableWeek]:
            index = await client.get_index()
            selected = index.find(year, term)
            if selected is None:
                raise TimetableError("invalid_input", "指定学年/学期不在学校返回的学期列表中。")
            if not 1 <= week <= selected.total_weeks:
                raise TimetableError("invalid_input", "week 超出该学期总周数。")
            week_data = await client.get_week(selected, week)
            return index, selected, week_data

        index, selected, week_data = await self._run(operation)
        session = await asyncio.to_thread(self.store.load)
        dates = week_data.dates
        courses = [
            meeting.public(_date_for_weekday(dates, meeting.weekday))
            for meeting in week_data.meetings
        ]
        courses.sort(key=_course_sort_key)
        payload = self._base("empty" if week_data.empty else "ok")
        payload.update(
            {
                "session": session.public() if session else None,
                "term": selected.public(),
                "week": week,
                "week_start": dates[0].isoformat(),
                "week_end": dates[-1].isoformat(),
                "weekdays": [day.isoformat() for day in dates],
                "courses": courses,
                "counts": {
                    "total": len(courses),
                    "returned": len(courses),
                    "courses": len(courses),
                    "unique_courses": _unique_courses(week_data.meetings),
                    "periods": len(
                        {(m.start_period, m.end_period) for m in week_data.meetings}
                    ),
                    "complete": True,
                },
                "complete": True,
                "calendar": _calendar_public(),
                "current": _current_public(index),
            }
        )
        return payload

    async def range(
        self,
        year: str,
        term: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Read a bounded natural-date range across at most three teaching weeks."""
        year = year.strip()
        term = term.strip()
        if not year or not term:
            raise TimetableError("invalid_input", "year 和 term 必须是学期列表中的非空值。")
        start = _parse_iso_date(start_date, "start_date")
        end = _parse_iso_date(end_date, "end_date")
        if start > end:
            raise TimetableError("invalid_input", "start_date 不能晚于 end_date。")
        span_days = (end - start).days + 1
        if span_days > MAX_RANGE_DAYS:
            raise TimetableError(
                "invalid_input",
                f"日期范围最多 {MAX_RANGE_DAYS} 天；未按整学期或无限范围取数。",
            )

        async def operation(
            _session: TimetableSession, client: TimetableClient
        ) -> tuple[
            TimetableIndex,
            TimetableTerm,
            list[int],
            list[TimetableWeek],
        ]:
            index = await client.get_index()
            selected = index.find(year, term)
            if selected is None:
                raise TimetableError("invalid_input", "指定学年/学期不在学校返回的学期列表中。")
            term_start = selected.startday
            term_end = term_start + dt.timedelta(days=selected.total_weeks * 7 - 1)
            if start < term_start or end > term_end:
                raise TimetableError(
                    "invalid_input",
                    "日期范围超出该学期；未跨学期拼接或猜测。",
                )
            first_week = (start - term_start).days // 7 + 1
            last_week = (end - term_start).days // 7 + 1
            weeks = list(range(first_week, last_week + 1))
            if len(weeks) > MAX_RANGE_WEEKS:
                raise TimetableError(
                    "invalid_input",
                    f"日期范围最多覆盖 {MAX_RANGE_WEEKS} 个教学周。",
                )
            week_data = [await client.get_week(selected, week) for week in weeks]
            return index, selected, weeks, week_data

        index, selected, weeks, week_data = await self._run(operation)
        session = await asyncio.to_thread(self.store.load)
        courses = []
        week_details = []
        for week, data in zip(weeks, week_data, strict=True):
            week_courses = 0
            for meeting in data.meetings:
                day = _date_for_weekday(data.dates, meeting.weekday)
                if start <= day <= end:
                    courses.append(meeting.public(day))
                    week_courses += 1
            week_details.append(
                {
                    "week": week,
                    "start": data.dates[0].isoformat(),
                    "end": data.dates[-1].isoformat(),
                    "courses": week_courses,
                }
            )
        courses.sort(key=_course_sort_key)
        days = [
            _day_public(start + dt.timedelta(days=offset))
            for offset in range(span_days)
        ]
        payload = self._base("empty" if not courses else "ok")
        payload.update(
            {
                "session": session.public() if session else None,
                "term": selected.public(),
                "range": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "days": span_days,
                    "weeks": weeks,
                    "week_details": week_details,
                    "complete": True,
                },
                "weekdays": [day["date"] for day in days],
                "days": days,
                "courses": courses,
                "counts": {
                    "total": len(courses),
                    "returned": len(courses),
                    "courses": len(courses),
                    "unique_courses": len(
                        {
                            (course.get("course_id"), course.get("course_name"))
                            for course in courses
                        }
                    ),
                    "periods": len(
                        {
                            (course["start_period"], course["end_period"])
                            for course in courses
                        }
                    ),
                    "complete": True,
                },
                "complete": True,
                "calendar": _calendar_public(),
                "current": _current_public(index),
            }
        )
        return payload

    async def clear(self) -> dict:
        await asyncio.to_thread(self.store.clear)
        payload = self._base("ok")
        payload.update(
            {
                "local_session_removed": True,
                "remote_session_revoked": False,
                "session": None,
            }
        )
        return payload

    async def _save_verified(
        self,
        session: TimetableSession,
        *,
        existing: TimetableSession | None,
        generation: int,
    ) -> dict:
        client = self._client(session)
        async with self._network_gate:
            index = await client.get_index()
        session.state = "stored"
        session.verified_at = now()
        await asyncio.to_thread(
            self.store.replace,
            session,
            expected_revision=existing.revision if existing is not None else None,
            # Always CAS on the generation captured before the browser wait.
            # ``None`` here used to let a late adoption survive a concurrent
            # clear when no session existed at the start.
            expected_generation=generation,
        )
        payload = self._base("ok")
        payload.update(
            {
                "session": session.public(),
                "terms": len(index.terms),
                "current": _current_public(index),
            }
        )
        return payload

    async def login(
        self,
        profile_dir: Path,
        *,
        wait_seconds: float = 180.0,
        rebind: bool = False,
    ) -> dict:
        async with self._auth_lock:
            held = await self._acquire_auth_handle()
            try:
                existing, generation = await self._auth_baseline(rebind=rebind)
                session = await browser_login(profile_dir, wait_seconds=wait_seconds)
                return await self._save_verified(
                    session, existing=existing, generation=generation
                )
            finally:
                held.release()

    async def adopt_cdp(self, endpoint: str, *, rebind: bool = False) -> dict:
        async with self._auth_lock:
            held = await self._acquire_auth_handle()
            try:
                existing, generation = await self._auth_baseline(rebind=rebind)
                session = await adopt_from_cdp(endpoint)
                return await self._save_verified(
                    session, existing=existing, generation=generation
                )
            finally:
                held.release()

    async def adopt_candidate(self, path: Path, *, rebind: bool = False) -> dict:
        async with self._auth_lock:
            held = await self._acquire_auth_handle()
            try:
                existing, generation = await self._auth_baseline(rebind=rebind)
                cookies = await asyncio.to_thread(self.store.candidate, path)
                try:
                    session = TimetableSession(
                        cookies=cookies,
                        state="unverified",
                        source="adopted_candidate",
                    )
                except ValidationError:
                    raise TimetableError("invalid_input", "课表会话候选内容无效。") from None
                return await self._save_verified(
                    session, existing=existing, generation=generation
                )
            finally:
                held.release()


def _current_public(index: TimetableIndex) -> dict | None:
    term = index.current()
    if term is None and index.current_week is None:
        return None
    return {
        "year": index.current_year,
        "term": index.current_term,
        "week": index.current_week,
        "startday": term.startday.isoformat() if term else None,
        "weekdays": [day.isoformat() for day in index.current_weekdays]
        if index.current_weekdays
        else None,
        "weekday": index.current_weekday,
    }


def _unique_courses(meetings: tuple[TimetableMeeting, ...]) -> int:
    keys = {
        (
            meeting.course_id,
            meeting.course_name,
            meeting.teacher,
            meeting.location,
        )
        for meeting in meetings
    }
    return len(keys)


def _date_for_weekday(dates: tuple[dt.date, ...], weekday: int) -> dt.date:
    for day in dates:
        if day.isoweekday() == weekday:
            return day
    raise TimetableError(
        "schema_changed",
        "课程 ISO weekday 与本周日期不一致；拒绝猜测日期。",
    )


def _parse_iso_date(value: str, name: str) -> dt.date:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value.strip()):
        raise TimetableError("invalid_input", f"{name} 必须是显式 YYYY-MM-DD。")
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        raise TimetableError("invalid_input", f"{name} 不是有效公历日期。") from None


def _course_sort_key(course: dict) -> tuple:
    return (
        course.get("date") or "",
        course.get("start_period") or 0,
        course.get("end_period") or 0,
        course.get("course_name") or "",
        course.get("location") or "",
        course.get("teacher") or "",
        course.get("instance_id") or "",
    )


def _calendar_public() -> dict:
    return {
        "week_starts_on": "sunday",
        "weekday_numbering": "ISO-8601: Monday=1, Sunday=7",
        "teaching_day_numbering": "Sunday=1, Monday=2, Saturday=7",
        "date_source": "meeting ISO weekday matched to explicit weekdays dates",
    }


def _day_public(day: dt.date) -> dict:
    return {
        "date": day.isoformat(),
        "weekday": day.isoweekday(),
        "weekday_label": ISO_WEEKDAY_LABELS[day.isoweekday() - 1],
        "teaching_day_index": day.isoweekday() % 7 + 1,
    }
