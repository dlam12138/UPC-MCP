"""Independent DPAPI session storage and locks for the campus-app timetable.

This module never imports another campus system's store or service.  It uses
its own file, lock, generation and auth lock names so clearing/rebinding the
timetable session cannot affect library, Shiguang, card-balance or JHZF data.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .timetable_protocol import TimetableCookie, TimetableError

MAX_CANDIDATE_BYTES = 16 * 1024
MAX_SESSION_BYTES = 256 * 1024
MAX_GENERATION_BYTES = 64
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise TimetableError("unsupported", "课表会话存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptProtectData(
            data, "UPC Timetable session", None, None, None, 1
        )
    except Exception:
        raise TimetableError(
            "local_error", "无法加密本机课表会话；未写入任何明文。"
        ) from None


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise TimetableError("unsupported", "课表会话存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
    except Exception:
        raise TimetableError(
            "auth_required",
            "无法解密本机课表会话；请重新完成本地正常登录。",
            action="complete_official_login",
        ) from None


class TimetableSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    cookies: list[TimetableCookie] = Field(default_factory=list, repr=False)
    acquired_at: str = Field(default_factory=now)
    verified_at: str | None = None
    state: Literal["stored", "unverified", "auth_expired"] = "unverified"
    source: Literal[
        "normal_browser_login",
        "adopted_cdp",
        "adopted_candidate",
        "auto_auth_sso",
        "auto_auth_password",
    ] = "adopted_candidate"
    identity_verified: bool = False
    credential_generation: int | None = None
    account_hash: str | None = None

    @field_validator("cookies")
    @classmethod
    def _nonempty_cookies(cls, value: list[TimetableCookie]) -> list[TimetableCookie]:
        if not value:
            raise ValueError("at least one cookie is required")
        names = [cookie.name for cookie in value]
        if len(names) != len(set(names)):
            raise ValueError("duplicate cookie names")
        return value

    def public(self) -> dict:
        return {
            "session_ref": "self",
            "state": self.state,
            "identity_verified": False,
            "identity_stable": False,
            "acquired_at": self.acquired_at,
            "last_verified_at": self.verified_at,
            "source": self.source,
            "cookie_count": len(self.cookies),
            "expiry_known": any(
                cookie.expires_at is not None for cookie in self.cookies
            ),
            "credential_generation": self.credential_generation,
            "credential_bound": bool(self.account_hash and self.credential_generation is not None),
            "account_ref": self.account_hash[:16] if self.account_hash else None,
        }

    def secret_payload(self) -> dict:
        data = self.model_dump(mode="json", exclude={"cookies"})
        data["cookies"] = [cookie.secret_payload() for cookie in self.cookies]
        return data


class TimetableStore:
    """Separate file, lock and generation; late writers are rejected."""

    def __init__(self, directory: Path, *, lock_timeout: float = 5.0):
        self.directory = directory
        self.path = directory / "timetable-self.dpapi"
        self.lock_path = directory / "timetable-session.lock"
        self.generation_path = directory / "timetable-generation"
        self.lock_timeout = lock_timeout

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise TimetableError("unsupported", "课表会话存储只支持 Windows DPAPI。")
        thread_lock = _thread_lock(self.lock_path)
        deadline = time.monotonic() + self.lock_timeout
        if not thread_lock.acquire(timeout=self.lock_timeout):
            raise TimetableError("local_error", "本地课表会话正在更新，请稍后重试。")
        try:
            import msvcrt

            with self.lock_path.open("a+b") as handle:
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimetableError(
                                "local_error", "本地课表会话正在更新，请稍后重试。"
                            ) from None
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            thread_lock.release()

    def _read_generation(self) -> int:
        if not self.generation_path.exists():
            return 0
        try:
            if self.generation_path.stat().st_size > MAX_GENERATION_BYTES:
                raise TimetableError("local_error", "本地课表会话代次损坏。")
            value = int(self.generation_path.read_text(encoding="ascii").strip() or "0")
        except TimetableError:
            raise
        except (OSError, ValueError):
            raise TimetableError("local_error", "本地课表会话代次损坏。") from None
        if value < 0:
            raise TimetableError("local_error", "本地课表会话代次损坏。")
        return value

    def _write_generation(self, value: int) -> None:
        fd, name = tempfile.mkstemp(prefix=".timetable-generation-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(str(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.generation_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _read(self) -> TimetableSession | None:
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > MAX_SESSION_BYTES:
                raise TimetableError("local_error", "本地课表会话文件过大，请重新登录。")
            payload = self.path.read_bytes()
        except TimetableError:
            raise
        except OSError:
            raise TimetableError(
                "local_error", "本地课表会话文件读取失败，请重新登录。"
            ) from None
        try:
            return TimetableSession.model_validate_json(unprotect(payload))
        except TimetableError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise TimetableError("local_error", "本地课表会话文件损坏，请重新登录。") from None

    def _write(self, session: TimetableSession) -> None:
        payload = json.dumps(session.secret_payload(), ensure_ascii=False).encode("utf-8")
        encrypted = protect(payload)
        fd, name = tempfile.mkstemp(prefix=".timetable-session-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self) -> TimetableSession | None:
        with self.locked():
            return self._read()

    def snapshot(self) -> tuple[TimetableSession | None, int]:
        with self.locked():
            return self._read(), self._read_generation()

    def generation(self) -> int:
        with self.locked():
            return self._read_generation()

    def replace(
        self,
        session: TimetableSession,
        *,
        expected_revision: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        with self.locked():
            generation = self._read_generation()
            if expected_generation is not None and generation != expected_generation:
                raise TimetableError("auth_required", "课表会话代次已变化，本次旧结果已丢弃。")
            current = self._read()
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise TimetableError("auth_required", "课表会话已清除或更换，本次旧结果已丢弃。")
            self._write(session)
            self._write_generation(generation + 1)

    def finish(self, revision: str, generation: int, state: str = "stored") -> None:
        with self.locked():
            current = self._read()
            if (
                current is None
                or current.revision != revision
                or self._read_generation() != generation
            ):
                raise TimetableError("auth_required", "课表会话已清除或更换，本次旧结果已丢弃。")
            current.state = state  # type: ignore[assignment]
            if state == "stored":
                current.verified_at = now()
            self._write(current)

    def mark_expired(self, revision: str, generation: int) -> None:
        with self.locked():
            current = self._read()
            if (
                current is None
                or current.revision != revision
                or self._read_generation() != generation
            ):
                return
            current.state = "auth_expired"
            self._write(current)

    def is_current(self, revision: str, generation: int) -> bool:
        with self.locked():
            current = self._read()
            return bool(
                current
                and current.revision == revision
                and self._read_generation() == generation
            )

    def clear(self) -> None:
        with self.locked():
            generation = self._read_generation()
            self.path.unlink(missing_ok=True)
            self._write_generation(generation + 1)

    @staticmethod
    def candidate(path: Path) -> list[TimetableCookie]:
        """Load an operator-created DPAPI cookie candidate; no plaintext file."""

        try:
            valid = (
                path.is_file()
                and path.suffix.lower() == ".dpapi"
                and path.stat().st_size <= MAX_CANDIDATE_BYTES
            )
        except OSError:
            valid = False
        if not valid:
            raise TimetableError(
                "invalid_input", "请选择本机 DPAPI 加密的课表会话候选文件。"
            )
        try:
            payload = json.loads(unprotect(path.read_bytes()))
        except TimetableError:
            raise
        except (ValueError, TypeError):
            raise TimetableError("invalid_input", "课表会话候选格式不受支持。") from None
        if not isinstance(payload, dict) or payload.get("version") not in (None, 1):
            raise TimetableError("invalid_input", "课表会话候选必须是 version=1 的 JSON 对象。")
        raw_cookies = payload.get("cookies")
        if not isinstance(raw_cookies, list) or not raw_cookies:
            raise TimetableError("invalid_input", "课表会话候选缺少 cookies。")
        try:
            cookies = [TimetableCookie.model_validate(item) for item in raw_cookies]
        except ValidationError:
            raise TimetableError("invalid_input", "课表会话候选的 Cookie 格式无效。") from None
        return cookies

    @staticmethod
    def write_candidate(path: Path, cookies: list[TimetableCookie]) -> None:
        """Test/operator helper: write a DPAPI-encrypted cookie candidate."""

        if path.suffix.lower() != ".dpapi":
            raise TimetableError("invalid_input", "候选文件必须使用 .dpapi 后缀。")
        payload = json.dumps(
            {"version": 1, "cookies": [cookie.secret_payload() for cookie in cookies]},
            ensure_ascii=False,
        ).encode("utf-8")
        encrypted = protect(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".timetable-candidate-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


class TimetableAuthLock:
    """Cross-process single-flight guard for normal browser login/adoption."""

    def __init__(self, directory: Path, timeout: float = 30.0):
        self.directory = directory
        self.path = directory / "timetable-auth.lock"
        self.timeout = timeout

    def acquire(self) -> _HeldLock:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise TimetableError("unsupported", "课表认证锁只支持 Windows。")
        thread_lock = _thread_lock(self.path)
        if not thread_lock.acquire(timeout=self.timeout):
            raise TimetableError("auth_in_progress", "另一次课表登录正在进行。")
        try:
            import msvcrt

            handle = self.path.open("a+b")
            try:
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                deadline = time.monotonic() + self.timeout
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        return _HeldLock(handle, thread_lock)
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimetableError(
                                "auth_in_progress", "另一次课表登录正在进行。"
                            ) from None
                        time.sleep(0.05)
            except Exception:
                handle.close()
                raise
        except Exception:
            thread_lock.release()
            raise

    @contextmanager
    def held(self):
        held = self.acquire()
        try:
            yield
        finally:
            held.release()


class _HeldLock:
    def __init__(self, handle, thread_lock: threading.Lock):
        self.handle = handle
        self.thread_lock = thread_lock
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        import msvcrt

        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.thread_lock.release()
