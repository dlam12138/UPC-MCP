"""Local browser/CDP authentication entry for the library seat system.

The normal browser path launches an owned Edge profile and opens only the
official library SSO page. The passive CDP path only reads a libseat page's own
sessionStorage; it never navigates, refreshes or closes the user's tabs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from websockets.asyncio.client import connect

from .library_seat import LOGIN_URL, LibraryError

MAX_TARGETS_BYTES = 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
LIBRARY_HOST = "libspace.upc.edu.cn"
CAPTURE_JS = """
(function () {
  try {
    return JSON.stringify({
      href: String(window.location.href || ''),
      token: String(window.sessionStorage.getItem('libseat-m-token') || '')
    });
  } catch (e) { return ''; }
})()
"""


def local_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError
        return value.rstrip("/")
    except ValueError:
        raise LibraryError(
            "invalid_input", "CDP 端点必须是带显式端口的本机 http 地址，不能含凭据。"
        ) from None


def _safe_ws(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "ws"
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.port is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/devtools/")
        ):
            raise ValueError
        return value
    except ValueError:
        raise LibraryError("upstream_error", "浏览器调试目标地址不在本机范围内。") from None


def _is_library_page(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == LIBRARY_HOST
        and parsed.port in {None, 443}
        and not parsed.username
        and not parsed.password
        and (parsed.path == "/libseat" or parsed.path.startswith("/libseat/"))
    )


def _token_from_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    candidates = [parsed.query]
    if parsed.fragment:
        candidates.append(parsed.fragment.split("?", 1)[-1])
    for candidate in candidates:
        if not candidate:
            continue
        values = parse_qs(candidate, keep_blank_values=False).get("token", [])
        for value in values:
            if value.strip():
                return value
    return None


def _target_ws(target: dict) -> str | None:
    value = target.get("webSocketDebuggerUrl")
    if not isinstance(value, str):
        return None
    try:
        return _safe_ws(value)
    except LibraryError:
        return None


async def _list_targets(endpoint: str) -> list[dict]:
    endpoint = local_endpoint(endpoint)
    try:
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=5
        ) as client:
            response = await client.get(endpoint + "/json/list")
            if response.status_code != 200 or len(response.content) > MAX_TARGETS_BYTES:
                raise LibraryError("auth_required", "无法读取本机浏览器调试目标。")
            data = response.json()
    except LibraryError:
        raise
    except (httpx.HTTPError, ValueError):
        raise LibraryError("auth_required", "无法连接本机浏览器调试入口。") from None
    if not isinstance(data, list):
        raise LibraryError("auth_required", "本机浏览器调试目标格式异常。")
    return [item for item in data if isinstance(item, dict)]


async def _evaluate(ws_url: str) -> dict | None:
    try:
        async with connect(
            ws_url,
            open_timeout=5,
            close_timeout=2,
            max_size=MAX_TARGETS_BYTES,
            ping_interval=None,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": CAPTURE_JS,
                            "returnByValue": True,
                            "awaitPromise": False,
                        },
                    }
                )
            )
            while True:
                raw = await asyncio.wait_for(websocket.recv(), 5)
                message = json.loads(raw)
                if message.get("id") != 1:
                    continue
                if "error" in message or "exceptionDetails" in message.get("result", {}):
                    return None
                value = message.get("result", {}).get("result", {}).get("value")
                if not isinstance(value, str) or not value:
                    return None
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _preferred_token(
    href: str, stored_token: str | None, fallback_url: str
) -> tuple[str, str] | None:
    if _is_library_page(href):
        if isinstance(stored_token, str) and stored_token.strip():
            return "seat_token", stored_token
        token = _token_from_url(href)
        if token:
            return "app_token", token
    token = _token_from_url(fallback_url)
    if token:
        return "app_token", token
    return None


async def _capture_target(target: dict) -> tuple[str, str] | None:
    url = target.get("url")
    if not isinstance(url, str) or not _is_library_page(url):
        return None
    ws_url = _target_ws(target)
    if ws_url is not None:
        value = await _evaluate(ws_url)
        if value:
            href = value.get("href")
            if isinstance(href, str):
                return _preferred_token(href, value.get("token"), url)
    return _preferred_token(url, None, url)


async def adopt_from_cdp(endpoint: str) -> tuple[str, str]:
    """Passive read of an already-open libseat page; no navigation or refresh."""
    targets = await _list_targets(endpoint)
    for target in targets:
        captured = await _capture_target(target)
        if captured is not None:
            return captured
    raise LibraryError(
        "auth_required",
        "本机调试浏览器中没有已登录的 libseat 页面；未导航或刷新任何标签。",
    )


def find_edge() -> str:
    configured = os.environ.get("UPC_MCP_EDGE_PATH")
    candidates = [
        configured,
        shutil.which("msedge.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        str(
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "Edge"
            / "Application"
            / "msedge.exe"
        ),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise LibraryError(
        "unsupported",
        "未找到本机 Edge；请设置 UPC_MCP_EDGE_PATH 或使用被动 CDP 采用。",
        details={"action": "set_edge_path"},
    )


def _terminate_owned_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def _devtools_port(profile_dir: Path, timeout: float = 20.0) -> int:
    path = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                first = path.read_text(encoding="ascii").splitlines()[0].strip()
                port = int(first)
            except (OSError, ValueError, IndexError):
                port = 0
            if 1 <= port <= 65535:
                return port
        time.sleep(0.1)
    raise LibraryError(
        "auth_required",
        "自有浏览器调试入口未就绪；未使用或重启用户现有浏览器。",
    )


def _prepare_profile(profile_dir: Path) -> Path:
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
    return profile_dir


def _launch_edge(args: list[str], creationflags: int) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )


async def browser_login(
    profile_dir: Path,
    *,
    wait_seconds: float = 180.0,
    headless: bool = False,
    edge_path: str | None = None,
) -> tuple[str, str]:
    """Launch an owned Edge profile and wait for the official SSO to finish.

    Only the page we launched is inspected. The profile is dedicated to this
    integration; the user's normal Edge profile is never opened or modified.
    """
    edge = edge_path or find_edge()
    profile_dir = await asyncio.to_thread(_prepare_profile, profile_dir)
    args = [
        edge,
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-restore",
        "--disable-session-crashed-bubble",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--window-size=1100,800",
        f"--app={LOGIN_URL}",
    ]
    if headless:
        args.insert(3, "--headless=new")
    creationflags = 0
    if headless and os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        process = await asyncio.to_thread(_launch_edge, args, creationflags)
    except OSError:
        raise LibraryError("unsupported", "无法启动自有图书馆登录浏览器。") from None
    try:
        port = await asyncio.to_thread(_devtools_port, profile_dir, 20.0)
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + max(5.0, wait_seconds)
        while time.monotonic() < deadline:
            try:
                targets = await _list_targets(endpoint)
            except LibraryError:
                targets = []
            for target in targets:
                captured = await _capture_target(target)
                if captured is not None:
                    return captured
            await asyncio.sleep(0.5)
    finally:
        await asyncio.to_thread(_terminate_owned_process, process)
    raise LibraryError(
        "auth_required",
        "等待正常学校登录超时；自有浏览器已关闭，请重新运行本地登录入口。",
        details={"action": "complete_official_sso_login"},
    )


def write_candidate(path: Path, kind: str, token: str) -> None:
    """Local operator-only helper; writes a DPAPI candidate for migration."""
    from .library_session import TOKEN_RE, protect

    if kind not in {"seat_token", "app_token"}:
        raise LibraryError("invalid_input", "候选 kind 必须是 seat_token 或 app_token。")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise LibraryError("invalid_input", "token 格式无效。")
    path = path.resolve()
    if path.suffix.lower() != ".dpapi":
        raise LibraryError("invalid_input", "候选文件必须使用 .dpapi 后缀。")
    payload = json.dumps({"kind": kind, "token": token}).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".library-candidate-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(protect(payload))
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
