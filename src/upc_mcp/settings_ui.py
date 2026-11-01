"""Short-lived loopback settings UI. School secrets never enter MCP messages.

The browser receives a one-time launch capability via URL fragment, exchanges
it for an HttpOnly session cookie, then uses a separate same-origin CSRF header.
No external resources, analytics, request logging or saved form values exist.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .auto_auth.coordinator import create_auto_auth
from .auto_auth.errors import AutoAuthError

ASSETS = Path(__file__).with_name("settings_assets")
MAX_BODY = 8192
SYSTEM_NAMES = {
    "shiguang": "石光第二课堂",
    "timetable": "校园 App 课表",
    "library": "图书馆座位",
    "service_portal": "e站通",
}
SAFE_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/settings.css": ("settings.css", "text/css; charset=utf-8"),
    "/assets/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/assets/body.ttf": ("body.ttf", "font/ttf"),
    "/assets/title.ttf": ("title.ttf", "font/ttf"),
}


class SettingsError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def mcp_configuration(directory: Path) -> dict:
    executable = Path(sys.executable).with_name("upc-mcp.exe")
    if executable.is_file():
        command, args = str(executable), []
    else:
        command, args = sys.executable, ["-m", "upc_mcp"]
    return {"mcpServers": {"upc": {
        "command": command,
        "args": [*args, "--state-dir", str(directory), "serve"],
    }}}


class SettingsApplication:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.auto_auth = create_auto_auth(self.directory)
        self._checks = {name: threading.Lock() for name in SYSTEM_NAMES}

    @staticmethod
    def _generation(body: dict) -> int:
        value = body.get("expected_generation")
        if type(value) is not int or value < 0:
            raise SettingsError("invalid_input", "设置状态无效，请刷新页面后重试。")
        return value

    def _expected(self, body: dict):
        expected = self.auto_auth.binding()
        if expected.generation != self._generation(body):
            raise SettingsError("credential_changed", "账号设置已变化，请刷新后重试。", 409)
        return expected

    def save(self, body: dict) -> dict:
        username, password, confirm = (body.get(key) for key in
                                       ("username", "password", "password_confirm"))
        if not isinstance(username, str) or not 1 <= len(username.strip()) <= 128:
            raise SettingsError("invalid_input", "请输入有效的学号或学校账号。")
        if not isinstance(password, str) or not 1 <= len(password) <= 512:
            raise SettingsError("invalid_input", "请输入密码，长度不超过 512 个字符。")
        if password != confirm:
            raise SettingsError("password_mismatch", "两次密码输入不一致。")
        expected = self._expected(body)
        if expected.configured and body.get("replace") is not True:
            raise SettingsError("already_configured", "已有账号，请使用更新账号操作。", 409)
        self.auto_auth.configure(username.strip(), password, expected=expected)
        return {
            "status": "ok",
            "message": "账号已加密保存；余额查询自动使用该账号的学号，无需另行配置。",
        }

    def clear(self, body: dict) -> dict:
        self.auto_auth.clear_credential(expected=self._expected(body))
        return {"status": "ok", "message": "统一凭据已移除，余额查询已停用；其他系统文件保留。"}

    def status(self) -> dict:
        # Imports/read failures in one business store stay within its own row.
        from .library_session import LibraryStore
        from .service_portal_store import ServicePortalStore
        from .shiguang_session import ShiguangStore
        from .timetable_store import TimetableStore

        auth = self.auto_auth.status()
        generation = auth.get("generation", 0)
        configured = bool(auth.get("configured"))
        try:
            binding = self.auto_auth.binding()
        except AutoAuthError:
            binding = None
        rows = []
        stores = {"shiguang": ShiguangStore, "timetable": TimetableStore,
                  "library": LibraryStore, "service_portal": ServicePortalStore}
        for name, store_type in stores.items():
            row = {"id": name, "name": SYSTEM_NAMES[name], "state": "absent",
                   "last_verified_at": None}
            try:
                session = store_type(self.directory).load()
                if session is not None:
                    row["state"] = session.state
                    row["last_verified_at"] = session.verified_at
                    if binding is None:
                        row["state"] = "credential_error"
                    elif binding.configured and (
                        session.account_hash != binding.account_hash
                        or session.credential_generation != binding.generation
                    ):
                        row["state"] = "needs_connection"
                    elif not binding.configured and session.account_hash:
                        row["state"] = "needs_account"
            except Exception:
                row["state"] = "local_error"
            rows.append(row)
        return {
            "status": "ok", "credential": {
                "configured": configured, "generation": generation,
                "error": auth.get("error"),
                "requires_user_action": bool((auth.get("recovery") or {}).get(
                    "requires_user_action")),
            },
            "systems": rows, "mcp_config": mcp_configuration(self.directory),
            "state_directory": str(self.directory),
        }

    def check(self, body: dict) -> dict:
        system = body.get("system")
        if not isinstance(system, str) or system not in SYSTEM_NAMES:
            raise SettingsError("invalid_input", "请选择有效的校园服务。")
        gate = self._checks[system]
        if not gate.acquire(blocking=False):
            raise SettingsError("check_in_progress", "该服务正在检查，请稍候。", 409)
        try:
            if system == "shiguang":
                from .shiguang_service import ShiguangService
                from .shiguang_session import ShiguangStore

                operation = ShiguangService(
                    ShiguangStore(self.directory), auto_auth=self.auto_auth
                ).check()
            elif system == "timetable":
                from .timetable_service import TimetableService
                from .timetable_store import TimetableStore

                operation = TimetableService(
                    TimetableStore(self.directory), auto_auth=self.auto_auth
                ).check()
            elif system == "library":
                from .library_service import LibraryService
                from .library_session import LibraryStore

                operation = LibraryService(
                    LibraryStore(self.directory), auto_auth=self.auto_auth
                ).check()
            else:
                from .service_portal_service import ServicePortalService
                from .service_portal_store import ServicePortalStore

                operation = ServicePortalService(
                    ServicePortalStore(self.directory), auto_auth=self.auto_auth
                ).check()
            result = asyncio.run(operation)
            if result.get("status") != "ok":
                raise SettingsError("connection_failed", "暂未建立连接，请检查本地账号设置。")
            # Never return business payloads to the settings page.
            return {"status": "ok", "system": system, "message": "连接检查通过。"}
        except SettingsError:
            raise
        except Exception as error:
            code = getattr(error, "code", "local_error")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
                code = "local_error"
            messages = {
                "identity_mismatch": "该服务绑定了另一账号，请先用本地命令清除该服务会话。",
                "credential_missing": "请先保存学校账号和密码。",
                "credential_reconfigure_required": "学校拒绝了当前凭据，请更新账号或密码。",
                "cas_invalid_credentials": "学校拒绝了当前凭据，请更新账号或密码。",
                "user_action_required": "学校要求验证码或二次验证，请完成官方交互。",
                "cas_captcha_required": "学校要求验证码，请完成官方交互。",
                "cas_second_factor_required": "学校要求二次验证，请完成官方交互。",
                "recovery_backoff": "最近一次连接未成功，正在短暂等待，请稍后重试。",
            }
            raise SettingsError(code, messages.get(
                code, "连接未完成，请稍后重试或检查学校网络和账号设置。"
            )) from None
        finally:
            gate.release()


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, app: SettingsApplication, port: int = 0):
        self.app = app
        self.launch_token = secrets.token_urlsafe(32)
        self.cookie_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.bootstrapped = False
        self.auth_lock = threading.Lock()
        self.started_at = self.last_access = time.monotonic()
        super().__init__(("127.0.0.1", port), SettingsHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.cookie_name = f"UPCSettings_{self.server_port}"

    @property
    def launch_url(self) -> str:
        return self.origin + "/#access=" + self.launch_token

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def handle_error(self, request, client_address):
        # HTTP exceptions may contain submitted forms; no traceback logging.
        pass


class SettingsHandler(BaseHTTPRequestHandler):
    server: SettingsServer
    server_version = "UPCSettings"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _reply(self, status: int, content: bytes, mime="application/json; charset=utf-8",
               cookie: str | None = None) -> None:
        # Drain a small rejected form before closing HTTP/1.0. Otherwise an
        # unread POST body can cause a TCP reset that hides our 401/403 reply.
        if self.command == "POST" and not getattr(self, "_body_consumed", False):
            size = self.headers.get("Content-Length", "")
            if (not self.headers.get("Transfer-Encoding") and size.isdecimal()
                    and 0 <= int(size) <= MAX_BODY + 1):
                try:
                    self.rfile.read(int(size))
                except OSError:
                    pass
                self._body_consumed = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; font-src 'self'; connect-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
                         "form-action 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: int, data: dict, cookie: str | None = None) -> None:
        self._reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), cookie=cookie)

    def _source(self) -> None:
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            raise SettingsError("forbidden", "该地址不能访问本地设置。", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise SettingsError("forbidden", "仅允许从本地设置页操作。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise SettingsError("forbidden", "仅允许从本地设置页操作。", 403)
        self.server.last_access = time.monotonic()

    def _authorized(self, *, mutation: bool = False) -> None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            key = self.server.cookie_name
            supplied = cookie[key].value if key in cookie else ""
        except Exception:
            supplied = ""
        if not self.server.bootstrapped or not hmac.compare_digest(
            supplied, self.server.cookie_token
        ):
            raise SettingsError("settings_locked", "请通过本机设置启动入口打开此页面。", 401)
        if mutation:
            if self.headers.get("Origin") != self.server.origin or not hmac.compare_digest(
                self.headers.get("X-UPC-Settings", ""), self.server.csrf_token
            ):
                raise SettingsError("forbidden", "设置操作未通过本机页面校验。", 403)

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise SettingsError("invalid_input", "不支持此请求格式。")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise SettingsError("invalid_input", "请使用设置页提交。", 415)
        raw_size = self.headers.get("Content-Length", "")
        if not raw_size.isdecimal() or not 1 <= int(raw_size) <= MAX_BODY:
            raise SettingsError("invalid_input", "设置请求过大或无效。", 413)
        try:
            raw = self.rfile.read(int(raw_size))
            self._body_consumed = True
            if len(raw) != int(raw_size):
                raise ValueError
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, UnicodeError):
            raise SettingsError("invalid_input", "设置请求格式无效。") from None

    def _dispatch(self, *, post: bool) -> None:
        try:
            self._source()
            parts = urlsplit(self.path)
            if parts.query or parts.fragment:
                raise SettingsError("not_found", "页面不存在。", 404)
            path = parts.path
            if not post and path in SAFE_ASSETS:
                filename, mime = SAFE_ASSETS[path]
                self._reply(200, (ASSETS / filename).read_bytes(), mime)
                return
            if post and path == "/api/bootstrap":
                if self.headers.get("Origin") != self.server.origin:
                    raise SettingsError("forbidden", "请通过本机入口打开设置。", 403)
                self._body()
                with self.server.auth_lock:
                    supplied = self.headers.get("X-UPC-Launch", "")
                    if (self.server.bootstrapped or time.monotonic() - self.server.started_at > 120
                            or not hmac.compare_digest(supplied, self.server.launch_token)):
                        raise SettingsError(
                            "settings_locked", "启动授权已失效，请重新打开设置。", 401
                        )
                    self.server.bootstrapped = True
                self._json(200, {"status": "ok"},
                           f"{self.server.cookie_name}={self.server.cookie_token}; "
                           "Path=/; HttpOnly; "
                           "SameSite=Strict")
                return
            self._authorized(mutation=post)
            if not post and path == "/api/status":
                data = self.server.app.status()
                data["csrf"] = self.server.csrf_token
                self._json(200, data)
                return
            if post:
                body = self._body()
                routes = {"/api/credentials": self.server.app.save,
                          "/api/credentials/clear": self.server.app.clear,
                          "/api/check": self.server.app.check}
                if path in routes:
                    self._json(200, routes[path](body))
                    return
                if path == "/api/close":
                    self._json(200, {"status": "ok"},
                               f"{self.server.cookie_name}=; Path=/; HttpOnly; "
                               "SameSite=Strict; Max-Age=0")
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            raise SettingsError("not_found", "页面不存在。", 404)
        except SettingsError as error:
            self._json(error.http_status, {"status": error.code, "message": str(error)})
        except AutoAuthError as error:
            code = error.code
            message = "本地凭据操作未完成，请刷新页面检查状态。"
            self._json(409 if code == "credential_changed" else 400,
                       {"status": code, "message": message})
        except Exception:
            self._json(500, {"status": "local_error", "message": "本地设置操作失败。"})

    def do_GET(self):
        self._dispatch(post=False)

    def do_POST(self):
        self._dispatch(post=True)


def run_settings(directory: Path, *, port: int = 0, idle_timeout: int = 1800) -> None:
    server = SettingsServer(SettingsApplication(directory), port)

    def expire():
        while not stop.wait(1):
            if time.monotonic() - server.last_access > idle_timeout:
                server.shutdown()
                return

    stop = threading.Event()
    threading.Thread(target=expire, daemon=True).start()
    print(f"本地设置已打开：{server.origin}/ （仅本机，闲置 {idle_timeout // 60} 分钟后关闭）",
          flush=True)
    try:
        webbrowser.open(server.launch_url)
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
