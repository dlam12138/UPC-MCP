"""Independent DPAPI-backed local profile for the campus-card balance query.

Only one student number is stored, in a file and lock that are separate from
both the retired JHZF session (``jhzf-self.dpapi``) and Shiguang
(``shiguang-self.dpapi``). The student number is encrypted with the current
Windows user's DPAPI, is never included in status/query payloads, and is never
written to logs by this module.

The legacy-candidate helper reads only ``student_id`` from an explicitly
selected local DPAPI file for one-time migration research. It does not read or
restore cookies, headers, CAS tickets, WeChat state, or any old authentication.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .card_balance import CardBalanceError, valid_student_id

PROFILE_FILENAME = "card-balance-self.dpapi"
LOCK_FILENAME = "card-balance.lock"
GENERATION_FILENAME = "card-balance-generation"
MAX_PROFILE_BYTES = 16 * 1024
MAX_LEGACY_CANDIDATE_BYTES = 16 * 1024


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_card_balance_state_dir() -> Path:
    """Mirror the project's state-dir convention without importing old storage."""
    configured = os.environ.get("UPC_MCP_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UPC-MCP"


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CardBalanceError("unsupported", "本地余额学号存储只支持 Windows DPAPI。")
    import win32crypt

    return win32crypt.CryptProtectData(data, "UPC card balance profile", None, None, None, 1)


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CardBalanceError("unsupported", "本地余额学号存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
    except Exception:
        raise CardBalanceError(
            "local_error",
            "无法解密本机余额学号配置，请重新执行本地配置。",
        ) from None


class CardBalanceProfile(BaseModel):
    """Encrypted-at-rest local profile; only the student number is sensitive."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    student_id: SecretStr = Field(repr=False)
    configured_at: str = Field(default_factory=now)
    last_verified_at: str | None = None
    source: Literal["local_cli", "legacy_candidate"] = "local_cli"

    @classmethod
    def new(
        cls,
        student_id: str,
        *,
        source: Literal["local_cli", "legacy_candidate"] = "local_cli",
    ) -> CardBalanceProfile:
        if not valid_student_id(student_id):
            raise CardBalanceError("invalid_input", "本地学号配置应为 8–16 位数字。")
        return cls(student_id=SecretStr(student_id), source=source)

    def public(self) -> dict:
        return {
            "state": "verified" if self.last_verified_at else "configured",
            "source": self.source,
            "configured_at": self.configured_at,
            "last_verified_at": self.last_verified_at,
            "identity_checked": self.last_verified_at is not None,
        }

    def secret_payload(self) -> dict:
        data = self.model_dump(mode="json", exclude={"student_id"})
        data["student_id"] = self.student_id.get_secret_value()
        return data


class CardBalanceStore:
    """One profile file plus an independent cross-process lock and generation."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / PROFILE_FILENAME
        self.lock_path = self.directory / LOCK_FILENAME
        self.generation_path = self.directory / GENERATION_FILENAME

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise CardBalanceError("unsupported", "本地余额学号存储只支持 Windows DPAPI。")
        import msvcrt

        with self.lock_path.open("a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + 5
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CardBalanceError(
                            "local_error",
                            "本地余额学号配置正在更新，请稍后重试。",
                        ) from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read_generation(self) -> int:
        if not self.generation_path.exists():
            return 0
        try:
            value = int(self.generation_path.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            raise CardBalanceError("local_error", "本地余额学号配置代次损坏。") from None
        if value < 0:
            raise CardBalanceError("local_error", "本地余额学号配置代次损坏。")
        return value

    def _write_generation(self, value: int) -> None:
        fd, name = tempfile.mkstemp(prefix=".card-balance-generation-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(str(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.generation_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _read(self) -> CardBalanceProfile | None:
        if not self.path.exists():
            return None
        try:
            size = self.path.stat().st_size
        except OSError:
            raise CardBalanceError("local_error", "本地余额学号配置读取失败。") from None
        if size > MAX_PROFILE_BYTES:
            raise CardBalanceError("local_error", "本地余额学号配置文件异常过大。")
        try:
            raw = unprotect(self.path.read_bytes())
        except CardBalanceError:
            raise
        except OSError:
            raise CardBalanceError("local_error", "本地余额学号配置读取失败。") from None
        if len(raw) > MAX_PROFILE_BYTES:
            raise CardBalanceError("local_error", "本地余额学号配置内容异常过大。")
        try:
            profile = CardBalanceProfile.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError):
            raise CardBalanceError(
                "local_error",
                "本地余额学号配置损坏，请重新执行本地配置。",
            ) from None
        if not valid_student_id(profile.student_id.get_secret_value()):
            raise CardBalanceError(
                "local_error",
                "本地余额学号配置无效，请重新执行本地配置。",
            )
        return profile

    def _write(self, profile: CardBalanceProfile) -> None:
        payload = json.dumps(profile.secret_payload(), ensure_ascii=False).encode("utf-8")
        encrypted = protect(payload)
        fd, name = tempfile.mkstemp(prefix=".card-balance-profile-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self) -> CardBalanceProfile | None:
        with self.locked():
            return self._read()

    def snapshot(self) -> tuple[CardBalanceProfile | None, int]:
        """Read the profile and generation atomically so clear cannot split them."""
        with self.locked():
            return self._read(), self._read_generation()

    def create(self, profile: CardBalanceProfile) -> None:
        """Create the first binding; never replace an existing profile."""
        if not valid_student_id(profile.student_id.get_secret_value()):
            raise CardBalanceError("invalid_input", "本地学号配置应为 8–16 位数字。")
        with self.locked():
            if self._read() is not None:
                raise CardBalanceError(
                    "already_configured",
                    "本机已有余额学号配置；为避免覆盖正式绑定，请先 clear。",
                )
            generation = self._read_generation()
            self._write(profile)
            self._write_generation(generation + 1)

    def replace(
        self,
        profile: CardBalanceProfile,
        *,
        expected_revision: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        if not valid_student_id(profile.student_id.get_secret_value()):
            raise CardBalanceError("invalid_input", "本地学号配置应为 8–16 位数字。")
        with self.locked():
            generation = self._read_generation()
            if expected_generation is not None and generation != expected_generation:
                raise CardBalanceError(
                    "profile_changed",
                    "本地余额学号配置已变化，本次旧结果已丢弃。",
                )
            current = self._read()
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise CardBalanceError(
                    "profile_changed",
                    "本地余额学号配置已清除或更换，本次旧结果已丢弃。",
                )
            self._write(profile)
            self._write_generation(generation + 1)

    def mark_verified(self, revision: str, expected_generation: int) -> None:
        with self.locked():
            generation = self._read_generation()
            if generation != expected_generation:
                raise CardBalanceError(
                    "profile_changed",
                    "本地余额学号配置已变化，本次结果已丢弃。",
                )
            current = self._read()
            if current is None or current.revision != revision:
                raise CardBalanceError(
                    "profile_changed",
                    "本地余额学号配置已清除或更换，本次结果已丢弃。",
                )
            current.last_verified_at = now()
            self._write(current)
            self._write_generation(generation + 1)

    def is_current(self, revision: str) -> bool:
        with self.locked():
            current = self._read()
            return current is not None and current.revision == revision

    def clear(self) -> None:
        with self.locked():
            generation = self._read_generation()
            self.path.unlink(missing_ok=True)
            self._write_generation(generation + 1)

    @staticmethod
    def legacy_student_id(path: Path) -> str:
        """Read only the old session's student id, for one-time local migration.

        The explicitly selected file must be a small DPAPI file owned by the
        current Windows user. The payload's cookies, headers, account and any
        other fields are deliberately ignored.
        """
        candidate = Path(path)
        try:
            if (
                not candidate.is_file()
                or candidate.suffix.lower() != ".dpapi"
                or candidate.stat().st_size > MAX_LEGACY_CANDIDATE_BYTES
            ):
                raise CardBalanceError(
                    "invalid_input",
                    "请选择本机 DPAPI 加密的旧会话候选文件（不超过 16 KiB）。",
                )
            raw = unprotect(candidate.read_bytes())
        except CardBalanceError:
            raise
        except OSError:
            raise CardBalanceError("invalid_input", "旧会话候选文件读取失败。") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            raise CardBalanceError("invalid_input", "旧会话候选格式不受支持。") from None
        if not isinstance(payload, dict):
            raise CardBalanceError("invalid_input", "旧会话候选格式不受支持。")
        student_id = payload.get("student_id")
        if not valid_student_id(student_id):
            raise CardBalanceError("invalid_input", "旧会话候选中没有可用的本人学号。")
        return student_id


# Neutral aliases for integration code that prefers the shorter names.
BalanceStore = CardBalanceStore
BalanceProfile = CardBalanceProfile
