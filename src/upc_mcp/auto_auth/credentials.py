"""Local DPAPI CurrentUser credential storage for unified CAS login.

The username and password exist only while a local configure/login operation is
in progress. At rest they are DPAPI-protected; metadata carries only a generation
and an irreversible account hash. All size, lock, atomic-write and ACL checks are
local and never depend on the research prototype.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import AutoAuthError

ENTROPY = b"UPC-AutoAuth-v1"
MAX_CREDENTIAL_BYTES = 16 * 1024
MAX_META_BYTES = 4 * 1024


def account_hash(username: str) -> str:
    raw = username.strip().encode("utf-8")
    return hashlib.sha256(b"upc-auto-auth\0" + raw).hexdigest()


@dataclass(frozen=True)
class Binding:
    configured: bool
    generation: int
    account_hash: str | None

    def account_ref(self) -> str | None:
        return self.account_hash[:16] if self.account_hash else None

    def matches(self, other: Binding) -> bool:
        return (
            self.configured == other.configured
            and self.generation == other.generation
            and self.account_hash == other.account_hash
        )


@dataclass
class Credential:
    username: str
    password: str
    binding: Binding

    def __repr__(self) -> str:
        return f"Credential(configured=True, generation={self.binding.generation})"


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise AutoAuthError("unsupported", "统一凭据存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptProtectData(
            data, "UPC unified auto-auth credential", ENTROPY, None, None, 1
        )
    except Exception:
        raise AutoAuthError("local_error", "无法加密本机统一凭据；未写入任何明文。") from None


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise AutoAuthError("unsupported", "统一凭据存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, ENTROPY, None, None, 1)[1]
    except Exception:
        raise AutoAuthError(
            "credential_unreadable",
            "本机统一凭据无法解密；请在本机重新配置。",
            action="credential_configure",
        ) from None


def _current_user() -> str:
    try:
        import win32api

        return win32api.GetUserNameEx(2)
    except Exception:
        return os.environ.get("USERNAME", "")


def _restrict(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        return
    user = _current_user()
    if not user:
        raise AutoAuthError("local_error", "无法确定当前 Windows 用户，未写入凭据。")
    grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
    try:
        result = subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", grant],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise AutoAuthError("local_error", "无法限制本机凭据文件权限。") from None
    if result.returncode != 0:
        raise AutoAuthError("local_error", "无法限制本机凭据文件权限。")


class CredentialStore:
    """Credential and generation storage under ``state_dir/auto-auth``."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "credential.dpapi"
        self.meta_path = directory / "credential-meta.json"
        self.lock_path = directory / "credential.lock"
        self.guard_path = directory / "generation.lock"
        directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _file_lock(self, path: Path):
        from .locks import FileLock

        with FileLock(path, timeout=5.0).held():
            yield

    @contextmanager
    def guard(self):
        """Hold the shared credential-generation lock.

        Callers may verify a Binding and then write a session while this lock is
        held, making generation check + writeback atomic with credential
        configure/clear/rebind.
        """
        with self._file_lock(self.guard_path):
            yield

    def _read_meta(self) -> dict:
        if not self.meta_path.exists():
            return {"version": 1, "generation": 0, "account_hash": None}
        try:
            if self.meta_path.stat().st_size > MAX_META_BYTES:
                raise ValueError
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError
            generation = data.get("generation")
            account = data.get("account_hash")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError
            if account is not None and (
                not isinstance(account, str)
                or len(account) != 64
                or any(ch not in "0123456789abcdef" for ch in account)
            ):
                raise ValueError
            return data
        except (OSError, ValueError, TypeError):
            raise AutoAuthError(
                "credential_unreadable",
                "本机统一凭据代次元数据损坏；未尝试登录。",
                action="credential_configure",
            ) from None

    def _binding_locked(self) -> Binding:
        meta = self._read_meta()
        configured = self.path.is_file()
        if configured and not meta.get("account_hash"):
            raise AutoAuthError(
                "credential_unreadable",
                "本机统一凭据缺少账户绑定元数据；未尝试登录。",
                action="credential_configure",
            )
        if not configured and meta.get("account_hash"):
            raise AutoAuthError(
                "credential_unreadable",
                "本机统一凭据文件缺失但绑定仍在；未尝试登录。",
                action="credential_configure",
            )
        return Binding(
            configured=configured,
            generation=int(meta["generation"]),
            account_hash=meta.get("account_hash"),
        )

    def snapshot(self) -> Binding:
        with self._file_lock(self.lock_path):
            return self._binding_locked()

    def status(self) -> dict:
        binding = self.snapshot()
        return {
            "configured": binding.configured,
            "generation": binding.generation,
            "account_ref": binding.account_ref(),
        }

    def snapshot_locked(self) -> Binding:
        """Read a binding while the caller already holds ``guard()``."""
        return self._binding_locked()

    def load(self) -> Credential:
        with self._file_lock(self.lock_path):
            binding = self._binding_locked()
            if not binding.configured:
                raise AutoAuthError(
                    "credential_missing",
                    "本机尚未配置统一账号密码。",
                    action="credential_configure",
                )
            try:
                if self.path.stat().st_size > MAX_CREDENTIAL_BYTES:
                    raise ValueError
                payload = json.loads(_unprotect(self.path.read_bytes()))
                if not isinstance(payload, dict) or payload.get("version") != 1:
                    raise ValueError
                username = payload.get("username")
                password = payload.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError
                username = username.strip()
                if not 1 <= len(username) <= 128 or not 1 <= len(password) <= 512:
                    raise ValueError
            except AutoAuthError:
                raise
            except (OSError, ValueError, TypeError):
                raise AutoAuthError(
                    "credential_unreadable",
                    "本机统一凭据格式无效；请重新配置。",
                    action="credential_configure",
                ) from None
            if (
                account_hash(username) != binding.account_hash
                or payload.get("generation") != binding.generation
                or payload.get("account_hash") != binding.account_hash
            ):
                raise AutoAuthError(
                    "credential_unreadable",
                    "本机统一凭据与账户绑定不一致；请重新配置。",
                    action="credential_configure",
                )
            return Credential(username=username, password=password, binding=binding)

    def _write(self, username: str, password: str, generation: int) -> Binding:
        if not isinstance(username, str) or not isinstance(password, str):
            raise AutoAuthError("invalid_input", "用户名和密码必须为字符串。")
        if not 1 <= len(username) <= 128 or not 1 <= len(password) <= 512:
            raise AutoAuthError("invalid_input", "用户名或密码长度超出本地限制。")
        account = account_hash(username)
        payload = json.dumps(
            {
                "version": 1, "username": username, "password": password,
                "generation": generation, "account_hash": account,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        encrypted = _protect(payload)
        _restrict(self.directory, directory=True)
        fd, name = tempfile.mkstemp(prefix=".credential-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
        _restrict(self.path)
        meta = {"version": 1, "generation": generation, "account_hash": account}
        fd, name = tempfile.mkstemp(prefix=".credential-meta-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(meta, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.meta_path)
        finally:
            Path(name).unlink(missing_ok=True)
        _restrict(self.meta_path)
        return Binding(configured=True, generation=generation, account_hash=account)

    def store(self, username: str, password: str) -> Binding:
        with self.guard():
            return self.store_locked(username, password)

    def store_locked(
        self, username: str, password: str, *, expected: Binding | None = None
    ) -> Binding:
        """Write while the caller holds guard(), including dependent cleanup."""
        if not isinstance(username, str) or not isinstance(password, str):
            raise AutoAuthError("invalid_input", "用户名和密码必须为字符串。")
        username = username.strip()
        if not username or not password:
            raise AutoAuthError("invalid_input", "用户名和密码不能为空。")
        if len(username) > 128 or len(password) > 512:
            raise AutoAuthError("invalid_input", "用户名或密码长度超出本地限制。")
        with self._file_lock(self.lock_path):
            current = self._binding_locked()
            if expected is not None and not current.matches(expected):
                raise AutoAuthError("credential_changed", "配置期间凭据已变化；未覆盖。")
            return self._write(username, password, current.generation + 1)

    def clear(self) -> Binding:
        with self.guard():
            return self.clear_locked()

    def clear_locked(self) -> Binding:
        """Clear while the caller holds guard()."""
        with self._file_lock(self.lock_path):
            meta = self._read_meta()
            generation = int(meta["generation"]) + 1
            self.path.unlink(missing_ok=True)
            meta = {"version": 1, "generation": generation, "account_hash": None}
            fd, name = tempfile.mkstemp(prefix=".credential-meta-", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(meta, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.meta_path)
            finally:
                Path(name).unlink(missing_ok=True)
            _restrict(self.meta_path)
            return Binding(configured=False, generation=generation, account_hash=None)

    def adopt_from(self, source: Path) -> Binding:
        """Adopt the already-authorized research DPAPI credential once.

        Refuses to overwrite any existing production credential. The source file
        and its sibling metadata are never modified.
        """
        expected = self.snapshot()
        if expected.configured:
            raise AutoAuthError(
                "already_configured",
                "生产状态目录已有统一凭据；未覆盖，请先显式 clear。",
                action="credential_clear",
            )
        source = source.resolve()
        try:
            valid = (
                source.is_file()
                and source.suffix.lower() == ".dpapi"
                and source.stat().st_size <= MAX_CREDENTIAL_BYTES
            )
        except OSError:
            valid = False
        if not valid:
            raise AutoAuthError("invalid_input", "请选择本机 DPAPI 研究凭据文件。")
        try:
            payload = json.loads(_unprotect(source.read_bytes()))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError
            username = payload.get("username")
            password = payload.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError
        except AutoAuthError:
            raise
        except (OSError, ValueError, TypeError):
            raise AutoAuthError("invalid_input", "研究凭据候选格式无效。") from None
        username = username.strip()
        digest = account_hash(username)
        sibling = source.with_name("credential-meta.json")
        if sibling.is_file():
            try:
                if sibling.stat().st_size > MAX_META_BYTES:
                    raise ValueError
                meta = json.loads(sibling.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    raise ValueError
                if meta.get("account_hash") not in (None, digest):
                    raise AutoAuthError(
                        "candidate_mismatch",
                        "研究凭据与账户哈希元数据不一致；未采用。",
                    )
            except AutoAuthError:
                raise
            except (OSError, ValueError, TypeError):
                raise AutoAuthError(
                    "invalid_input", "研究凭据的账户哈希元数据无效。"
                ) from None
        if not username or not password:
            raise AutoAuthError("invalid_input", "研究凭据中的用户名或密码为空。")
        with self.guard():
            with self._file_lock(self.lock_path):
                current = self._binding_locked()
                if not current.matches(expected):
                    raise AutoAuthError(
                        "credential_changed",
                        "采用期间凭据已更换或清除；本次采用已丢弃。",
                    )
                generation = int(self._read_meta()["generation"]) + 1
                return self._write(username, password, generation)
