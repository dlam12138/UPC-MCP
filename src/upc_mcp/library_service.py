"""Library seat query and one explicit booking write.

Reads are independent and hold no service-wide lock. The only cross-process
single-flight region is the single ``freeBook`` write, whose lock is held across
the real POST and is not released early when the awaiting coroutine is cancelled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from .auto_auth.errors import AutoAuthError
from .auto_auth.locks import FileLock
from .library_seat import (
    Building,
    LibraryError,
    LibrarySeatClient,
    LibraryUser,
    Reservation,
    identity_hash,
    minutes,
    overlaps,
    reservation_active,
    reservation_matches,
    safe_date,
    safe_id,
)
from .library_session import (
    LibraryBookingLock,
    LibrarySession,
    LibraryStore,
    now,
)

if TYPE_CHECKING:
    from .auto_auth.coordinator import AutoAuthCoordinator

T = TypeVar("T")
MAX_RECEIPT_BYTES = 256 * 1024
RECEIPT_STATUSES = {"in_flight", "unknown", "success"}
RECEIPT_FUTURE_SKEW_SECONDS = 300
RECENT_SUCCESS_SECONDS = 600
ACTIVE_STATES = {"stored", "unverified"}


class LibraryService:
    def __init__(
        self,
        store: LibraryStore,
        client: LibrarySeatClient | None = None,
        *,
        auth_lock_timeout: float = 30.0,
        booking_lock_timeout: float = 10.0,
        auto_auth: AutoAuthCoordinator | None = None,
    ):
        self.store = store
        self.client = client or LibrarySeatClient()
        self.auto_auth = auto_auth
        self.auth_lock_timeout = auth_lock_timeout
        self.booking_lock_timeout = booking_lock_timeout
        self._auth_lock = asyncio.Lock()
        self._booking_lock = asyncio.Lock()
        self.receipts_path = store.directory / "library-booking-receipts.json"

    @staticmethod
    def _base(status: str, *, query: dict | None = None) -> dict:
        return {
            "status": status,
            "source_system": "library",
            "session_ref": "self",
            "fetched_at": now(),
            "query": query or {},
            "warnings": [],
        }

    async def _snapshot(self) -> tuple[LibrarySession | None, int]:
        return await asyncio.to_thread(self.store.snapshot)

    @staticmethod
    def _require(session: LibrarySession | None) -> LibrarySession:
        if session is None:
            raise LibraryError("auth_required", "本机没有已采用的图书馆座位会话。")
        if session.state not in ACTIVE_STATES:
            raise LibraryError("auth_required", "图书馆座位会话已失效，请重新登录。")
        if not session.token.get_secret_value():
            raise LibraryError("auth_required", "本地图书馆座位会话缺少令牌，请重新登录。")
        return session

    async def _expire(self, session: LibrarySession) -> None:
        try:
            await asyncio.to_thread(self.store.mark_expired, session.revision)
        except LibraryError:
            return

    async def _finish(self, session: LibrarySession) -> None:
        await asyncio.to_thread(self.store.finish, session.revision)

    @staticmethod
    def _auto_error(error: AutoAuthError) -> LibraryError:
        details = dict(error.details)
        if error.action:
            details["action"] = error.action
        return LibraryError(error.code, str(error), details=details)

    def _binding(self):
        if self.auto_auth is None:
            return None
        try:
            return self.auto_auth.binding()
        except AutoAuthError as error:
            raise self._auto_error(error) from None

    def _check_binding_sync(self, session: LibrarySession) -> None:
        if self.auto_auth is None:
            return
        binding = self.auto_auth.binding()
        if not binding.configured:
            if session.account_hash or session.credential_generation is not None:
                raise LibraryError(
                    "auth_required",
                    "统一凭据已清除；旧图书馆会话不再使用。",
                    details={"action": "credential_configure"},
                )
            return
        if session.account_hash and session.account_hash != binding.account_hash:
            raise LibraryError("identity_mismatch", "既有业务会话属于另一账户；请先显式清除。")
        if (
            session.credential_generation != binding.generation
            or session.account_hash != binding.account_hash
        ):
            raise LibraryError(
                "auth_required",
                "图书馆会话未绑定当前统一凭据；将重新建立本人会话。",
                details={"action": "recover_with_unified_credential"},
            )

    def _recover_auto_sync(self, binding, baseline, generation) -> LibrarySession:
        """Recover once against the snapshot captured before any network wait.

        The independent business lock includes persistence, so waiting services
        reuse the first caller's saved session rather than minting another one.
        Credential generation and business generation are separate CAS tokens.
        """
        assert self.auto_auth is not None
        try:
            with FileLock(
                self.store.directory / "library-auto-recovery.lock", timeout=45.0
            ).held():
                current_binding = self.auto_auth.binding()
                if not current_binding.matches(binding):
                    raise LibraryError("credential_changed", "认证等待期间统一凭据已变化。")
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
                        and (
                            baseline is None
                            or current.effective_binding_id() == baseline.effective_binding_id()
                        )
                    ):
                        return current
                    raise LibraryError("profile_changed", "认证等待期间业务会话已清除或换绑。")
                if current is not None and (
                    current.account_hash is not None
                    and current.account_hash != binding.account_hash
                ):
                    raise LibraryError(
                        "identity_mismatch", "现有业务会话属于另一账户；请先显式清除。"
                    )
                recovered = self.auto_auth.recover_sync("library")
                if (
                    recovered.account_hash != binding.account_hash
                    or recovered.credential_generation != binding.generation
                ):
                    raise LibraryError("credential_changed", "恢复结果与当前凭据绑定不一致。")
                if (
                    baseline is not None and baseline.identity_hash
                    and baseline.identity_hash != recovered.identity_hash
                ):
                    raise LibraryError("identity_mismatch", "新图书馆会话与既有本人身份不一致。")
                if baseline is not None:
                    # Keep receipt keys stable across same-account renewal.
                    recovered.binding_id = baseline.effective_binding_id()
                with self.auto_auth.credentials.guard():
                    current_binding = self.auto_auth.credentials.snapshot_locked()
                    if not current_binding.matches(binding):
                        raise LibraryError("credential_changed", "业务恢复完成前统一凭据已变化。")
                    self.store.replace(
                        recovered,
                        expected_revision=original_revision,
                        expected_generation=generation,
                    )
                return recovered
        except AutoAuthError as error:
            raise self._auto_error(error) from None

    def _finish_binding_sync(self, session: LibrarySession, binding) -> None:
        if self.auto_auth is None or binding is None:
            self.store.finish(session.revision)
            return
        with self.auto_auth.credentials.guard():
            current = self.auto_auth.credentials.snapshot_locked()
            if not current.matches(binding):
                raise LibraryError(
                    "credential_changed",
                    "读取完成后统一凭据已更换或清除；本次结果已丢弃。",
                )
            self.store.finish(session.revision)

    async def _recover_auto(self, binding, baseline, generation) -> LibrarySession:
        return await asyncio.to_thread(
            self._recover_auto_sync, binding, baseline, generation
        )

    def _validate_before_write_sync(
        self, session: LibrarySession, generation: int, binding
    ) -> None:
        if self.auto_auth is None:
            current, current_generation = self.store.snapshot()
            if (
                current is None
                or current.revision != session.revision
                or current_generation != generation
                or current.effective_binding_id() != session.effective_binding_id()
            ):
                raise LibraryError(
                    "auth_required", "预约前会话已清除或换绑，未提交预约。"
                )
            return
        with self.auto_auth.credentials.guard():
            current_binding = self.auto_auth.credentials.snapshot_locked()
            if binding is not None and binding.configured and not current_binding.configured:
                raise LibraryError(
                    "credential_changed",
                    "预约前统一凭据已清除，未提交预约。",
                )
            if current_binding.configured and (
                binding is None
                or not current_binding.matches(binding)
                or session.credential_generation != current_binding.generation
                or session.account_hash != current_binding.account_hash
            ):
                raise LibraryError(
                    "credential_changed",
                    "预约前统一凭据已更换或清除，未提交预约。",
                )
            current, current_generation = self.store.snapshot()
            if (
                current is None
                or current.revision != session.revision
                or current_generation != generation
                or current.effective_binding_id() != session.effective_binding_id()
            ):
                raise LibraryError(
                    "auth_required", "预约前图书馆会话已清除或换绑，未提交预约。"
                )

    async def _read(
        self, operation: Callable[[LibrarySession, str], Awaitable[T]],
    ) -> T:
        session, generation = await self._snapshot()
        binding = await asyncio.to_thread(self._binding)
        recovered = False
        try:
            session = self._require(session)
            await asyncio.to_thread(self._check_binding_sync, session)
        except LibraryError as error:
            if error.code != "auth_required" or binding is None or not binding.configured:
                raise
            session = await self._recover_auto(binding, session, generation)
            recovered = True
        try:
            result = await operation(session, session.token.get_secret_value())
        except LibraryError as error:
            if error.code not in {"auth_expired", "auth_required"} or recovered:
                raise
            await self._expire(session)
            if binding is None or not binding.configured:
                raise
            session = await self._recover_auto(binding, session, generation)
            result = await operation(session, session.token.get_secret_value())
        await asyncio.to_thread(self._finish_binding_sync, session, binding)
        return result

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
        if session is None or not bound:
            payload = self._base("auth_required")
        elif session.state == "auth_expired":
            payload = self._base("auth_expired")
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
        async def operation(session: LibrarySession, token: str) -> dict:
            user = await self.client.user(token)
            self._check_identity(session, user)
            dates, buildings = await self.client.filters(token)
            return {"dates": dates, "buildings": buildings, "user": user}

        result = await self._read(operation)
        session = await asyncio.to_thread(self.store.load)
        payload = self._base("ok")
        payload.update(
            {
                "session": session.public() if session else None,
                "dates": len(result["dates"]),
                "buildings": len(result["buildings"]),
                "first_date": result["dates"][0] if result["dates"] else None,
                "last_date": result["dates"][-1] if result["dates"] else None,
            }
        )
        return payload

    async def filters(self) -> dict:
        warnings: list[str] = []

        async def operation(_session: LibrarySession, token: str) -> dict:
            dates, buildings = await self.client.filters(token)
            captcha: bool | None = None
            try:
                settings = await self.client.settings(token)
                captcha = settings.captcha_required
            except LibraryError as error:
                if error.code in {"auth_expired", "auth_required"}:
                    raise
                warnings.append("settings_unavailable")
            return {"dates": dates, "buildings": buildings, "captcha": captcha}

        result = await self._read(operation)
        status = "empty" if not result["dates"] or not result["buildings"] else "ok"
        payload = self._base(status)
        payload.update(
            {
                "dates": result["dates"],
                "buildings": [
                    {"building_id": item.id, "name": item.name}
                    for item in result["buildings"]
                ],
                "captcha_required": result["captcha"],
            }
        )
        payload["warnings"] = warnings
        return payload

    async def rooms(
        self,
        building_id: str,
        day: str,
        *,
        floor: int | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict:
        safe_id(building_id, "building_id")
        safe_date(day)
        if floor is not None and not 0 <= floor <= 100:
            raise LibraryError("invalid_input", "floor 必须是学校楼层号。")
        offset, limit = self._page(offset, limit, maximum=500)
        warnings: list[str] = []

        async def operation(_session: LibrarySession, token: str) -> dict:
            rooms = await self.client.rooms(token, building_id, day)
            floors = []
            try:
                floors = await self.client.floors(token, building_id)
            except LibraryError as error:
                if error.code in {"auth_expired", "auth_required"}:
                    raise
                warnings.append("floors_unavailable")
            return {"rooms": rooms, "floors": floors}

        result = await self._read(operation)
        floor_names = {item.floor_num: item.floor_name for item in result["floors"]}
        rooms = [
            {
                "room_id": item.room_id,
                "name": item.name,
                "floor": item.floor,
                "floor_name": floor_names.get(item.floor),
            }
            for item in result["rooms"]
            if floor is None or item.floor == floor
        ]
        page = rooms[offset : offset + limit]
        status = "empty" if not rooms else "ok"
        payload = self._base(
            status,
            query={
                "building_id": building_id,
                "date": day,
                "floor": floor,
                "offset": offset,
                "limit": limit,
            },
        )
        payload.update(
            {
                "floors": [
                    {"floor_num": item.floor_num, "floor_name": item.floor_name}
                    for item in result["floors"]
                ],
                "rooms": page,
                "total": len(rooms),
                "returned": len(page),
                "offset": offset,
                "limit": limit,
                "truncated": offset + len(page) < len(rooms),
            }
        )
        payload["warnings"] = warnings
        return payload

    async def seats(
        self,
        room_id: str,
        day: str,
        *,
        only_available: bool = False,
        offset: int = 0,
        limit: int = 300,
    ) -> dict:
        safe_id(room_id, "room_id")
        safe_date(day)
        offset, limit = self._page(offset, limit, maximum=1000)

        async def operation(_session: LibrarySession, token: str) -> Any:
            return await self.client.layout(token, room_id, day)

        layout = await self._read(operation)
        available_total = sum(1 for seat in layout.seats if seat.selectable)
        seats = [seat for seat in layout.seats if not only_available or seat.selectable]
        page = seats[offset : offset + limit]
        status = "empty" if not seats else "ok"
        payload = self._base(
            status,
            query={
                "room_id": room_id,
                "date": day,
                "only_available": only_available,
                "offset": offset,
                "limit": limit,
            },
        )
        payload.update(
            {
                "rows": layout.rows,
                "cols": layout.cols,
                "total": len(seats),
                "layout_total": len(layout.seats),
                "available_total": available_total,
                "returned": len(page),
                "offset": offset,
                "limit": limit,
                "truncated": offset + len(page) < len(seats),
                "seats": [seat.public() for seat in page],
            }
        )
        return payload

    async def times(
        self,
        seat_id: str,
        day: str,
        *,
        start_id: str | None = None,
    ) -> dict:
        safe_id(seat_id, "seat_id")
        safe_date(day)
        if start_id is not None:
            safe_id(start_id, "start_id")

        async def operation(_session: LibrarySession, token: str) -> dict:
            starts = await self.client.start_times(token, seat_id, day)
            selected = None
            ends = []
            if start_id is not None:
                selected = next((item for item in starts if item.id == start_id), None)
                if selected is None:
                    raise LibraryError("invalid_start", "开始时间不在学校返回的选项中。")
                ends = await self.client.end_times(token, seat_id, day, start_id)
            return {"starts": starts, "selected": selected, "ends": ends}

        result = await self._read(operation)
        payload = self._base(
            "empty" if not result["starts"] else "ok",
            query={"seat_id": seat_id, "date": day, "start_id": start_id},
        )
        payload.update(
            {
                "starts": [item.public() for item in result["starts"]],
                "start": result["selected"].public() if result["selected"] else None,
                "ends": [item.public() for item in result["ends"]],
            }
        )
        if result["selected"] is not None and not result["ends"]:
            payload["status"] = "empty"
        return payload

    async def reservations(self) -> dict:
        async def operation(_session: LibrarySession, token: str) -> Any:
            return await self.client.reservations(token)

        result = await self._read(operation)
        payload = self._base("empty" if not result.items else "ok")
        payload.update(
            {
                "empty_state": result.empty_state,
                "total": len(result.items),
                "reservations": [item.public() for item in result.items],
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
            }
        )
        return payload

    async def adopt_seat_token(
        self,
        token: str,
        *,
        source: str = "browser_seat_token",
        rebind: bool = False,
    ) -> dict:
        async with self._auth_lock:
            _existing, generation = await self._snapshot()
            user = await self.client.user(token)
            session = self._session_from_user(token, user, source)
            await self._save_adopted(
                session, rebind=rebind, expected_generation=generation
            )
            return self._adopted_payload(session, user)

    async def adopt_app_token(
        self,
        app_token: str,
        *,
        source: str = "browser_app_token",
        rebind: bool = False,
    ) -> dict:
        async with self._auth_lock:
            _existing, generation = await self._snapshot()
            seat_token = await self.client.exchange_app_token(app_token)
            user = await self.client.user(seat_token)
            session = self._session_from_user(seat_token, user, source)
            await self._save_adopted(
                session, rebind=rebind, expected_generation=generation
            )
            return self._adopted_payload(session, user)

    async def adopt_candidate(self, path: Path, *, rebind: bool = False) -> dict:
        kind, token = await asyncio.to_thread(LibraryStore.candidate, path)
        if kind == "app_token":
            return await self.adopt_app_token(
                token, source="adopted_candidate", rebind=rebind
            )
        return await self.adopt_seat_token(
            token, source="adopted_candidate", rebind=rebind
        )

    @staticmethod
    def _session_from_user(
        token: str, user: LibraryUser, source: str
    ) -> LibrarySession:
        digest = identity_hash(user)
        return LibrarySession(
            token=token,
            user_id=user.id,
            username=user.username,
            name=user.name,
            identity_hash=digest,
            identity_stable=True,
            binding_id=digest,
            state="stored",
            verified_at=now(),
            source=source,  # type: ignore[arg-type]
        )

    async def _save_adopted(
        self,
        session: LibrarySession,
        *,
        rebind: bool,
        expected_generation: int,
    ) -> None:
        existing, _generation = await self._snapshot()
        if (
            existing is not None
            and not rebind
            and existing.effective_binding_id() != session.effective_binding_id()
        ):
            raise LibraryError(
                "already_bound",
                "目标状态目录已有其他本人绑定；如确需更换本人，请显式使用 rebind。",
            )
        if existing is not None and not rebind:
            # Same identity may refresh its token; keep the stable binding.
            session.binding_id = existing.effective_binding_id()
        await asyncio.to_thread(
            self.store.replace,
            session,
            expected_generation=expected_generation,
        )

    def _adopted_payload(self, session: LibrarySession, user: LibraryUser) -> dict:
        payload = self._base("ok")
        payload.update(
            {
                "adopted": True,
                "session": session.public(),
                "identity_verified": bool(session.identity_hash and session.identity_stable),
                "user": user.public(),
            }
        )
        return payload

    @staticmethod
    def _check_identity(session: LibrarySession, user: LibraryUser) -> None:
        digest = identity_hash(user)
        if session.identity_hash and digest != session.identity_hash:
            raise LibraryError(
                "auth_required",
                "学校返回的本人标识与本地绑定不一致，已拒绝本次结果。",
            )

    @staticmethod
    def _page(offset: int, limit: int, *, maximum: int) -> tuple[int, int]:
        if not isinstance(offset, int) or offset < 0:
            raise LibraryError("invalid_input", "offset 必须是非负整数。")
        if not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise LibraryError("invalid_input", f"limit 必须在 1–{maximum}。")
        return offset, limit

    async def _room_identity(
        self,
        token: str,
        buildings: list[Building],
        day: str,
        room_id: str,
    ) -> tuple[str | None, bool]:
        """Resolve the selected room's name for location-text fallback.

        This is a bounded lookup for the chosen date, not a room/date scan. If
        any building lookup fails, or the name is not unique, text-only matching
        stays disabled. Exact reservation ``roomId``/``seatId`` do not need it.
        """
        names: list[str] = []
        matches = []
        complete = True
        for building in buildings:
            try:
                rooms = await self.client.rooms(token, building.id, day)
            except LibraryError as error:
                if error.code in {"auth_expired", "auth_required"}:
                    raise
                complete = False
                continue
            for room in rooms:
                if room.name:
                    names.append(room.name.strip().casefold())
                if room.room_id == room_id:
                    matches.append(room)
        if len(matches) != 1:
            return None, False
        name = matches[0].name.strip()
        unique = complete and names.count(name.casefold()) == 1
        return name, unique

    # ------------------------------------------------------------------
    # The only write path.
    # ------------------------------------------------------------------

    async def book(
        self,
        *,
        room_id: str,
        seat_id: str,
        day: str,
        start_id: str,
        end_id: str,
    ) -> dict:
        safe_id(room_id, "room_id")
        safe_id(seat_id, "seat_id")
        safe_date(day)
        safe_id(start_id, "start_id")
        safe_id(end_id, "end_id")
        async with self._booking_lock:
            acquire_task = asyncio.create_task(
                asyncio.to_thread(
                    LibraryBookingLock(
                        self.store.directory, self.booking_lock_timeout
                    ).acquire
                )
            )
            try:
                held = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                # The worker may acquire after this coroutine is cancelled.
                # Wait for that late acquisition, release it, and never start
                # the write path or the POST.
                try:
                    late_held = await acquire_task
                except Exception:
                    late_held = None
                if late_held is not None:
                    await asyncio.to_thread(late_held.release)
                raise
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                # Cancellation was requested in the small gap after acquire.
                # Do not start the write task or the POST.
                await asyncio.to_thread(held.release)
                raise asyncio.CancelledError
            task = asyncio.create_task(
                self._book_locked(
                    room_id=room_id,
                    seat_id=seat_id,
                    day=day,
                    start_id=start_id,
                    end_id=end_id,
                )
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # The real POST may still be running. Wait for the worker path
                # to finish before the cross-process booking lock is released.
                try:
                    await task
                finally:
                    raise
            finally:
                await asyncio.to_thread(held.release)

    async def _book_locked(
        self,
        *,
        room_id: str,
        seat_id: str,
        day: str,
        start_id: str,
        end_id: str,
    ) -> dict:
        baseline, generation = await self._snapshot()
        binding = await asyncio.to_thread(self._binding)
        state = {
            "receipt": False, "session": baseline,
            "generation": generation, "binding": binding,
        }
        for attempt in range(2):
            try:
                return await self._book_attempt(
                    room_id=room_id,
                    seat_id=seat_id,
                    day=day,
                    start_id=start_id,
                    end_id=end_id,
                    state=state,
                )
            except LibraryError as error:
                if (
                    error.code not in {"auth_expired", "auth_required"}
                    or state["receipt"]
                    or attempt == 1
                ):
                    raise
                if binding is None or not binding.configured:
                    raise
                state["session"] = await self._recover_auto(binding, baseline, generation)
                _current, state["generation"] = await self._snapshot()
        raise LibraryError("auth_required", "图书馆预约预检恢复失败；未提交预约。")

    async def _book_attempt(
        self,
        *,
        room_id: str,
        seat_id: str,
        day: str,
        start_id: str,
        end_id: str,
        state: dict,
    ) -> dict:
        query = {
            "room_id": room_id,
            "seat_id": seat_id,
            "date": day,
            "start_id": start_id,
            "end_id": end_id,
        }
        receipts = await asyncio.to_thread(self._read_receipts)
        session = self._require(state["session"])
        generation = state["generation"]
        binding = state["binding"]
        await asyncio.to_thread(self._check_binding_sync, session)
        key = self._receipt_key(session, query)
        receipt = receipts.get(key)
        if isinstance(receipt, dict):
            if receipt["status"] == "in_flight":
                return self._pending_payload(query, "previous_in_flight")
            if receipt["status"] == "unknown":
                return self._pending_payload(query, "previous_unknown")
            if (
                receipt["status"] == "success"
                and time.time() - float(receipt["updated_at"]) < RECENT_SUCCESS_SECONDS
            ):
                payload = self._base("already_booked", query=query)
                payload.update(
                    {
                        "submitted": False,
                        "verified": True,
                        "retry": False,
                        "retry_recommended": False,
                        "reservation_id": receipt.get("reservation_id"),
                        "message": "同一目标刚刚已预约成功；未重复提交。",
                    }
                )
                return payload

        token = session.token.get_secret_value()
        user = await self.client.user(token)
        self._check_identity(session, user)

        dates, buildings = await self.client.filters(token)
        if day not in dates:
            raise LibraryError("invalid_date", "所选日期不在学校当前可预约日期中。")

        layout = await self.client.layout(token, room_id, day)
        seat = next((item for item in layout.seats if item.id == seat_id), None)
        if seat is None:
            raise LibraryError("seat_not_available", "所选座位不在学校当前布局中。")
        if not seat.name:
            raise LibraryError(
                "seat_not_available",
                "学校未返回该座位的稳定名称，无法在提交后核对；未提交预约。",
                details={"reason": "seat_name_unavailable"},
            )
        seat_name_unique = (
            sum(
                1
                for item in layout.seats
                if item.name
                and item.name.strip().casefold() == seat.name.strip().casefold()
            )
            == 1
        )
        if not seat.selectable:
            raise LibraryError(
                "seat_not_available",
                "所选座位当前不可预约。",
                details={
                    "seat_status": seat.status,
                    "seat_type": seat.type,
                    "enabled": seat.enabled,
                    "local_only": seat.local_only,
                },
            )

        starts = await self.client.start_times(token, seat_id, day)
        start_option = next((item for item in starts if item.id == start_id), None)
        if start_option is None:
            raise LibraryError("invalid_start", "开始时间不在学校当前选项中。")
        if start_id == "now":
            raise LibraryError(
                "unsupported",
                "“现在”选项没有明确开始钟点，无法在提交后可靠核对；未提交预约。",
                details={"action": "choose_explicit_start"},
            )
        begin = start_option.value
        if minutes(begin) is None:
            raise LibraryError(
                "unsupported",
                "该开始时间选项没有明确钟点，无法在提交后可靠核对；未提交预约。",
                details={"action": "choose_explicit_start"},
            )
        ends = await self.client.end_times(token, seat_id, day, start_id)
        end_option = next((item for item in ends if item.id == end_id), None)
        if end_option is None:
            raise LibraryError("invalid_end", "结束时间不在学校当前选项中。")
        if minutes(end_option.value) is None:
            raise LibraryError(
                "unsupported",
                "该结束时间选项没有明确钟点，无法在提交后可靠核对；未提交预约。",
                details={"action": "choose_explicit_end"},
            )
        if minutes(end_option.value) <= minutes(begin):  # type: ignore[operator]
            raise LibraryError("invalid_input", "结束时间必须晚于开始时间。")

        room_name, room_name_unique = await self._room_identity(
            token, buildings, day, room_id
        )

        before = await self.client.reservations(token)
        pre_ids = {item.id for item in before.items}
        duplicate = next(
            (
                item
                for item in before.items
                if reservation_active(item.status) is True
                and reservation_matches(
                    item,
                    date=day,
                    begin=begin,
                    end=end_option.value,
                    seat_name=seat.name,
                    seat_id=seat_id,
                    room_id=room_id,
                    room_name=room_name,
                    room_name_unique=room_name_unique,
                    seat_name_unique=seat_name_unique,
                )[0]
            ),
            None,
        )
        if duplicate is not None:
            payload = self._base("already_booked", query=query)
            payload.update(
                {
                    "submitted": False,
                    "verified": True,
                    "retry": False,
                    "retry_recommended": False,
                    "reservation": duplicate.public(),
                    "message": "本人已有同一座位与时段的预约；未重复提交。",
                }
            )
            return payload
        for item in before.items:
            overlap = overlaps(
                item, date=day, begin=begin, end=end_option.value
            )
            if overlap is None:
                raise LibraryError(
                    "reservation_conflict",
                    "本人已有无法按时段判重的预约；为避免冲突，未提交预约。",
                )
            if overlap:
                raise LibraryError(
                    "reservation_conflict",
                    "本人已有与该时段重叠的预约；未提交预约。",
                )

        settings = await self.client.settings(token)
        if settings.captcha_required is True:
            raise LibraryError(
                "requires_user_action",
                "学校当前启用了图形验证码；本次未提交，请先在官方页面完成。",
                details={"action": "complete_captcha_in_official_page"},
            )
        if settings.captcha_required is None:
            raise LibraryError(
                "requires_user_action",
                "无法确认学校验证码设置；本次未提交。",
                details={"action": "verify_captcha_setting"},
            )

        await asyncio.to_thread(
            self._validate_before_write_sync, session, generation, binding
        )

        # Persist before the POST. A crash, timeout or cancellation after this
        # point can never cause a later call to silently submit the same target.
        await asyncio.to_thread(
            self._set_receipt,
            key,
            "in_flight",
            query,
            None,
        )
        state["receipt"] = True

        post_error: LibraryError | None = None
        response_ok = False
        try:
            await self.client.free_book(
                token,
                user=user,
                day=day,
                seat_id=seat_id,
                start_id=start_id,
                end_id=end_id,
            )
            response_ok = True
        except LibraryError as error:
            if error.code in {"auth_expired", "auth_required", "business_rejected"}:
                await self._clear_receipt_best_effort(key)
                raise
            if error.code == "upstream_error" and error.details.get("http_status") in {
                400,
                404,
                409,
                422,
            }:
                await self._clear_receipt_best_effort(key)
                raise
            post_error = error

        verified, verification_reason, matched = await self._verify_after_write(
            session,
            pre_ids,
            day,
            begin,
            end_option.value,
            seat.name,
            seat_id=seat_id,
            room_id=room_id,
            room_name=room_name,
            room_name_unique=room_name_unique,
            seat_name_unique=seat_name_unique,
        )
        if verified and matched is not None:
            try:
                await asyncio.to_thread(
                    self._set_receipt,
                    key,
                    "success",
                    query,
                    matched.id,
                )
            except Exception:
                # The in-flight receipt remains; a later call is safe.
                pass
            payload = self._base("booked", query=query)
            payload.update(
                {
                    "submitted": True,
                    "verified": True,
                    "retry": False,
                    "retry_recommended": False,
                    "reservation": matched.public(),
                    "message": "已提交且通过本人当前预约核对。",
                }
            )
            await self._finish_best_effort(session, payload)
            return payload

        if post_error is not None:
            reason = self._post_reason(post_error, False)
        else:
            reason = verification_reason or self._post_reason(None, response_ok)
        try:
            await asyncio.to_thread(self._set_receipt, key, "unknown", query, None)
        except Exception:
            pass
        payload = self._pending_payload(query, reason)
        payload["response_success"] = response_ok
        payload["verification"] = "not_confirmed"
        await self._finish_best_effort(session, payload)
        return payload

    async def _verify_after_write(
        self,
        session: LibrarySession,
        pre_ids: set[str],
        day: str,
        begin: str,
        end: str,
        seat_name: str | None,
        *,
        seat_id: str,
        room_id: str,
        room_name: str | None,
        room_name_unique: bool,
        seat_name_unique: bool,
    ) -> tuple[bool, str | None, Reservation | None]:
        token = session.token.get_secret_value()
        try:
            result = await self.client.reservations(token)
        except LibraryError as error:
            if error.code in {"auth_expired", "auth_required"}:
                return False, "verification_auth_expired", None
            return False, "verification_failed", None
        except Exception:
            return False, "verification_failed", None
        matches = [
            item
            for item in result.items
            if item.id not in pre_ids
            and reservation_matches(
                item,
                date=day,
                begin=begin,
                end=end,
                seat_name=seat_name,
                seat_id=seat_id,
                room_id=room_id,
                room_name=room_name,
                room_name_unique=room_name_unique,
                seat_name_unique=seat_name_unique,
            )[0]
        ]
        if not matches:
            return False, "verification_mismatch", None
        if len(matches) > 1:
            return False, "verification_ambiguous", None
        matched = matches[0]
        active = reservation_active(matched.status)
        if active is True:
            return True, None, matched
        if active is False:
            return False, "verification_inactive", None
        return False, "verification_status_unknown", None

    async def _finish_best_effort(self, session: LibrarySession, payload: dict) -> None:
        try:
            await self._finish(session)
        except Exception:
            payload.setdefault("warnings", []).append(
                "提交后本地会话已清除或换绑；远端预约结果仍以本次核对为准。"
            )

    async def _clear_receipt_best_effort(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._clear_receipt, key)
        except Exception:
            # Keeping the receipt is the safe direction: a later call will not
            # resubmit even if this rejected request is never cleared.
            return

    @staticmethod
    def _post_reason(error: LibraryError | None, response_ok: bool) -> str:
        if response_ok:
            return "verification_mismatch"
        if error is None:
            return "unknown"
        if error.details.get("timeout"):
            return "timeout"
        if error.details.get("http_status") is not None:
            return "http_error"
        if error.code == "schema_changed":
            return "invalid_response"
        if error.code == "upstream_unknown":
            return "unknown_business_code"
        if error.code == "tls_error":
            return "tls_error"
        return "network_error"

    def _pending_payload(self, query: dict, reason: str) -> dict:
        payload = self._base("pending_confirmation", query=query)
        payload.update(
            {
                "submitted": None,
                "verified": False,
                "retry": False,
                "retry_recommended": False,
                "verification_required": True,
                "reason": reason,
                "message": "预约结果未知或仍在提交；本次未重发，请先核对本人当前预约。",
            }
        )
        return payload

    # ------------------------------------------------------------------
    # Local receipts are read only by the write path.
    # ------------------------------------------------------------------

    @staticmethod
    def _receipt_key(session: LibrarySession, query: dict) -> str:
        raw = (
            f"{session.effective_binding_id()}:{query['date']}:{query['room_id']}:"
            f"{query['seat_id']}:{query['start_id']}:{query['end_id']}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _receipt_error(self, reason: str) -> LibraryError:
        return LibraryError(
            "local_error",
            "本地预约回执损坏或不可读；已拒绝提交并保留原文件。",
            details={
                "submitted": None,
                "retry": False,
                "verification_required": True,
                "reason": reason,
            },
        )

    def _read_receipts(self) -> dict:
        try:
            stat = self.receipts_path.stat()
        except FileNotFoundError:
            return {}
        except OSError:
            raise self._receipt_error("stat_failed") from None
        if stat.st_size > MAX_RECEIPT_BYTES:
            raise self._receipt_error("too_large")
        try:
            raw = self.receipts_path.read_bytes()
        except OSError:
            raise self._receipt_error("read_failed") from None
        if len(raw) > MAX_RECEIPT_BYTES:
            raise self._receipt_error("too_large")
        try:
            data = json.loads(
                raw.decode("utf-8"), parse_constant=self._reject_json_constant
            )
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise self._receipt_error("invalid_json") from None
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise self._receipt_error("invalid_version")
        records = data.get("records")
        if not isinstance(records, dict):
            raise self._receipt_error("invalid_records")
        now_value = time.time()
        for key, record in records.items():
            if not isinstance(key, str) or len(key) != 64:
                raise self._receipt_error("invalid_record_key")
            try:
                int(key, 16)
            except ValueError:
                raise self._receipt_error("invalid_record_key") from None
            if not isinstance(record, dict):
                raise self._receipt_error("invalid_record")
            if record.get("status") not in RECEIPT_STATUSES:
                raise self._receipt_error("invalid_status")
            updated_at = record.get("updated_at")
            if (
                isinstance(updated_at, bool)
                or not isinstance(updated_at, (int, float))
                or not math.isfinite(updated_at)
                or updated_at <= 0
                or updated_at > now_value + RECEIPT_FUTURE_SKEW_SECONDS
            ):
                raise self._receipt_error("invalid_updated_at")
            for field in ("date", "room_id", "seat_id", "start_id", "end_id"):
                value = record.get(field)
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise self._receipt_error("invalid_record_fields")
            reservation_id = record.get("reservation_id")
            if reservation_id is not None and (
                not isinstance(reservation_id, str) or not reservation_id
            ):
                raise self._receipt_error("invalid_reservation_id")
        return records

    def _write_receipts(self, records: dict) -> None:
        self.store.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=".library-receipts-", dir=self.store.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "records": records}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.receipts_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _set_receipt(
        self,
        key: str,
        status: str,
        query: dict,
        reservation_id: str | None,
    ) -> None:
        records = self._read_receipts()
        records[key] = {
            "status": status,
            "updated_at": time.time(),
            "date": query["date"],
            "room_id": query["room_id"],
            "seat_id": query["seat_id"],
            "start_id": query["start_id"],
            "end_id": query["end_id"],
            "reservation_id": reservation_id,
        }
        self._write_receipts(records)

    def _clear_receipt(self, key: str) -> None:
        records = self._read_receipts()
        if key in records:
            del records[key]
            self._write_receipts(records)
