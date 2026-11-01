"""Small cross-process file locks used by the local auto-auth coordinator.

All blocking acquisition happens in worker threads at call sites. A caller that
is cancelled while waiting must release a late acquisition instead of leaking it.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import AutoAuthError

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class FileLock:
    """A one-byte msvcrt lock with an explicit owner handle."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self._handle = None
        self._thread_lock: threading.Lock | None = None
        self._released = False

    def acquire(self) -> FileLock:
        if self._handle is not None:
            raise AutoAuthError("local_error", "同一认证锁不能重复获取。")
        self._released = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            raise AutoAuthError("unsupported", "本地自动认证锁只支持 Windows。")
        import msvcrt

        thread_lock = _thread_lock(self.path)
        if not thread_lock.acquire(timeout=self.timeout):
            raise AutoAuthError("auth_in_progress", "另一次本地认证正在进行，等待超时。")
        handle = None
        try:
            handle = self.path.open("a+b")
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    self._handle = handle
                    self._thread_lock = thread_lock
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise AutoAuthError(
                            "auth_in_progress", "另一次本地认证正在进行，等待超时。"
                        ) from None
                    time.sleep(0.02)
        except AutoAuthError:
            if handle is not None:
                handle.close()
            thread_lock.release()
            raise
        except Exception:
            if handle is not None:
                handle.close()
            thread_lock.release()
            raise

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        import msvcrt

        try:
            if self._handle is not None:
                self._handle.seek(0)
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    self._handle.close()
                    self._handle = None
        finally:
            if self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_lock = None

    @contextmanager
    def held(self):
        self.acquire()
        try:
            yield self
        finally:
            self.release()
