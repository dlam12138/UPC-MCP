"""Cross-process single-flight CAS coordinator for the three systems.

The global CAS lock covers only probe/password/ticket/checkpoint work. A slow
library provider never holds it while establishing its business session.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .cas import CasAuth, CasSessionStore, CasTicket
from .credentials import Binding, CredentialStore
from .errors import AutoAuthError
from .locks import FileLock
from .providers import (
    LibraryProvider,
    ServicePortalProvider,
    ShiguangProvider,
    TimetableProvider,
)
from .state import RecoveryState, RecoveryStateStore

AUTH_ACTION_CODES = {
    "cas_invalid_credentials",
    "cas_captcha_required",
    "cas_second_factor_required",
}
SHORT_BACKOFF_CODES = {
    "cas_access_denied",
    "cas_unexpected_redirect",
    "cas_ticket_missing",
    "cas_unexpected_form_action",
    "cas_login_rejected",
    "cas_login_page_unrecognized",
    "cas_network_error",
    "cas_tls_error",
    "cas_upstream_error",
    "service_timeout",
    "service_network_error",
    "service_upstream_error",
    "service_protocol_error",
    "service_unexpected_redirect",
    "service_token_missing",
    "service_redirect_limit",
    "service_auth_failed",
}


class AutoAuthCoordinator:
    def __init__(
        self,
        directory: Path,
        *,
        credentials: CredentialStore | None = None,
        cas: CasAuth | None = None,
        cas_store: CasSessionStore | None = None,
        state: RecoveryStateStore | None = None,
        providers: dict | None = None,
        clock=time.time,
    ):
        self.directory = directory
        self.credentials = credentials or CredentialStore(directory)
        self.cas = cas or CasAuth()
        self.cas_store = cas_store or CasSessionStore(directory)
        self.state = state or RecoveryStateStore(directory)
        self.providers = providers or {
            "shiguang": ShiguangProvider(),
            "timetable": TimetableProvider(),
            "library": LibraryProvider(),
            "service_portal": ServicePortalProvider(),
        }
        self.clock = clock
        self.cas_lock_path = directory / "cas-login.lock"
        self.system_lock_paths = {
            name: directory / f"system-{name}.lock" for name in self.providers
        }

    def binding(self) -> Binding:
        return self.credentials.snapshot()

    def is_configured(self) -> bool:
        return self.credentials.snapshot().configured

    def _map_error(self, system: str, binding: Binding, error: AutoAuthError) -> None:
        with self.credentials.guard():
            if not self.credentials.snapshot_locked().matches(binding):
                raise AutoAuthError(
                    "credential_changed", "凭据已更换；旧认证结果和状态已丢弃。"
                )
            self._map_error_current(system, binding, error)

    def _map_error_current(self, system: str, binding: Binding, error: AutoAuthError) -> None:
        if error.code not in AUTH_ACTION_CODES and error.code not in SHORT_BACKOFF_CODES:
            return
        now = self.clock()
        if error.code in AUTH_ACTION_CODES:
            kind = {
                "cas_invalid_credentials": "invalid_credentials",
                "cas_captcha_required": "captcha",
                "cas_second_factor_required": "second_factor",
            }.get(error.code, error.code)
            self.state.update(
                binding,
                requires_user_action=True,
                user_action_kind=kind,
                blocked_code=error.code,
                blocked_until=0.0,
            )
            return
        if error.code.startswith("cas_"):
            self.state.update(
                binding, blocked_until=now + 15.0, blocked_code=error.code
            )
            return
        state = self.state.load(binding)
        backoff = dict(state.system_backoff)
        backoff[system] = max(backoff.get(system, 0.0), now + 15.0)
        self.state.update(binding, system_backoff=backoff)

    def _check_backoff(
        self, state: RecoveryState, system: str, *, include_global: bool = True
    ) -> None:
        now = self.clock()
        if include_global and state.blocked_until > now:
            raise AutoAuthError(
                "recovery_backoff",
                "最近一次自动登录尚未确认或正在退避；本轮未提交凭据。",
                details={"retry_after_seconds": round(state.blocked_until - now, 1)},
            )
        system_until = state.system_backoff.get(system, 0.0)
        if system_until > now:
            raise AutoAuthError(
                "recovery_backoff",
                "该系统最近一次恢复失败，正在短暂退避。",
                details={"retry_after_seconds": round(system_until - now, 1)},
            )

    def _check_user_block(self, state: RecoveryState, system: str, binding: Binding) -> None:
        self._check_backoff(state, system)
        if state.requires_user_action:
            if state.user_action_kind == "invalid_credentials":
                raise AutoAuthError(
                    "credential_reconfigure_required",
                    "本代次密码登录已被明确拒绝；请重新配置统一凭据。",
                    action="credential_configure",
                )
            raise AutoAuthError(
                "user_action_required",
                "CAS 要求验证码或二次验证；请先完成官方交互，再显式解锁一次。",
                action="auto_auth_unlock",
                details={"kind": state.user_action_kind},
            )

    def _ticket_for(
        self, system: str, binding: Binding, prepared, *, allow_password: bool = True
    ) -> CasTicket:
        with FileLock(self.cas_lock_path, timeout=45.0).held():
            # Re-read after acquiring the cross-process lock: another process
            # may have completed a login while this caller waited.
            current = self.credentials.snapshot()
            if not current.matches(binding):
                raise AutoAuthError(
                    "credential_changed",
                    "等待认证期间统一凭据已更换或清除；本次恢复已丢弃。",
                )
            self._check_backoff(self.state.load(binding), system)
            try:
                cookies = self.cas_store.load(binding)
            except AutoAuthError:
                raise
            if cookies:
                try:
                    ticket = self.cas.get_ticket_at(
                        prepared.cas_url or self.cas.login_url(system),
                        system,
                        cookies,
                    )
                except AutoAuthError as exc:
                    if exc.code in {
                        "cas_network_error",
                        "cas_tls_error",
                        "cas_upstream_error",
                        "cas_access_denied",
                    }:
                        self._map_error(system, binding, exc)
                        raise
                    raise
                if ticket is not None:
                    return ticket
            if not allow_password:
                raise AutoAuthError(
                    "auth_required",
                    "CAS SSO 检查点不可用；本轮未提交密码。",
                    action="complete_official_login",
                )
            state = self.state.load(binding)
            self._check_user_block(state, system, binding)
            try:
                credential = self.credentials.load()
            except AutoAuthError:
                raise
            if not credential.binding.matches(binding):
                raise AutoAuthError(
                    "credential_changed",
                    "读取凭据时账户代次已变化；本次恢复已丢弃。",
                )
            # Persist intent before the only password POST. A killed worker or
            # process cannot release the lock and immediately start another login.
            with self.credentials.guard():
                if not self.credentials.snapshot_locked().matches(binding):
                    raise AutoAuthError("credential_changed", "凭据已更换，未提交密码。")
                self.state.update(
                    binding,
                    blocked_until=self.clock() + 300.0,
                    blocked_code="cas_login_in_flight",
                )
            try:
                if prepared.cas_url:
                    round_trip = self.cas.password_login_at(
                        prepared.cas_url, system, credential
                    )
                else:
                    round_trip = self.cas.password_login(system, credential)
            except AutoAuthError as exc:
                if exc.code in AUTH_ACTION_CODES:
                    self._map_error(system, binding, exc)
                elif exc.code in {
                    "cas_submit_timeout", "cas_network_error", "cas_upstream_error"
                }:
                    with self.credentials.guard():
                        if self.credentials.snapshot_locked().matches(binding):
                            self.state.update(
                                binding,
                                blocked_until=self.clock() + 300.0,
                                blocked_code=exc.code,
                            )
                else:
                    self._map_error(system, binding, exc)
                raise
            # The generation check and checkpoint write are atomic with
            # credential clear/rebind through the shared guard.
            with self.credentials.guard():
                current = self.credentials.snapshot_locked()
                if not current.matches(binding):
                    raise AutoAuthError(
                        "credential_changed",
                        "登录完成前统一凭据已更换或清除；本次结果已丢弃。",
                    )
                self.cas_store.save(round_trip.cookies, binding)
                self.state.update(binding, blocked_until=0.0, blocked_code=None)
            return round_trip.ticket

    def recover_sync(self, system: str, *, allow_password: bool = True) -> Any:
        if system not in self.providers:
            raise AutoAuthError("invalid_input", "未知的自动认证系统。")
        provider = self.providers[system]
        with FileLock(self.system_lock_paths[system], timeout=45.0).held():
            binding = self.credentials.snapshot()
            if not binding.configured:
                raise AutoAuthError(
                    "credential_missing",
                    "本机尚未配置统一账号密码。",
                    action="credential_configure",
                )
            # An active password attempt holds cas-login.lock. Other systems
            # wait there, then reuse its checkpoint instead of failing early.
            self._check_backoff(self.state.load(binding), system, include_global=False)
            # SSO may still work while a password challenge is pending.
            try:
                prepared = provider.prepare()
            except AutoAuthError as exc:
                self._map_error(system, binding, exc)
                raise
            ticket = self._ticket_for(
                system, binding, prepared, allow_password=allow_password
            )
            try:
                session = provider.establish(ticket, binding, prepared)
            except AutoAuthError as exc:
                self._map_error(system, binding, exc)
                raise
            with self.credentials.guard():
                current = self.credentials.snapshot_locked()
                if not current.matches(binding):
                    raise AutoAuthError(
                        "credential_changed",
                        "业务会话建立期间统一凭据已更换或清除；本次结果已丢弃。",
                    )
            return session

    async def recover(self, system: str, *, allow_password: bool = True) -> Any:
        return await asyncio.to_thread(
            self.recover_sync, system, allow_password=allow_password
        )

    def status(self) -> dict:
        try:
            binding = self.credentials.snapshot()
        except AutoAuthError as exc:
            return {"configured": False, "error": exc.code}
        result = {
            "configured": binding.configured,
            "generation": binding.generation,
            "account_ref": binding.account_ref(),
            "cas_checkpoint": "absent",
            "recovery": None,
        }
        if binding.configured:
            try:
                cookies = self.cas_store.load(binding)
                result["cas_checkpoint"] = "present" if cookies else "absent"
            except AutoAuthError as exc:
                result["cas_checkpoint"] = "corrupt"
                result["cas_error"] = exc.code
            try:
                result["recovery"] = self.state.load(binding).public(self.clock())
            except AutoAuthError as exc:
                result["recovery"] = {"error": exc.code}
        return result

    def unlock(self) -> dict:
        with self.credentials.guard():
            binding = self.credentials.snapshot_locked()
            if not binding.configured:
                raise AutoAuthError(
                    "credential_missing", "本机尚未配置统一凭据。", action="credential_configure"
                )
            return self.state.unlock_user_action(binding)

    def configure(self, username: str, password: str, *, expected: Binding) -> Binding:
        with self.credentials.guard():
            if not self.credentials.snapshot_locked().matches(expected):
                raise AutoAuthError("credential_changed", "配置期间凭据已更换，请重试。")
            self.cas_store.clear()
            self.state.clear()
            return self.credentials.store_locked(username, password, expected=expected)

    def adopt_checkpoint(self, source: Path, binding: Binding) -> None:
        with self.credentials.guard():
            if not self.credentials.snapshot_locked().matches(binding):
                raise AutoAuthError("credential_changed", "凭据已更换，未采用旧检查点。")
            self.cas_store.adopt_from(source, binding)

    def clear_credential(self, *, expected: Binding | None = None) -> Binding:
        with self.credentials.guard():
            if expected is not None and not self.credentials.snapshot_locked().matches(expected):
                raise AutoAuthError("credential_changed", "清除前凭据已更换，请刷新设置页。")
            self.cas_store.clear()
            self.state.clear()
            return self.credentials.clear_locked()

    def clear_cas_checkpoint(self) -> None:
        with self.credentials.guard():
            self.cas_store.clear()

    def clear_state(self) -> None:
        with self.credentials.guard():
            self.state.clear()


def create_auto_auth(directory: Path) -> AutoAuthCoordinator:
    """Create the production coordinator without reading any credential."""
    auto_dir = directory / "auto-auth"
    return AutoAuthCoordinator(auto_dir)
