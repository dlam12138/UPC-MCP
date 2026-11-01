"""Independent DPAPI-backed session storage for Shiguang.

The JHZF store is never touched. Cookie values are encrypted at rest and never
included in public summaries or MCP/CLI results.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from http.cookiejar import Cookie as HttpCookie
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .shiguang import ShiguangError

MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
CAS_DOMAIN = "cas.upc.edu.cn"
DEKT_DOMAIN = "dekt.upc.edu.cn"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise ShiguangError("unsupported", "石光会话存储只支持 Windows DPAPI。")
    import win32crypt

    return win32crypt.CryptProtectData(data, "UPC Shiguang session", None, None, None, 1)


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise ShiguangError("unsupported", "石光会话存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
    except Exception:
        raise ShiguangError(
            "auth_required",
            "无法解密本机石光授权资料，请重新完成本地采用。",
        ) from None


class Cookie(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    value: SecretStr
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", max_length=256)
    secure: bool = False
    http_only: bool = False
    expires: int | None = None
    session: bool = True
    same_site: str | None = None


class ShiguangSession(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    binding_id: str | None = None
    cookies: list[Cookie]
    acquired_at: str = Field(default_factory=now)
    verified_at: str | None = None
    state: Literal["stored", "unverified", "auth_expired"] = "unverified"
    identity_hash: str | None = None
    identity_verified: bool = False
    credential_generation: int | None = None
    account_hash: str | None = None
    source: Literal[
        "adopted_dpapi",
        "cas_sso_recovery",
        "auto_auth_sso",
        "auto_auth_password",
    ]

    def effective_binding_id(self) -> str:
        # Old DPAPI files predate binding_id; revision is stable for those files.
        return self.binding_id or self.revision

    def has_cas_sso(self) -> bool:
        return any(
            cookie.name == "CASTGC" and cookie.domain.lstrip(".") == CAS_DOMAIN
            for cookie in self.cookies
        )

    def has_dekt_session(self) -> bool:
        return any(
            cookie.name == "SESSION" and cookie.domain.lstrip(".") == DEKT_DOMAIN
            for cookie in self.cookies
        )

    def public(self) -> dict:
        return {
            "session_ref": "self",
            "state": self.state,
            "identity_verified": self.identity_verified,
            "cookie_count": len(self.cookies),
            "acquired_at": self.acquired_at,
            "last_verified_at": self.verified_at,
            "source": self.source,
            "conditional_cas_sso": self.has_cas_sso(),
            "dekt_session_present": self.has_dekt_session(),
            "credential_generation": self.credential_generation,
            "credential_bound": bool(self.account_hash and self.credential_generation is not None),
            "account_ref": self.account_hash[:16] if self.account_hash else None,
        }

    def secret_payload(self) -> dict:
        data = self.model_dump(mode="json", exclude={"cookies"})
        data["cookies"] = [
            {
                **cookie.model_dump(mode="json", exclude={"value"}),
                "value": cookie.value.get_secret_value(),
            }
            for cookie in self.cookies
        ]
        return data


class ShiguangStore:
    """Separate file and lock; JHZF's jhzf-self.dpapi is never read or written."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "shiguang-self.dpapi"
        self.lock_path = directory / "shiguang-session.lock"
        self.generation_path = directory / "shiguang-generation"
        self.backoff_path = directory / "shiguang-recovery-state.json"

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise ShiguangError("unsupported", "石光会话存储只支持 Windows DPAPI。")
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
                        raise ShiguangError(
                            "local_error",
                            "本地石光会话正在更新，请稍后重试。",
                        ) from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self) -> ShiguangSession | None:
        if not self.path.exists():
            return None
        try:
            session = ShiguangSession.model_validate_json(unprotect(self.path.read_bytes()))
            if session.binding_id is None:
                session.binding_id = session.revision
            return session
        except ShiguangError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise ShiguangError("local_error", "本地石光会话文件损坏，请重新采用。") from None

    def load(self) -> ShiguangSession | None:
        with self.locked():
            return self._read()

    def snapshot(self) -> tuple[ShiguangSession | None, int]:
        """Read session and generation under one lock so clear/rebind cannot split them."""
        with self.locked():
            return self._read(), self._read_generation()

    def _read_generation(self) -> int:
        if not self.generation_path.exists():
            return 0
        try:
            value = int(self.generation_path.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            raise ShiguangError("local_error", "本地石光会话代次损坏。") from None
        if value < 0:
            raise ShiguangError("local_error", "本地石光会话代次损坏。")
        return value

    def _write_generation(self, value: int) -> None:
        fd, name = tempfile.mkstemp(prefix=".shiguang-generation-", dir=self.directory)
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

    def _backoff_active_unlocked(self) -> bool:
        if not self.backoff_path.exists():
            return False
        try:
            data = json.loads(self.backoff_path.read_text(encoding="utf-8"))
            until = float(data.get("until", 0))
            generation = int(data.get("generation", -1))
        except (OSError, ValueError, TypeError):
            return False
        return until > time.time() and generation == self._read_generation()

    def is_backoff_active(self) -> bool:
        with self.locked():
            return self._backoff_active_unlocked()

    def set_backoff(self, seconds: float = 15.0) -> None:
        with self.locked():
            payload = {
                "until": time.time() + max(1.0, seconds),
                "generation": self._read_generation(),
                "reason": "auth_required",
            }
            fd, name = tempfile.mkstemp(
                prefix=".shiguang-backoff-", dir=self.directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(payload, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.backoff_path)
            finally:
                Path(name).unlink(missing_ok=True)

    def _clear_backoff_unlocked(self) -> None:
        self.backoff_path.unlink(missing_ok=True)

    def _write(self, session: ShiguangSession) -> None:
        if session.binding_id is None:
            session.binding_id = session.revision
        payload = json.dumps(session.secret_payload(), ensure_ascii=False).encode("utf-8")
        encrypted = protect(payload)
        fd, name = tempfile.mkstemp(prefix=".shiguang-session-", dir=self.directory)
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
        session: ShiguangSession,
        expected_revision: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        with self.locked():
            generation = self._read_generation()
            if expected_generation is not None and generation != expected_generation:
                raise ShiguangError("auth_required", "石光会话代次已变化，本次旧结果已丢弃。")
            current = self._read()
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise ShiguangError("auth_required", "石光会话已清除或更换，本次旧结果已丢弃。")
            self._write(session)
            self._write_generation(generation + 1)
            self._clear_backoff_unlocked()

    def finish(self, revision: str, state: str = "stored") -> None:
        with self.locked():
            current = self._read()
            if current is None or current.revision != revision:
                raise ShiguangError("auth_required", "石光会话已清除或更换，本次旧结果已丢弃。")
            current.state = state  # type: ignore[assignment]
            if state == "stored":
                current.verified_at = now()
            self._write(current)
            self._clear_backoff_unlocked()

    def clear(self) -> None:
        with self.locked():
            generation = self._read_generation()
            self.path.unlink(missing_ok=True)
            self._write_generation(generation + 1)
            self._clear_backoff_unlocked()

    @staticmethod
    def candidate(path: Path) -> ShiguangSession:
        if (
            not path.is_file()
            or path.suffix.lower() != ".dpapi"
            or path.stat().st_size > MAX_CANDIDATE_BYTES
        ):
            raise ShiguangError("invalid_input", "请选择本机 DPAPI 加密的石光会话候选文件。")
        try:
            payload = json.loads(unprotect(path.read_bytes()))
        except ShiguangError:
            raise
        except (ValueError, TypeError):
            raise ShiguangError("invalid_input", "石光会话候选格式不受支持。") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
            raise ShiguangError("invalid_input", "石光会话候选缺少 cookies。")
        cookies: list[Cookie] = []
        seen: set[tuple[str, str, str]] = set()
        for raw in payload["cookies"]:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            value = raw.get("value")
            domain = (raw.get("domain") or "").lstrip(".")
            if not isinstance(name, str) or not name or not isinstance(value, str):
                continue
            if not value or name in {"HttpOnly", "Secure"}:
                continue
            if not isinstance(domain, str) or domain not in {DEKT_DOMAIN, CAS_DOMAIN}:
                continue
            key = (name, domain, raw.get("path") or "/")
            if key in seen:
                continue
            seen.add(key)
            expires = raw.get("expires")
            if not isinstance(expires, int) or expires <= 0:
                expires = None
            try:
                cookies.append(
                    Cookie(
                        name=name,
                        value=SecretStr(value),
                        domain=domain,
                        path=raw.get("path") or "/",
                        secure=bool(raw.get("secure")),
                        http_only=bool(raw.get("httpOnly")),
                        expires=expires,
                        session=bool(raw.get("session", expires is None)),
                        same_site=(
                            raw.get("sameSite")
                            if isinstance(raw.get("sameSite"), str)
                            else None
                        ),
                    )
                )
            except ValidationError:
                raise ShiguangError("invalid_input", "石光会话候选 cookie 字段无效。") from None
        if not any(c.name == "SESSION" and c.domain == DEKT_DOMAIN for c in cookies):
            raise ShiguangError("auth_required", "候选文件中没有石光 SESSION。")
        return ShiguangSession(cookies=cookies, state="unverified", source="adopted_dpapi")


class RecoveryLock:
    """Cross-process single-flight lock for CAS recovery only.

    It is deliberately separate from the short store lock; it may be held while
    the CAS/network work runs. Waiters get a bounded, structured failure.
    """

    def __init__(self, directory: Path, timeout: float = 35.0):
        self.directory = directory
        self.path = directory / "shiguang-recovery.lock"
        self.timeout = timeout

    @contextmanager
    def held(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise ShiguangError("unsupported", "石光恢复锁只支持 Windows。")
        import msvcrt

        with self.path.open("a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ShiguangError(
                            "recovery_in_progress",
                            "另一次石光恢复正在进行，等待超时；本次未发起新的恢复。",
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class RegistrationLock:
    """Cross-process single-flight guard for a registration submission."""

    def __init__(self, directory: Path, timeout: float = 10.0):
        self.directory = directory
        self.path = directory / "shiguang-registration.lock"
        self.timeout = timeout

    @contextmanager
    def held(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise ShiguangError("unsupported", "石光报名锁只支持 Windows。")
        import msvcrt

        with self.path.open("a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ShiguangError(
                            "registration_in_progress",
                            "另一次石光报名正在提交，本次未发起报名。",
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def jar_from_cookies(cookies: list[Cookie], domains: set[str] | None = None) -> httpx.Cookies:
    """Build a domain/path/secure-aware jar; cross-domain cookies are not merged."""
    jar = CookieJar()
    for cookie in cookies:
        domain = cookie.domain.lstrip(".")
        if domains is not None and domain not in domains:
            continue
        rest = {"HttpOnly": None} if cookie.http_only else {}
        if cookie.same_site:
            rest["SameSite"] = cookie.same_site
        jar.set_cookie(
            HttpCookie(
                version=0,
                name=cookie.name,
                value=cookie.value.get_secret_value(),
                port=None,
                port_specified=False,
                domain=cookie.domain,
                domain_specified=True,
                domain_initial_dot=cookie.domain.startswith("."),
                path=cookie.path,
                path_specified=True,
                secure=cookie.secure,
                expires=cookie.expires,
                discard=cookie.session,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
        )
    result = httpx.Cookies()
    result.jar = jar
    return result


def cookies_from_jar(jar: httpx.Cookies) -> list[Cookie]:
    result: list[Cookie] = []
    for raw in jar.jar:
        if not raw.name or raw.name in {"HttpOnly", "Secure"}:
            continue
        if not isinstance(raw.value, str) or not raw.value:
            continue
        same_site = None
        if raw._rest:
            same_site = raw._rest.get("SameSite") or raw._rest.get("samesite")
        result.append(
            Cookie(
                name=raw.name,
                value=SecretStr(raw.value),
                domain=(raw.domain or "").lstrip("."),
                path=raw.path or "/",
                secure=bool(raw.secure),
                http_only=bool(
                    raw.has_nonstandard_attr("HttpOnly")
                    or raw.has_nonstandard_attr("httponly")
                ),
                expires=raw.expires if raw.expires and raw.expires > 0 else None,
                session=bool(raw.discard),
                same_site=same_site if isinstance(same_site, str) else None,
            )
        )
    return result


def identity_hash_from_html(html: str) -> str | None:
    """Optional local identity check; never returns or logs the raw value."""
    import hashlib

    from .shiguang import parse_html

    root = parse_html(html)
    for node in root.find_all("input"):
        name = (node.attrs.get("name") or "").lower()
        value = node.attrs.get("value") or ""
        if name in {"currid", "userid", "currentuserid", "studentid", "currname"} and value:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return None
