"""Local normal-browser login and passive CDP adoption for the timetable.

Only an owned Edge profile is launched.  The user types credentials directly in
the browser; this module never reads form values or accepts a password.  The
passive path reads an already-open app.upc.edu.cn page and its own cookies
without navigation, refresh or closing any tab.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from .timetable_protocol import PAGE_PATH, TimetableCookie, TimetableError
from .timetable_store import TimetableSession, protect

MAX_TARGETS_BYTES = 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
APP_HOST = "app.upc.edu.cn"
INDEX_URL = "https://app.upc.edu.cn" + PAGE_PATH
CAPTURE_JS = """
(async () => {
  try {
    const r = await fetch('/timetable/wap/default/get-index', {
      credentials: 'include',
      cache: 'no-store',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/plain, */*'
      }
    });
    const t = await r.text();
    let j = null;
    try { j = JSON.parse(t); } catch (e) {}
    return JSON.stringify({ status: r.status, e: j && j.e });
  } catch (e) {
    return JSON.stringify({ status: 0, e: null });
  }
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
        raise TimetableError(
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
        raise TimetableError("upstream_error", "浏览器调试目标地址不在本机范围内。") from None


def _is_app_page(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == APP_HOST
        and parsed.port in {None, 443}
        and not parsed.username
        and not parsed.password
    )


def _target_ws(target: dict) -> str | None:
    value = target.get("webSocketDebuggerUrl")
    if not isinstance(value, str):
        return None
    try:
        return _safe_ws(value)
    except TimetableError:
        return None


async def _list_targets(endpoint: str) -> list[dict]:
    endpoint = local_endpoint(endpoint)
    try:
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=5
        ) as client:
            response = await client.get(endpoint + "/json/list")
            if response.status_code != 200 or len(response.content) > MAX_TARGETS_BYTES:
                raise TimetableError("auth_required", "无法读取本机浏览器调试目标。")
            data = response.json()
    except TimetableError:
        raise
    except (httpx.HTTPError, ValueError):
        raise TimetableError("auth_required", "无法连接本机浏览器调试入口。") from None
    if not isinstance(data, list):
        raise TimetableError("auth_required", "本机浏览器调试目标格式异常。")
    return [item for item in data if isinstance(item, dict)]


async def _command(websocket, request_id: int, method: str, params: dict) -> dict:
    await websocket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    while True:
        raw = await asyncio.wait_for(websocket.recv(), 8)
        message = json.loads(raw)
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise TimetableError("auth_required", "浏览器调试命令失败。")
        return message.get("result", {})


def _cookies_from_cdp(raw_cookies: object) -> list[TimetableCookie]:
    if not isinstance(raw_cookies, list):
        raise TimetableError("auth_required", "浏览器未返回可用课表 Cookie。")
    cookies: list[TimetableCookie] = []
    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain")
        if not isinstance(domain, str) or domain.lower().lstrip(".") not in {
            APP_HOST,
            "upc.edu.cn",
        }:
            continue
        try:
            cookies.append(
                TimetableCookie(
                    name=item.get("name"),
                    value=str(item.get("value", "")),
                    domain=domain,
                    path=str(item.get("path") or "/"),
                    secure=bool(item.get("secure")),
                    http_only=bool(item.get("httpOnly")),
                    same_site=item.get("sameSite")
                    if item.get("sameSite") in {"Strict", "Lax", "None"}
                    else None,
                    expires_at=float(item["expires"])
                    if isinstance(item.get("expires"), (int, float)) and item["expires"] > 0
                    else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return cookies


async def _inspect_target(target: dict, *, source: str) -> TimetableSession | None:
    if not _is_app_page(str(target.get("url", ""))):
        return None
    ws_url = _target_ws(target)
    if ws_url is None:
        return None
    try:
        async with connect(
            ws_url,
            open_timeout=5,
            close_timeout=2,
            max_size=MAX_TARGETS_BYTES,
            ping_interval=None,
        ) as websocket:
            result = await _command(
                websocket,
                1,
                "Runtime.evaluate",
                {
                    "expression": CAPTURE_JS,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            value = result.get("result", {}).get("value")
            if not isinstance(value, str):
                return None
            parsed = json.loads(value)
            if not isinstance(parsed, dict) or parsed.get("status") != 200:
                return None
            if parsed.get("e") != 0:
                return None
            await _command(websocket, 2, "Network.enable", {})
            cookie_result = await _command(websocket, 3, "Network.getAllCookies", {})
    except TimetableError:
        return None
    except Exception:
        return None
    cookies = _cookies_from_cdp(cookie_result.get("cookies"))
    if not cookies:
        return None
    return TimetableSession(cookies=cookies, state="unverified", source=source)  # type: ignore[arg-type]


async def adopt_from_cdp(endpoint: str) -> TimetableSession:
    """Passively read an already-open, already-authenticated app page."""

    endpoint = local_endpoint(endpoint)
    targets = await _list_targets(endpoint)
    for target in targets:
        session = await _inspect_target(target, source="adopted_cdp")
        if session is not None:
            return session
    raise TimetableError(
        "auth_required",
        "本机调试浏览器中没有已登录的课表页面；未导航或刷新任何标签。",
        action="complete_official_login",
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
    raise TimetableError(
        "unsupported",
        "未找到本机 Edge；请设置 UPC_MCP_EDGE_PATH 后重试本地登录。",
        action="set_edge_path",
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
                port = int(path.read_text(encoding="ascii").splitlines()[0].strip())
            except (OSError, ValueError, IndexError):
                port = 0
            if 1 <= port <= 65535:
                return port
        time.sleep(0.1)
    raise TimetableError("auth_required", "自有课表登录浏览器未就绪。")


def _prepare_profile(profile_dir: Path) -> Path:
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
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
    edge_path: str | None = None,
) -> TimetableSession:
    """Launch an owned Edge profile at the official timetable page.

    The user completes the normal official login in that window.  No password
    field is read and no credential is accepted by this function.
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
        f"--app={INDEX_URL}",
    ]
    try:
        process = await asyncio.to_thread(_launch_edge, args, 0)
    except OSError:
        raise TimetableError("unsupported", "无法启动自有课表登录浏览器。") from None
    try:
        port = await asyncio.to_thread(_devtools_port, profile_dir, 20.0)
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + max(5.0, wait_seconds)
        while time.monotonic() < deadline:
            try:
                targets = await _list_targets(endpoint)
            except TimetableError:
                targets = []
            for target in targets:
                session = await _inspect_target(target, source="normal_browser_login")
                if session is not None:
                    return session
            await asyncio.sleep(0.5)
    finally:
        await asyncio.to_thread(_terminate_owned_process, process)
    raise TimetableError(
        "auth_required",
        "等待正常学校登录超时；自有浏览器已关闭，请重新运行本地课表登录入口。",
        action="complete_official_login",
    )


def write_candidate(path: Path, cookies: list[TimetableCookie]) -> None:
    """Test/operator helper for a DPAPI-encrypted cookie candidate."""

    if path.suffix.lower() != ".dpapi":
        raise TimetableError("invalid_input", "候选文件必须使用 .dpapi 后缀。")
    payload = json.dumps(
        {"version": 1, "cookies": [cookie.secret_payload() for cookie in cookies]},
        ensure_ascii=False,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(protect(payload))
