"""Async Shiguang read service with one conditional CAS SSO recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

import httpx

from .auto_auth.errors import AutoAuthError
from .auto_auth.locks import FileLock
from .shiguang import ShiguangClient, ShiguangError
from .shiguang_auth import ShiguangAuth
from .shiguang_session import (
    RecoveryLock,
    RegistrationLock,
    ShiguangSession,
    ShiguangStore,
    jar_from_cookies,
    now,
)

if TYPE_CHECKING:
    from .auto_auth.coordinator import AutoAuthCoordinator

T = TypeVar("T")
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_RECEIPT_BYTES = 256 * 1024
RECEIPT_STATUSES = {"in_flight", "unknown", "success"}
RECEIPT_FUTURE_SKEW_SECONDS = 300


class ShiguangRecovery:
    """Directory-wide single-flight CAS recovery with short failure backoff."""

    def __init__(
        self,
        store: ShiguangStore,
        auth: ShiguangAuth,
        wait_timeout: float = 35.0,
        backoff_seconds: float = 15.0,
    ):
        self.store = store
        self.auth = auth
        self.wait_timeout = wait_timeout
        self.backoff_seconds = backoff_seconds

    def recover_or_reuse(
        self,
        session: ShiguangSession,
        generation: int,
    ) -> tuple[ShiguangSession, bool]:
        if self.store.is_backoff_active():
            raise ShiguangError(
                "recovery_backoff",
                "石光恢复刚刚失败，短暂退避中；未发起新的恢复。",
            )
        with RecoveryLock(self.store.directory, self.wait_timeout).held():
            current, current_generation = self.store.snapshot()
            if current is None:
                raise ShiguangError("auth_required", "石光会话已清除，旧恢复操作已丢弃。")
            if current.effective_binding_id() != session.effective_binding_id():
                raise ShiguangError("auth_required", "石光绑定已更换，旧恢复操作已丢弃。")
            if current.revision != session.revision or current_generation != generation:
                return current, True
            if self.store.is_backoff_active():
                raise ShiguangError(
                    "recovery_backoff",
                    "石光恢复刚刚失败，短暂退避中；未发起新的恢复。",
                )
            try:
                recovered = self.auth.recover(current)
            except ShiguangError:
                self.store.set_backoff(self.backoff_seconds)
                raise
            self.store.replace(
                recovered,
                expected_revision=current.revision,
                expected_generation=current_generation,
            )
            return recovered, False


class ShiguangService:
    def __init__(
        self,
        store: ShiguangStore,
        auth: ShiguangAuth | None = None,
        timeout: httpx.Timeout | None = None,
        recovery: ShiguangRecovery | None = None,
        registration_lock_timeout: float = 10.0,
        auto_auth: AutoAuthCoordinator | None = None,
    ):
        self.store = store
        self.auth = auth or ShiguangAuth()
        self.timeout = timeout or TIMEOUT
        self.auto_auth = auto_auth
        # Reads are intentionally lock-free at the service level and rely on
        # snapshot/revision plus the cross-process recovery lock. Registration
        # uses a separate async lock; the file lock itself is acquired in a
        # worker thread so waiting never blocks the event loop.
        self._write_lock = asyncio.Lock()
        self.recovery = recovery or ShiguangRecovery(self.store, self.auth)
        self.registration_lock_timeout = registration_lock_timeout
        self.receipts_path = self.store.directory / "shiguang-registration-receipts.json"

    def _client(self, session: ShiguangSession) -> httpx.Client:
        return httpx.Client(
            cookies=jar_from_cookies(session.cookies),
            follow_redirects=False,
            timeout=self.timeout,
            trust_env=False,
        )

    def _execute(
        self,
        session: ShiguangSession,
        operation: Callable[[ShiguangClient], T],
    ) -> T:
        with self._client(session) as client:
            return operation(ShiguangClient(client, timeout=self.timeout))

    @staticmethod
    def _auto_error(error: AutoAuthError) -> ShiguangError:
        details = dict(error.details)
        if error.action:
            details["action"] = error.action
        return ShiguangError(error.code, str(error), details)

    def _binding(self):
        if self.auto_auth is None:
            return None
        try:
            return self.auto_auth.binding()
        except AutoAuthError as error:
            raise self._auto_error(error) from None

    def _check_binding_sync(self, session: ShiguangSession) -> None:
        if self.auto_auth is None:
            return
        binding = self.auto_auth.binding()
        if not binding.configured:
            if session.account_hash or session.credential_generation is not None:
                raise ShiguangError(
                    "auth_required",
                    "统一凭据已清除；旧石光会话不再使用。",
                    details={"action": "credential_configure"},
                )
            return
        if session.account_hash and session.account_hash != binding.account_hash:
            raise ShiguangError("identity_mismatch", "既有业务会话属于另一账户；请先显式清除。")
        if (
            session.credential_generation != binding.generation
            or session.account_hash != binding.account_hash
        ):
            raise ShiguangError(
                "auth_required",
                "石光会话未绑定当前统一凭据；将重新建立本人会话。",
                details={"action": "recover_with_unified_credential"},
            )

    def _recover_auto_sync(self, binding, baseline, generation) -> ShiguangSession:
        """Recover once against the snapshot captured before any network wait.

        The independent business lock includes persistence, so waiting services
        reuse the first caller's saved session rather than minting another one.
        Credential generation and business generation are separate CAS tokens.
        """
        assert self.auto_auth is not None
        try:
            with FileLock(
                self.store.directory / "shiguang-auto-recovery.lock", timeout=45.0
            ).held():
                current_binding = self.auto_auth.binding()
                if not current_binding.matches(binding):
                    raise ShiguangError("credential_changed", "认证等待期间统一凭据已变化。")
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
                    raise ShiguangError("profile_changed", "认证等待期间业务会话已清除或换绑。")
                if current is not None and (
                    current.account_hash is not None
                    and current.account_hash != binding.account_hash
                ):
                    raise ShiguangError(
                        "identity_mismatch", "现有业务会话属于另一账户；请先显式清除。"
                    )
                recovered = self.auto_auth.recover_sync("shiguang")
                if (
                    recovered.account_hash != binding.account_hash
                    or recovered.credential_generation != binding.generation
                ):
                    raise ShiguangError("credential_changed", "恢复结果与当前凭据绑定不一致。")
                if baseline is not None:
                    # Keep receipt keys stable across same-account renewal.
                    recovered.binding_id = baseline.effective_binding_id()
                with self.auto_auth.credentials.guard():
                    current_binding = self.auto_auth.credentials.snapshot_locked()
                    if not current_binding.matches(binding):
                        raise ShiguangError("credential_changed", "业务恢复完成前统一凭据已变化。")
                    self.store.replace(
                        recovered,
                        expected_revision=original_revision,
                        expected_generation=generation,
                    )
                return recovered
        except AutoAuthError as error:
            raise self._auto_error(error) from None

    def _finish_sync(self, session: ShiguangSession, binding) -> None:
        if self.auto_auth is None or binding is None:
            self.store.finish(session.revision)
            return
        with self.auto_auth.credentials.guard():
            current = self.auto_auth.credentials.snapshot_locked()
            if not current.matches(binding):
                raise ShiguangError(
                    "credential_changed",
                    "读取完成后统一凭据已更换或清除；本次结果已丢弃。",
                )
            self.store.finish(session.revision)

    def _validate_before_write_sync(
        self, session: ShiguangSession, generation: int
    ) -> None:
        if self.auto_auth is None:
            current, current_generation = self.store.snapshot()
            if (
                current is None
                or current.revision != session.revision
                or current_generation != generation
                or current.effective_binding_id() != session.effective_binding_id()
            ):
                raise ShiguangError(
                    "auth_required", "报名前会话已清除或换绑，未提交报名。"
                )
            return
        with self.auto_auth.credentials.guard():
            binding = self.auto_auth.credentials.snapshot_locked()
            if not binding.configured and (
                session.account_hash or session.credential_generation is not None
            ):
                raise ShiguangError(
                    "credential_changed",
                    "报名前统一凭据已清除，未提交报名。",
                )
            if binding.configured and (
                session.credential_generation != binding.generation
                or session.account_hash != binding.account_hash
            ):
                raise ShiguangError(
                    "credential_changed",
                    "报名前统一凭据已更换或清除，未提交报名。",
                )
            current, current_generation = self.store.snapshot()
            if (
                current is None
                or current.revision != session.revision
                or current_generation != generation
                or current.effective_binding_id() != session.effective_binding_id()
            ):
                raise ShiguangError(
                    "auth_required", "报名前石光会话已清除或换绑，未提交报名。"
                )
    async def _recover_auto(self, binding, baseline, generation) -> ShiguangSession:
        return await asyncio.to_thread(
            self._recover_auto_sync, binding, baseline, generation
        )

    async def _read(self, operation: Callable[[ShiguangClient], T]) -> T:
        session, generation = await asyncio.to_thread(self.store.snapshot)
        binding = await asyncio.to_thread(self._binding)
        recovered = False
        try:
            if session is None or (
                session.state == "auth_expired" and binding is not None and binding.configured
            ):
                raise ShiguangError("auth_required", "本机没有可用石光会话。")
            await asyncio.to_thread(self._check_binding_sync, session)
        except ShiguangError as error:
            if error.code != "auth_required" or binding is None or not binding.configured:
                raise
            session = await self._recover_auto(binding, session, generation)
            _current, generation = await asyncio.to_thread(self.store.snapshot)
            recovered = True
        try:
            result = await asyncio.to_thread(self._execute, session, operation)
        except ShiguangError as error:
            if error.code not in {"auth_required", "session_expired"} or recovered:
                raise
            if binding is not None and binding.configured:
                session = await self._recover_auto(binding, session, generation)
            elif session.has_cas_sso():
                session, _reused = await asyncio.to_thread(
                    self.recovery.recover_or_reuse, session, generation
                )
            else:
                raise
            result = await asyncio.to_thread(self._execute, session, operation)
        await asyncio.to_thread(self._finish_sync, session, binding)
        return result

    async def activities(
        self,
        page: int = 1,
        keyword: str | None = None,
        online_type: str | None = None,
        base_type: str | None = None,
    ) -> dict:
        return await self._read(
            lambda client: client.activities(
                page=page,
                keyword=keyword,
                online_type=online_type,
                base_type=base_type,
            )
        )

    async def activity_detail(self, activity_id: str, kind: str = "activity") -> dict:
        return await self._read(lambda client: client.activity_detail(activity_id, kind))

    async def my_participations(self, page: int = 1) -> dict:
        return await self._read(lambda client: client.my_participations(page))

    async def credits(self) -> dict:
        return await self._read(lambda client: client.credits())

    async def report_card(self) -> dict:
        return await self._read(lambda client: client.report_card())

    async def register_activity(
        self,
        activity_id: str,
        kind: str,
        invite_code: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Submit one explicit registration; never retries the write itself.

        The complete lock-and-write section runs in a worker thread. Cancelling
        the awaiting coroutine therefore cannot release the file lock while the
        real POST is still running.
        """
        async with self._write_lock:
            return await asyncio.to_thread(
                self._register_sync,
                activity_id,
                kind,
                invite_code,
                reason,
            )

    def _receipt_key(
        self, session: ShiguangSession, activity_id: str, kind: str
    ) -> str:
        raw = f"{session.effective_binding_id()}:{kind}:{activity_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _receipt_error(self, reason: str) -> ShiguangError:
        return ShiguangError(
            "local_error",
            "本地报名回执损坏或不可读；已拒绝提交并保留原文件，请人工核对。",
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
            # Only genuine absence is allowed to mean "no receipt".
            return {}
        except OSError:
            raise self._receipt_error("stat_failed") from None
        if stat.st_size > MAX_RECEIPT_BYTES:
            raise self._receipt_error("too_large")
        try:
            with self.receipts_path.open("rb") as receipt_file:
                raw = receipt_file.read(MAX_RECEIPT_BYTES + 1)
        except OSError:
            raise self._receipt_error("read_failed") from None
        if len(raw) > MAX_RECEIPT_BYTES:
            raise self._receipt_error("too_large")
        try:
            text = raw.decode("utf-8")
            data = json.loads(text, parse_constant=self._reject_json_constant)
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
            if not isinstance(key, str) or not key or len(key) > 128:
                raise self._receipt_error("invalid_record_key")
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
            activity_id = record.get("activity_id")
            kind = record.get("kind")
            if (
                not isinstance(activity_id, str)
                or not activity_id
                or len(activity_id) > 128
                or kind not in {"activity", "social_practice"}
            ):
                raise self._receipt_error("invalid_record_fields")
        return records

    def _write_receipts(self, records: dict) -> None:
        self.store.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=".shiguang-receipts-", dir=self.store.directory
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
        self, key: str, status: str, activity_id: str, kind: str
    ) -> None:
        records = self._read_receipts()
        records[key] = {
            "status": status,
            "updated_at": time.time(),
            "activity_id": activity_id,
            "kind": kind,
        }
        self._write_receipts(records)

    def _clear_receipt(self, key: str) -> None:
        records = self._read_receipts()
        if key in records:
            del records[key]
            self._write_receipts(records)

    def _recent_registration_payload(
        self, activity_id: str, kind: str, invite_code: bool
    ) -> dict:
        return {
            "status": "registration_recently_submitted",
            "source_system": "shiguang",
            "activity_id": activity_id,
            "kind": kind,
            "activity_title": None,
            "invite_code_used": invite_code,
            "submitted": True,
            "retry": False,
            "retry_recommended": False,
            "server_oper": None,
            "message": "该项目刚刚成功提交过；未重复提交。",
            "registration": None,
            "warnings": [],
        }

    def _unknown_registration_payload(
        self, activity_id: str, kind: str, invite_code: bool
    ) -> dict:
        return {
            "status": "submission_unknown",
            "source_system": "shiguang",
            "activity_id": activity_id,
            "kind": kind,
            "activity_title": None,
            "invite_code_used": invite_code,
            "submitted": None,
            "retry": False,
            "retry_recommended": False,
            "server_oper": None,
            "verification_required": True,
            "message": "上次报名结果未知或仍在提交；本次未重发，请先核对本人参与记录。",
            "registration": None,
            "warnings": [],
        }

    def _register_sync(
        self,
        activity_id: str,
        kind: str,
        invite_code: str | None,
        reason: str | None,
    ) -> dict:
        with RegistrationLock(
            self.store.directory, self.registration_lock_timeout
        ).held():
            session, generation = self.store.snapshot()
            binding = self._binding()
            recovered = False
            try:
                if session is None or (
                    session.state == "auth_expired" and binding is not None and binding.configured
                ):
                    raise ShiguangError("auth_required", "本机没有可用石光会话。")
                self._check_binding_sync(session)
            except ShiguangError as error:
                if error.code != "auth_required" or binding is None or not binding.configured:
                    raise
                session = self._recover_auto_sync(binding, session, generation)
                _current, generation = self.store.snapshot()
                recovered = True
            key = self._receipt_key(session, activity_id, kind)
            receipt = self._read_receipts().get(key)
            if isinstance(receipt, dict):
                if receipt.get("status") in {"in_flight", "unknown"}:
                    return self._unknown_registration_payload(
                        activity_id, kind, invite_code is not None
                    )
                if (
                    receipt.get("status") == "success"
                    and time.time() - float(receipt.get("updated_at", 0)) < 60
                ):
                    return self._recent_registration_payload(
                        activity_id, kind, invite_code is not None
                    )
            preview, session, generation = self._preview_registration_sync(
                session, generation, activity_id, kind, recovered=recovered, binding=binding
            )
            registration = preview["registration"]
            if registration["status"] != "open":
                code = {
                    "not_started": "registration_not_started",
                    "closed": "registration_closed",
                    "activity_in_progress": "registration_closed",
                    "finished": "activity_finished",
                    "unavailable": "registration_unavailable",
                }.get(registration["status"], "registration_unavailable")
                raise ShiguangError(
                    code,
                    "当前详情页状态不允许报名，未提交报名。",
                    details={
                        "activity_id": activity_id,
                        "kind": kind,
                        "registration": registration,
                    },
                )
            if invite_code is not None and not registration["invite_code_supported"]:
                raise ShiguangError(
                    "registration_unavailable",
                    "详情页没有邀请码报名入口，未提交报名。",
                    details={"activity_id": activity_id, "kind": kind},
                )
            if invite_code is None and not registration["normal_supported"]:
                raise ShiguangError(
                    "registration_unavailable",
                    "详情页没有普通报名入口，未提交报名。",
                    details={"activity_id": activity_id, "kind": kind},
                )
            if kind == "social_practice" and invite_code is None and not reason:
                raise ShiguangError(
                    "requires_user_action",
                    "社会实践报名必须提供报名理由；未提交报名。",
                    details={
                        "activity_id": activity_id,
                        "kind": kind,
                        "action": "provide_reason",
                    },
                )
            try:
                self._validate_before_write_sync(session, generation)
            except ShiguangError as error:
                raise ShiguangError(
                    error.code,
                    str(error),
                    details={"activity_id": activity_id, "kind": kind, **error.details},
                ) from None
            # Persist before the POST so a crash/unknown result cannot cause a
            # later call to silently submit the same activity again.
            self._set_receipt(key, "in_flight", activity_id, kind)
            try:
                result = self._execute(
                    session,
                    lambda client: client.register_activity(
                        activity_id, kind, invite_code=invite_code, reason=reason
                    ),
                )
            except ShiguangError as error:
                if error.code in {"auth_required", "access_denied"}:
                    self._clear_receipt(key)
                else:
                    self._set_receipt(key, "unknown", activity_id, kind)
                details = {
                    "activity_id": activity_id,
                    "kind": kind,
                    "submitted": (
                        None if error.code == "submission_unknown" else False
                    ),
                    "retry": False,
                    **error.details,
                }
                raise ShiguangError(error.code, str(error), details) from None
            if result["status"] in {"registered", "registered_pending_review"}:
                self._set_receipt(key, "success", activity_id, kind)
            elif result["status"] in {"submission_rejected", "submission_unknown"}:
                self._set_receipt(key, "unknown", activity_id, kind)
            else:
                self._clear_receipt(key)
            warnings = []
            try:
                self.store.finish(session.revision)
            except ShiguangError:
                warnings.append(
                    "提交后本地会话已清除或换绑；远端报名结果仍以本次响应为准，"
                    "本地 clear 不会撤销已发出的报名。"
                )
            return {
                "status": result["status"],
                "source_system": "shiguang",
                "activity_id": activity_id,
                "kind": kind,
                "activity_title": preview["title"],
                "invite_code_used": invite_code is not None,
                "submitted": result["submitted"],
                "retry": False,
                "retry_recommended": result["retry_recommended"],
                "server_oper": result["server_oper"],
                "message": result["message"],
                "verification_required": bool(
                    result.get("verification_required", False)
                    or result["status"] == "submission_unknown"
                ),
                "registration": registration,
                "warnings": warnings,
            }

    def _preview_registration_sync(
        self,
        session: ShiguangSession,
        generation: int,
        activity_id: str,
        kind: str,
        *,
        recovered: bool = False,
        binding=None,
    ) -> tuple[dict, ShiguangSession, int]:
        for attempt in range(2):
            try:
                preview = self._execute(
                    session,
                    lambda client: client.registration_preview(activity_id, kind),
                )
                return preview, session, generation
            except ShiguangError as error:
                if error.code not in {"auth_required", "session_expired"} or recovered or attempt:
                    raise
                if binding is not None and binding.configured:
                    session = self._recover_auto_sync(binding, session, generation)
                elif session.has_cas_sso():
                    session, _reused = self.recovery.recover_or_reuse(session, generation)
                else:
                    raise
                _current, generation = self.store.snapshot()
                recovered = True
        raise ShiguangError("auth_required", "石光预检恢复失败；未提交报名。")

    async def check(self) -> dict:
        payload = await self.activities(page=1)
        session = await asyncio.to_thread(self.store.load)
        return {
            "status": "ok",
            "source_system": "shiguang",
            "items": len(payload["items"]),
            "total_pages": payload["total_pages"],
            "session": session.public() if session else None,
        }

    async def adopt_candidate(self, path: Path) -> dict:
        candidate = await asyncio.to_thread(ShiguangStore.candidate, path)
        candidate.binding_id = uuid4().hex
        async with self._write_lock:
            existing, initial_generation = await asyncio.to_thread(self.store.snapshot)
            if existing is not None:
                raise ShiguangError(
                    "already_bound",
                    "目标状态目录已有石光会话；为避免覆盖其他绑定，请先 clear 再 adopt。",
                )
            result = await asyncio.to_thread(
                self._execute, candidate, lambda client: client.activities(page=1)
            )
            candidate.state = "stored"
            candidate.verified_at = now()
            await asyncio.to_thread(
                self.store.replace, candidate, expected_generation=initial_generation
            )
            return {
                "status": "ok",
                "source_system": "shiguang",
                "adopted": True,
                "items": len(result["items"]),
                "total_pages": result["total_pages"],
                "session": candidate.public(),
            }

    def status(self) -> dict:
        session = self.store.load()
        return {
            "status": "ok" if session else "auth_required",
            "source_system": "shiguang",
            "session": session.public() if session else None,
        }

    def clear(self) -> dict:
        self.store.clear()
        return {
            "status": "ok",
            "source_system": "shiguang",
            "local_session_removed": True,
            "remote_session_revoked": False,
        }
