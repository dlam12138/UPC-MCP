"""Local configuration lifecycle and one anonymous balance query.

``configure``/``configure_from_legacy_candidate`` are local, user-driven
operations. They must never be exposed as MCP tool parameters: a model must not
be able to choose or change the student number. ``query`` takes no identifier
and always uses the locally configured profile.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from .card_balance import CardBalanceClient, CardBalanceError, CardBalanceSnapshot, valid_student_id
from .card_balance_profile import CardBalanceProfile, CardBalanceStore, now


class CardBalanceService:
    def __init__(
        self,
        store: CardBalanceStore,
        client: CardBalanceClient | None = None,
        *,
        credentials=None,
    ):
        self.store = store
        self.client = client or CardBalanceClient()
        self.credentials = credentials
        self._query_lock = asyncio.Lock()

    def _unified_identity(self):
        """Read only the local identity; never log in or send a password."""
        if self.credentials is None:
            return None
        from .auto_auth.errors import AutoAuthError

        try:
            binding = self.credentials.snapshot()
            if not binding.configured:
                if binding.generation:
                    raise CardBalanceError(
                        "not_configured", "统一账号已移除，请在本地设置页重新保存学号。"
                    )
                return None  # Compatibility for standalone balance-only installations.
            credential = self.credentials.load()
            student_id = credential.username
            binding = credential.binding
            del credential
            if not valid_student_id(student_id):
                raise CardBalanceError(
                    "invalid_student_id", "已保存的学校账号不是有效学号，请更新本地账号。"
                )
            return student_id, binding
        except AutoAuthError:
            raise CardBalanceError(
                "local_error", "无法读取本机统一账号，请检查本地设置。"
            ) from None

    @staticmethod
    def _unified_status() -> dict:
        return {
            "status": "ok", "source_system": "card_balance", "configured": True,
            "profile": {"state": "configured", "source": "unified_account",
                        "identity_checked": False},
        }

    def configure(
        self,
        student_id: str,
        *,
        source: Literal["local_cli", "legacy_candidate"] = "local_cli",
    ) -> dict:
        """Create the local self binding; local CLI only, never an MCP input.

        Rebinding to a different student number requires an explicit ``clear``
        first, so a stale or mistaken command cannot silently overwrite the
        formal local binding.
        """
        profile = CardBalanceProfile.new(student_id, source=source)
        unified = self._unified_identity()
        if unified is not None:
            if student_id != unified[0]:
                raise CardBalanceError(
                    "account_mismatch", "余额学号跟随统一账号，请在本地设置页更换账号。"
                )
            return self._unified_status()
        existing, _generation = self.store.snapshot()
        if existing is not None:
            current_id = existing.student_id.get_secret_value()
            if current_id != student_id:
                raise CardBalanceError(
                    "already_configured",
                    "本机已有其他学号的余额配置；请先 clear 后再配置。",
                )
            return self._status(existing)
        try:
            self.store.create(profile)
        except CardBalanceError as error:
            if error.code != "already_configured":
                raise
            existing, _generation = self.store.snapshot()
            if existing is None or existing.student_id.get_secret_value() != student_id:
                raise
            return self._status(existing)
        current = self.store.load()
        if current is None:
            raise CardBalanceError(
                "profile_changed",
                "本地余额学号配置在创建后立即变化，请重试。",
            )
        return self._status(current)

    def configure_from_legacy_candidate(self, path: Path) -> dict:
        """One-time migration research: read only the old ``student_id``.

        Existing bindings are never read or overwritten. Cookies, headers, CAS
        tickets, WeChat state and the old account value are not read into the
        profile.
        """
        if self.store.load() is not None:
            raise CardBalanceError(
                "already_configured",
                "本机已有余额学号配置；为避免覆盖正式绑定，未读取旧候选。",
            )
        student_id = CardBalanceStore.legacy_student_id(path)
        return self.configure(student_id, source="legacy_candidate")

    def status(self) -> dict:
        """Report configuration state without the student number or revision."""
        if self._unified_identity() is not None:
            return self._unified_status()
        profile = self.store.load()
        return self._status(profile)

    def _status(self, profile: CardBalanceProfile | None) -> dict:
        if profile is None:
            return {
                "status": "not_configured",
                "source_system": "card_balance",
                "configured": False,
                "profile": None,
            }
        return {
            "status": "ok",
            "source_system": "card_balance",
            "configured": True,
            "profile": profile.public(),
        }

    def clear(self) -> dict:
        self.store.clear()
        if self.credentials is not None and self.credentials.snapshot().configured:
            return {
                **self.status(), "local_profile_removed": True, "remote_changed": False,
                "message": "独立余额配置已移除；余额仍跟随统一账号，停用请移除统一凭据。",
            }
        return {
            "status": "ok",
            "source_system": "card_balance",
            "configured": False,
            "local_profile_removed": True,
            "remote_changed": False,
        }

    async def query(self) -> dict:
        """Query the locally configured card; no student id is accepted here."""
        async with self._query_lock:
            unified = await asyncio.to_thread(self._unified_identity)
            if unified is not None:
                student_id, binding = unified
                snapshot = await self.client.fetch(student_id)
                current = await asyncio.to_thread(self.credentials.snapshot)
                if not binding.matches(current):
                    raise CardBalanceError(
                        "profile_changed", "统一账号已清除或更换，本次余额结果已丢弃。"
                    )
                result = self._result(snapshot)
                result["query"]["student_scope"] = "unified_account"
                return result
            # DPAPI/file locks are blocking. Keep them off the event loop so a
            # contended balance lock cannot stall Shiguang or another MCP tool.
            profile, generation = await asyncio.to_thread(self.store.snapshot)
            if profile is None:
                raise CardBalanceError(
                    "not_configured",
                    "本机尚未配置本人余额学号，请先在本地完成配置。",
                )
            snapshot = await self.client.fetch(profile.student_id.get_secret_value())

            # A unified account saved during a standalone query becomes the authority.
            if await asyncio.to_thread(self._unified_identity) is not None:
                raise CardBalanceError(
                    "profile_changed", "统一账号已配置，本次旧余额结果已丢弃。"
                )

            # Never return a result that belongs to a profile cleared or
            # rebound while the network request was in flight.
            if not await asyncio.to_thread(self.store.is_current, profile.revision):
                raise CardBalanceError(
                    "profile_changed",
                    "本地余额学号配置已清除或更换，本次结果已丢弃。",
                )
            try:
                await asyncio.to_thread(
                    self.store.mark_verified,
                    profile.revision,
                    generation,
                )
            except CardBalanceError as error:
                if error.code != "profile_changed":
                    raise
                if not await asyncio.to_thread(
                    self.store.is_current,
                    profile.revision,
                ):
                    raise CardBalanceError(
                        "profile_changed",
                        "本地余额学号配置已清除或更换，本次结果已丢弃。",
                    ) from None
                # Another process may have only refreshed last_verified_at;
                # the binding revision is still current, so the result is valid.
            return self._result(snapshot)

    @staticmethod
    def _result(snapshot: CardBalanceSnapshot) -> dict:
        return {
            "status": "ok",
            "source_system": "card_balance",
            "query": {"subject": "self", "student_scope": "local_profile"},
            "fetched_at": now(),
            "from_cache": False,
            "stale": False,
            "data": snapshot.public(),
            "error": None,
        }


BalanceService = CardBalanceService
