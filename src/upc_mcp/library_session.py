"""Independent DPAPI-backed session storage for the library seat system.

The Shiguang, card-balance and retired JHZF stores are never imported, read or
written here. Only a public summary (never the token, user id or name) may cross
the CLI/MCP boundary.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .library_seat import LibraryError

MAX_CANDIDATE_BYTES = 16 * 1024
TOKEN_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,4096}$")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise LibraryError("unsupported", "图书馆座位会话存储只支持 Windows DPAPI。")
    import win32crypt

    return win32crypt.CryptProtectData(data, "UPC Library seat session", None, None, None, 1)


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise LibraryError("unsupported", "图书馆座位会话存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
    except Exception:
        raise LibraryError(
            "auth_required",
            "无法解密本机图书馆座位授权资料，请重新完成本地采用。",
        ) from None


class LibrarySession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    binding_id: str | None = None
    token: SecretStr
    user_id: str = Field(repr=False, min_length=1, max_length=256)
    username: str = Field(repr=False, min_length=1, max_length=256)
    name: str = Field(default="", repr=False, max_length=256)
    identity_hash: str | None = None
    identity_stable: bool = False
    acquired_at: str = Field(default_factory=now)
    verified_at: str | None = None
    state: Literal["stored", "unverified", "auth_expired"] = "unverified"
    source: Literal[
        "browser_seat_token",
        "browser_app_token",
        "adopted_candidate",
        "cdp_seat_token",
        "auto_auth_sso",
        "auto_auth_password",
    ] = "adopted_candidate"
    credential_generation: int | None = None
    account_hash: str | None = None

    def effective_binding_id(self) -> str:
        return self.binding_id or self.revision

    def public(self) -> dict:
        return {
            "session_ref": "self",
            "state": self.state,
            "identity_verified": bool(self.identity_hash and self.identity_stable),
            "identity_stable": self.identity_stable,
            "identity_ref": self.identity_hash[:16] if self.identity_hash else None,
            "acquired_at": self.acquired_at,
            "last_verified_at": self.verified_at,
            "source": self.source,
            "token_present": bool(self.token.get_secret_value()),
            "expiry_known": False,
            "credential_generation": self.credential_generation,
            "credential_bound": bool(self.account_hash and self.credential_generation is not None),
            "account_ref": self.account_hash[:16] if self.account_hash else None,
        }

    def secret_payload(self) -> dict:
        data = self.model_dump(mode="json", exclude={"token"})
        data["token"] = self.token.get_secret_value()
        return data


class LibraryStore:
    """Separate file, lock and generation; clear/rebind invalidate late writers."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "library-self.dpapi"
        self.lock_path = directory / "library-session.lock"
        self.generation_path = directory / "library-generation"

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise LibraryError("unsupported", "图书馆座位会话存储只支持 Windows DPAPI。")
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
                        raise LibraryError(
                            "local_error",
                            "本地图书馆座位会话正在更新，请稍后重试。",
                        ) from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self) -> LibrarySession | None:
        if not self.path.exists():
            return None
        try:
            session = LibrarySession.model_validate_json(unprotect(self.path.read_bytes()))
            if session.binding_id is None:
                session.binding_id = session.revision
            return session
        except LibraryError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise LibraryError(
                "local_error", "本地图书馆座位会话文件损坏，请重新采用。"
            ) from None

    def load(self) -> LibrarySession | None:
        with self.locked():
            return self._read()

    def snapshot(self) -> tuple[LibrarySession | None, int]:
        with self.locked():
            return self._read(), self._read_generation()

    def _read_generation(self) -> int:
        if not self.generation_path.exists():
            return 0
        try:
            value = int(self.generation_path.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            raise LibraryError("local_error", "本地图书馆座位会话代次损坏。") from None
        if value < 0:
            raise LibraryError("local_error", "本地图书馆座位会话代次损坏。")
        return value

    def _write_generation(self, value: int) -> None:
        fd, name = tempfile.mkstemp(prefix=".library-generation-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(str(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.generation_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def generation(self) -> int:
        with self.locked():
            return self._read_generation()

    def _write(self, session: LibrarySession) -> None:
        if session.binding_id is None:
            session.binding_id = session.revision
        payload = json.dumps(session.secret_payload(), ensure_ascii=False).encode("utf-8")
        encrypted = protect(payload)
        fd, name = tempfile.mkstemp(prefix=".library-session-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def replace(
        self,
        session: LibrarySession,
        *,
        expected_revision: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        with self.locked():
            generation = self._read_generation()
            if expected_generation is not None and generation != expected_generation:
                raise LibraryError("auth_required", "图书馆座位会话代次已变化，本次旧结果已丢弃。")
            current = self._read()
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise LibraryError(
                    "auth_required", "图书馆座位会话已清除或更换；本次旧结果已丢弃。"
                )
            self._write(session)
            self._write_generation(generation + 1)

    def finish(self, revision: str, state: str = "stored") -> None:
        with self.locked():
            current = self._read()
            if current is None or current.revision != revision:
                raise LibraryError(
                    "auth_required", "图书馆座位会话已清除或更换；本次旧结果已丢弃。"
                )
            current.state = state  # type: ignore[assignment]
            if state == "stored":
                current.verified_at = now()
            self._write(current)

    def mark_expired(self, revision: str) -> None:
        with self.locked():
            current = self._read()
            if current is None or current.revision != revision:
                return
            current.state = "auth_expired"
            self._write(current)

    def clear(self) -> None:
        with self.locked():
            generation = self._read_generation()
            self.path.unlink(missing_ok=True)
            self._write_generation(generation + 1)

    @staticmethod
    def candidate(path: Path) -> tuple[Literal["seat_token", "app_token"], str]:
        if (
            not path.is_file()
            or path.suffix.lower() != ".dpapi"
            or path.stat().st_size > MAX_CANDIDATE_BYTES
        ):
            raise LibraryError("invalid_input", "请选择本机 DPAPI 加密的图书馆座位候选文件。")
        try:
            payload = json.loads(unprotect(path.read_bytes()))
        except LibraryError:
            raise
        except (ValueError, TypeError):
            raise LibraryError("invalid_input", "图书馆座位候选格式不受支持。") from None
        if not isinstance(payload, dict):
            raise LibraryError("invalid_input", "图书馆座位候选必须是 JSON 对象。")
        kind = payload.get("kind")
        token = payload.get("token")
        if kind not in {"seat_token", "app_token"}:
            raise LibraryError("invalid_input", "候选必须明确 kind 为 seat_token 或 app_token。")
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise LibraryError("invalid_input", "候选中的 token 格式无效。")
        return kind, token


class LibraryAuthLock:
    """Cross-process single-flight guard for browser/manual adoption."""

    def __init__(self, directory: Path, timeout: float = 30.0):
        self.directory = directory
        self.path = directory / "library-auth.lock"
        self.timeout = timeout

    def acquire(self) -> _HeldLock:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise LibraryError("unsupported", "图书馆座位认证锁只支持 Windows。")
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
                    return _HeldLock(handle)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LibraryError(
                            "auth_in_progress",
                            "另一次图书馆座位认证正在进行，本次未发起新的认证。",
                        ) from None
                    time.sleep(0.05)
        except Exception:
            handle.close()
            raise

    @contextmanager
    def held(self):
        held = self.acquire()
        try:
            yield
        finally:
            held.release()


class _HeldLock:
    def __init__(self, handle):
        self.handle = handle
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


class LibraryBookingLock:
    """Cross-process single-flight guard for the single write endpoint.

    The lock is acquired and released in worker threads, so the event loop never
    waits on the file system. The caller may keep the returned handle across
    awaits; cancellation must await the real POST before releasing it.
    """

    def __init__(self, directory: Path, timeout: float = 10.0):
        self.directory = directory
        self.path = directory / "library-booking.lock"
        self.timeout = timeout

    def acquire(self) -> _HeldLock:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise LibraryError("unsupported", "图书馆座位预约锁只支持 Windows。")
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
                    return _HeldLock(handle)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LibraryError(
                            "booking_in_progress",
                            "另一次图书馆座位预约正在提交，本次未发起预约。",
                        ) from None
                    time.sleep(0.05)
        except Exception:
            handle.close()
            raise

    @contextmanager
    def held(self):
        held = self.acquire()
        try:
            yield
        finally:
            held.release()
