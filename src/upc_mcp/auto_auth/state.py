"""Persistent cross-process recovery state and backoff for auto-auth."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .credentials import Binding
from .errors import AutoAuthError
from .locks import FileLock

MAX_STATE_BYTES = 32 * 1024


@dataclass
class RecoveryState:
    version: int = 1
    generation: int = 0
    account_hash: str | None = None
    blocked_until: float = 0.0
    blocked_code: str | None = None
    requires_user_action: bool = False
    user_action_kind: str | None = None
    system_backoff: dict[str, float] = field(default_factory=dict)
    updated_at: float = 0.0

    def public(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "generation": self.generation,
            "blocked": bool(self.blocked_until > now or self.requires_user_action),
            "blocked_until": self.blocked_until if self.blocked_until > now else None,
            "blocked_code": self.blocked_code,
            "requires_user_action": self.requires_user_action,
            "user_action_kind": self.user_action_kind,
            "system_backoff": {
                name: until for name, until in self.system_backoff.items() if until > now
            },
        }


class RecoveryStateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "recovery-state.json"
        self.lock_path = directory / "recovery-state.lock"

    def _parse(self, raw: dict, binding: Binding) -> RecoveryState:
        if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
            raise ValueError
        generation = raw.get("generation")
        account = raw.get("account_hash")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError
        if account is not None and (
            not isinstance(account, str) or len(account) != 64
            or any(ch not in "0123456789abcdef" for ch in account)
        ):
            raise ValueError
        blocked_until = raw.get("blocked_until", 0.0)
        if isinstance(blocked_until, bool) or not isinstance(blocked_until, (int, float)):
            raise ValueError
        if not math.isfinite(blocked_until) or blocked_until < 0:
            raise ValueError
        blocked_code = raw.get("blocked_code")
        if blocked_code is not None and not isinstance(blocked_code, str):
            raise ValueError
        requires = raw.get("requires_user_action", False)
        if not isinstance(requires, bool):
            raise ValueError
        kind = raw.get("user_action_kind")
        if kind is not None and not isinstance(kind, str):
            raise ValueError
        backoff = raw.get("system_backoff", {})
        if not isinstance(backoff, dict):
            raise ValueError
        clean: dict[str, float] = {}
        for name, until in backoff.items():
            if not isinstance(name, str) or not name or len(name) > 32:
                raise ValueError
            if isinstance(until, bool) or not isinstance(until, (int, float)):
                raise ValueError
            if not math.isfinite(until) or until < 0:
                raise ValueError
            clean[name] = float(until)
        updated = raw.get("updated_at", 0.0)
        if (
            isinstance(updated, bool) or not isinstance(updated, (int, float))
            or not math.isfinite(updated) or updated < 0
        ):
            raise ValueError
        state = RecoveryState(
            generation=generation,
            account_hash=account,
            blocked_until=float(blocked_until),
            blocked_code=blocked_code,
            requires_user_action=requires,
            user_action_kind=kind,
            system_backoff=clean,
            updated_at=float(updated),
        )
        # A different account/generation starts clean; old blocks never leak
        # into a new identity.
        if state.generation != binding.generation or state.account_hash != binding.account_hash:
            return RecoveryState(
                generation=binding.generation, account_hash=binding.account_hash
            )
        return state

    def _read_locked(self, binding: Binding) -> RecoveryState:
        if not self.path.exists():
            return RecoveryState(
                generation=binding.generation, account_hash=binding.account_hash
            )
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
            return self._parse(raw, binding)
        except (OSError, ValueError, TypeError):
            raise AutoAuthError(
                "auto_auth_state_corrupt",
                "本机自动认证状态损坏；未提交密码，请显式清除后重试。",
                action="auto_auth_clear_state",
            ) from None

    def load(self, binding: Binding) -> RecoveryState:
        with FileLock(self.lock_path, timeout=5.0).held():
            return self._read_locked(binding)

    def _write(self, state: RecoveryState) -> None:
        payload = {
            "version": 1,
            "generation": state.generation,
            "account_hash": state.account_hash,
            "blocked_until": state.blocked_until,
            "blocked_code": state.blocked_code,
            "requires_user_action": state.requires_user_action,
            "user_action_kind": state.user_action_kind,
            "system_backoff": state.system_backoff,
            "updated_at": time.time(),
        }
        self._parse(payload, Binding(True, state.generation, state.account_hash))
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > MAX_STATE_BYTES:
            raise AutoAuthError("local_error", "自动认证状态超过本地大小限制。")
        fd, name = tempfile.mkstemp(prefix=".auto-auth-state-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def update(self, binding: Binding, **changes) -> RecoveryState:
        with FileLock(self.lock_path, timeout=5.0).held():
            state = self._read_locked(binding)
            for key, value in changes.items():
                if not hasattr(state, key):
                    raise ValueError(key)
                setattr(state, key, value)
            self._write(state)
            return state

    def set(self, state: RecoveryState) -> None:
        with FileLock(self.lock_path, timeout=5.0).held():
            self._write(state)

    def reset(self, binding: Binding) -> RecoveryState:
        state = RecoveryState(
            generation=binding.generation, account_hash=binding.account_hash
        )
        self.set(state)
        return state

    def clear(self) -> None:
        with FileLock(self.lock_path, timeout=5.0).held():
            self.path.unlink(missing_ok=True)

    def unlock_user_action(self, binding: Binding) -> dict:
        with FileLock(self.lock_path, timeout=5.0).held():
            if not self.path.exists():
                return {"status": "ok", "unlocked": False, "reason": "no_block"}
            try:
                if self.path.stat().st_size > MAX_STATE_BYTES:
                    raise ValueError
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                state = self._parse(raw, binding)
            except (OSError, ValueError, TypeError):
                raise AutoAuthError(
                    "auto_auth_state_corrupt",
                    "本机自动认证状态损坏；未清除任何密码/验证码状态。",
                ) from None
            if not state.requires_user_action:
                return {"status": "ok", "unlocked": False, "reason": "not_user_action"}
            if state.user_action_kind not in {"captcha", "second_factor"}:
                raise AutoAuthError(
                    "credential_reconfigure_required",
                    "当前封锁来自密码错误；请重新配置凭据，而不是仅解锁。",
                    action="credential_configure",
                )
            state.requires_user_action = False
            state.user_action_kind = None
            state.blocked_code = None
            state.blocked_until = 0.0
            self._write(state)
            return {"status": "ok", "unlocked": True, "kind": "user_action"}
