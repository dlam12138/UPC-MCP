import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .config import default_state_dir  # noqa: F401 (historical import compatibility)
from .errors import UPCError
from .models import Session, now


def protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise UPCError("unsupported", "当前凭据存储只支持 Windows DPAPI。")
    import win32crypt

    return win32crypt.CryptProtectData(data, "UPC MCP session", None, None, None, 1)


def unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise UPCError("unsupported", "当前凭据存储只支持 Windows DPAPI。")
    import win32crypt

    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
    except Exception:
        raise UPCError("auth_required", "无法解密本机授权资料，请重新完成本地授权。") from None


class SessionStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "jhzf-self.dpapi"

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise UPCError("unsupported", "当前凭据存储只支持 Windows DPAPI。")
        import msvcrt

        with (self.directory / "session.lock").open("a+b") as handle:
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
                        raise UPCError("local_error", "本地会话正在更新，请稍后重试。") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self) -> Session | None:
        if not self.path.exists():
            return None
        try:
            return Session.model_validate_json(unprotect(self.path.read_bytes()))
        except UPCError:
            raise
        except Exception:
            raise UPCError("local_error", "本地会话文件损坏，请重新授权。") from None

    def load(self) -> Session | None:
        with self.locked():
            return self._read()

    def _write(self, session: Session):
        data = session.model_dump(mode="json", exclude={"cookie"})
        data["cookie"] = session.cookie.get_secret_value()
        encrypted = protect(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        fd, name = tempfile.mkstemp(prefix=".session-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def replace(self, session: Session):
        with self.locked():
            self._write(session)

    def finish(self, revision: str, state: str = "stored"):
        """CAS under a cross-process lock: logout/rebinding cannot be undone by a late reply."""
        with self.locked():
            current = self._read()
            if current is None or current.revision != revision:
                raise UPCError("auth_required", "授权已清除或更换，本次旧会话结果已丢弃。")
            current.state = state
            if state == "stored":
                current.verified_at = now()
            self._write(current)

    def clear(self):
        with self.locked():
            self.path.unlink(missing_ok=True)
