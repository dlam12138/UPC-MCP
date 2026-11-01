"""Independent DPAPI session storage for e站通 (service.upc.edu.cn).

This module never imports or reads Shiguang, timetable, library or research
session files. It keeps its own file, generation, lock and DPAPI description.
Only service-host cookies with their original scope are accepted.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from http.cookiejar import Cookie
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from .service_portal_protocol import SERVICE_HOST, ServicePortalError

MAX_SESSION_BYTES = 256 * 1024
MAX_GENERATION_BYTES = 64
_COOKIE_NAME = r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise ServicePortalError("unsupported", "e站通会话存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptProtectData(
            data, "UPC Service Portal session", None, None, None, 1
        )
    except Exception:
        raise ServicePortalError("local_error", "无法加密本机 e站通会话；未写入明文。") from None


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise ServicePortalError("unsupported", "e站通会话存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
    except Exception:
        raise ServicePortalError(
            "auth_required",
            "无法解密本机 e站通会话；请清除后重新自动认证。",
            action="service_portal_clear",
        ) from None


class ServicePortalCookie(BaseModel):
    """A service-host cookie, preserving domain/path/secure/httpOnly/expiry."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    value: SecretStr
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=1024)
    secure: bool = False
    http_only: bool = False
    host_only: bool = False
    same_site: Literal["Strict", "Lax", "None"] | None = None
    expires_at: float | None = None

    @field_validator("expires_at")
    @classmethod
    def _expires(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("invalid cookie expiry")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        import re

        if not re.fullmatch(_COOKIE_NAME, value):
            raise ValueError("invalid cookie name")
        return value

    @field_validator("value")
    @classmethod
    def _value(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) > 8192 or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
            raise ValueError("invalid cookie value")
        return value

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        domain = value.strip().lower().lstrip(".")
        if domain != SERVICE_HOST:
            raise ValueError("cookie domain must be exactly service.upc.edu.cn")
        return SERVICE_HOST

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/") or any(ch in value for ch in "\r\n\x00"):
            raise ValueError("invalid cookie path")
        return value

    def public(self) -> dict:
        return {
            "name": self.name,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "http_only": self.http_only,
            "host_only": self.host_only,
            "expires_known": self.expires_at is not None,
        }


class ServicePortalSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    cookies: list[ServicePortalCookie] = Field(default_factory=list, repr=False)
    acquired_at: str = Field(default_factory=now)
    verified_at: str | None = None
    state: Literal["stored", "unverified", "auth_expired"] = "unverified"
    source: Literal["auto_auth_sso", "auto_auth_password", "adopted_candidate"] = (
        "adopted_candidate"
    )
    identity_verified: bool = False
    identity_stable: bool = False
    credential_generation: int | None = None
    account_hash: str | None = None

    @field_validator("cookies")
    @classmethod
    def _cookies(cls, value: list[ServicePortalCookie]) -> list[ServicePortalCookie]:
        if not value or len(value) > 256:
            raise ValueError("invalid cookie count")
        keys = [(cookie.name, cookie.domain, cookie.path) for cookie in value]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate cookie scope")
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
            "cookie_names": sorted({cookie.name for cookie in self.cookies}),
            "expiry_known": any(cookie.expires_at is not None for cookie in self.cookies),
            "credential_generation": self.credential_generation,
            "credential_bound": bool(
                self.account_hash is not None and self.credential_generation is not None
            ),
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


class ServicePortalStore:
    """Separate file, lock and generation; stale recovery results are rejected."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "service-portal-self.dpapi"
        self.lock_path = directory / "service-portal-session.lock"
        self.generation_path = directory / "service-portal-generation"

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise ServicePortalError("unsupported", "e站通会话存储只支持 Windows DPAPI。")
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
                        raise ServicePortalError(
                            "local_error", "本地 e站通会话正在更新，请稍后重试。"
                        ) from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self) -> ServicePortalSession | None:
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > MAX_SESSION_BYTES:
                raise ValueError
            with self.path.open("rb") as source:
                encrypted = source.read(MAX_SESSION_BYTES + 1)
            if len(encrypted) > MAX_SESSION_BYTES:
                raise ValueError
            return ServicePortalSession.model_validate_json(
                unprotect(encrypted)
            )
        except ServicePortalError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise ServicePortalError(
                "auth_required",
                "本地 e站通会话文件损坏；请清除后重新自动认证。",
                action="service_portal_clear",
            ) from None

    def _read_generation(self) -> int:
        if not self.generation_path.exists():
            return 0
        try:
            if self.generation_path.stat().st_size > MAX_GENERATION_BYTES:
                raise ValueError
            with self.generation_path.open("rb") as source:
                raw = source.read(MAX_GENERATION_BYTES + 1)
            if len(raw) > MAX_GENERATION_BYTES:
                raise ValueError
            text = raw.decode("ascii").strip()
            if not text or not text.isdecimal():
                raise ValueError
            value = int(text)
        except (OSError, ValueError):
            raise ServicePortalError("local_error", "本地 e站通会话代次损坏。") from None
        if value < 0:
            raise ServicePortalError("local_error", "本地 e站通会话代次损坏。")
        return value

    def _write_generation(self, value: int) -> None:
        fd, name = tempfile.mkstemp(prefix=".service-portal-generation-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(str(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.generation_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _write(self, session: ServicePortalSession) -> None:
        payload = json.dumps(session.secret_payload(), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_SESSION_BYTES:
            raise ServicePortalError("local_error", "e站通会话超过本地大小限制。")
        encrypted = protect(payload)
        if len(encrypted) > MAX_SESSION_BYTES:
            raise ServicePortalError("local_error", "e站通加密会话超过本地大小限制。")
        fd, name = tempfile.mkstemp(prefix=".service-portal-session-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self) -> ServicePortalSession | None:
        with self.locked():
            return self._read()

    def snapshot(self) -> tuple[ServicePortalSession | None, int]:
        with self.locked():
            return self._read(), self._read_generation()

    def generation(self) -> int:
        with self.locked():
            return self._read_generation()

    def replace(
        self,
        session: ServicePortalSession,
        *,
        expected_revision: str | None = None,
        expected_generation: int | None = None,
    ) -> None:
        with self.locked():
            generation = self._read_generation()
            if expected_generation is not None and generation != expected_generation:
                raise ServicePortalError(
                    "auth_required",
                    "e站通会话代次已变化；本次旧认证结果已丢弃。",
                )
            current = self._read()
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise ServicePortalError(
                    "auth_required",
                    "e站通会话已清除或更换；本次旧认证结果已丢弃。",
                )
            self._write(session)
            self._write_generation(generation + 1)

    def finish(self, revision: str, state: str = "stored") -> None:
        with self.locked():
            current = self._read()
            if current is None or current.revision != revision:
                raise ServicePortalError(
                    "auth_required", "e站通会话已清除或更换；本次旧验证结果已丢弃。"
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


def jar_from_cookies(cookies: list[ServicePortalCookie]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in cookies:
        rest = {}
        if cookie.http_only:
            rest["HttpOnly"] = None
        if cookie.same_site:
            rest["SameSite"] = cookie.same_site
        jar.jar.set_cookie(Cookie(
            version=0, name=cookie.name, value=cookie.value.get_secret_value(),
            port=None, port_specified=False, domain=cookie.domain,
            domain_specified=not cookie.host_only, domain_initial_dot=False,
            path=cookie.path, path_specified=True, secure=cookie.secure,
            expires=int(cookie.expires_at) if cookie.expires_at is not None else None,
            discard=cookie.expires_at is None, comment=None, comment_url=None,
            rest=rest, rfc2109=False,
        ))
    return jar
