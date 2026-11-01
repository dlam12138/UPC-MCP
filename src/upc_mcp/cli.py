import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path

from .auto_auth import AutoAuthError
from .auto_auth.coordinator import create_auto_auth
from .card_balance import CardBalanceError
from .card_balance_profile import CardBalanceStore
from .card_balance_service import CardBalanceService
from .config import default_state_dir
from .library_auth import adopt_from_cdp, browser_login
from .library_seat import LibraryError
from .library_service import LibraryService
from .library_session import LibraryAuthLock, LibraryStore
from .service_portal_protocol import ServicePortalError
from .service_portal_service import ServicePortalService
from .service_portal_store import ServicePortalStore
from .shiguang import ShiguangError
from .shiguang_service import ShiguangService
from .shiguang_session import ShiguangStore
from .timetable_cli import add_timetable_parser, execute_timetable
from .timetable_protocol import TimetableError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="upc-mcp")
    root.add_argument("--state-dir", type=Path, default=None)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the local stdio MCP server")
    commands.add_parser("mcp-config", help="Print portable MCP config for this installation")
    settings = commands.add_parser("settings", help="Open the local account/settings page")
    settings.add_argument("--port", type=int, default=0,
                          help="Loopback port; 0 chooses an available port")
    settings.add_argument("--idle-minutes", type=int, default=30,
                          help="Stop the settings server after this many idle minutes")
    credential = commands.add_parser(
        "credential", help="Local unified CAS account and checkpoint management"
    )
    credential_actions = credential.add_subparsers(dest="credential_action", required=True)
    configure = credential_actions.add_parser(
        "configure", help="Prompt locally for the school username/password once"
    )
    configure.add_argument("--rebind", action="store_true")
    credential_actions.add_parser("status")
    credential_actions.add_parser("clear")
    credential_actions.add_parser("unlock", help="Clear a captcha/2FA user-action block")
    credential_actions.add_parser("clear-cas", help="Remove only the local CAS checkpoint")
    credential_actions.add_parser("clear-state", help="Remove only recovery/backoff state")
    for action in ("adopt", "migrate"):
        adopt = credential_actions.add_parser(
            action, help="Adopt the already-authorized research credential/checkpoint once"
        )
        adopt.add_argument("--source", type=Path, required=False)
        adopt.add_argument("--cas-source", type=Path, required=False)
    balance = commands.add_parser(
        "card-balance", help="Independent local self balance profile/query"
    )
    balance_actions = balance.add_subparsers(dest="balance_action", required=True)
    for action in ("configure", "status", "check", "clear"):
        balance_actions.add_parser(action)
    shiguang = commands.add_parser("shiguang", help="Local Shiguang session lifecycle")
    shiguang_actions = shiguang.add_subparsers(dest="shiguang_action", required=True)
    for action in ("status", "check", "clear"):
        shiguang_actions.add_parser(action)
    adopt_shiguang = shiguang_actions.add_parser(
        "adopt",
        help="Adopt and verify a local DPAPI-encrypted Shiguang session candidate",
    )
    adopt_shiguang.add_argument("--source", type=Path, required=True)
    library = commands.add_parser(
        "library", help="Independent library seat session and browser login"
    )
    library_actions = library.add_subparsers(dest="library_action", required=True)
    for action in ("status", "check", "clear"):
        library_actions.add_parser(action)
    login_library = library_actions.add_parser(
        "login",
        help="Open an owned Edge profile at the official library SSO page",
    )
    login_library.add_argument("--profile", type=Path, default=None)
    login_library.add_argument("--timeout", type=float, default=180.0)
    login_library.add_argument("--headless", action="store_true")
    login_library.add_argument("--rebind", action="store_true")
    adopt_library = library_actions.add_parser(
        "adopt", help="Adopt a local DPAPI-encrypted seat/app token candidate"
    )
    adopt_library.add_argument("--source", type=Path, required=True)
    adopt_library.add_argument("--rebind", action="store_true")
    cdp_library = library_actions.add_parser(
        "adopt-cdp", help="Passively adopt a token from an existing libseat CDP page"
    )
    cdp_library.add_argument("--endpoint", required=True)
    cdp_library.add_argument("--rebind", action="store_true")
    add_timetable_parser(commands)
    service_portal = commands.add_parser(
        "service-portal", help="Independent e站通 session lifecycle/check"
    )
    service_portal_actions = service_portal.add_subparsers(
        dest="service_portal_action", required=True
    )
    for action in ("status", "check", "clear"):
        service_portal_actions.add_parser(action)
    return root


async def execute_shiguang(args, store: ShiguangStore):
    service = ShiguangService(store, auto_auth=create_auto_auth(store.directory))
    if args.shiguang_action == "status":
        return await asyncio.to_thread(service.status)
    if args.shiguang_action == "check":
        return await service.check()
    if args.shiguang_action == "clear":
        return await asyncio.to_thread(service.clear)
    return await service.adopt_candidate(args.source)


async def execute_card_balance(args, directory: Path):
    service = CardBalanceService(CardBalanceStore(directory))
    if args.balance_action == "configure":
        student_id = getpass.getpass("本人学号（仅本机保存，输入隐藏）：")
        return await asyncio.to_thread(service.configure, student_id)
    if args.balance_action == "check":
        return await service.query()
    if args.balance_action == "clear":
        return await asyncio.to_thread(service.clear)
    return await asyncio.to_thread(service.status)


def _library_token_adopt(
    service: LibraryService,
    kind: str,
    token: str,
    rebind: bool,
    *,
    seat_source: str = "browser_seat_token",
):
    if kind == "app_token":
        return service.adopt_app_token(token, rebind=rebind)
    return service.adopt_seat_token(token, source=seat_source, rebind=rebind)


async def execute_library(args, directory: Path):
    service = LibraryService(LibraryStore(directory), auto_auth=create_auto_auth(directory))
    if args.library_action == "status":
        return await service.status()
    if args.library_action == "check":
        return await service.check()
    if args.library_action == "clear":
        return await service.clear()
    if args.library_action not in {"adopt", "adopt-cdp", "login"}:
        raise LibraryError("invalid_input", "未知的图书馆本地认证动作。")
    held = await asyncio.to_thread(LibraryAuthLock(directory).acquire)
    try:
        if args.library_action == "adopt":
            return await service.adopt_candidate(args.source, rebind=args.rebind)
        if args.library_action == "adopt-cdp":
            kind, token = await adopt_from_cdp(args.endpoint)
            return await _library_token_adopt(
                service,
                kind,
                token,
                args.rebind,
                seat_source="cdp_seat_token",
            )
        profile = args.profile or (directory / "library-browser-profile")
        kind, token = await browser_login(
            profile,
            wait_seconds=args.timeout,
            headless=args.headless,
        )
        return await _library_token_adopt(service, kind, token, args.rebind)
    finally:
        await asyncio.to_thread(held.release)


def execute_credential(args, directory: Path) -> dict:
    auto_auth = create_auto_auth(directory)
    action = args.credential_action
    if action == "status":
        return auto_auth.status()
    if action == "clear":
        binding = auto_auth.clear_credential()
        return {
            "status": "ok",
            "configured": False,
            "generation": binding.generation,
            "credential_removed": True,
            "cas_checkpoint_removed": True,
            "sessions_marked_unbound": True,
        }
    if action == "unlock":
        return auto_auth.unlock()
    if action == "clear-cas":
        auto_auth.clear_cas_checkpoint()
        return {"status": "ok", "cas_checkpoint_removed": True}
    if action == "clear-state":
        auto_auth.clear_state()
        return {"status": "ok", "recovery_state_removed": True}
    if action in {"adopt", "migrate"}:
        if args.source is None and args.cas_source is None:
            raise AutoAuthError(
                "invalid_input", "adopt/migrate 至少需要 --source 或 --cas-source。"
            )
        binding = auto_auth.binding()
        credential_adopted = False
        if args.source is not None:
            binding = auto_auth.credentials.adopt_from(args.source)
            credential_adopted = True
        elif not binding.configured:
            raise AutoAuthError(
                "credential_missing", "尚无生产凭据，--cas-source 需要先采用统一凭据。"
            )
        checkpoint_adopted = False
        if args.cas_source is not None:
            auto_auth.adopt_checkpoint(args.cas_source, binding)
            checkpoint_adopted = True
        return {
            "status": "ok",
            "credential_adopted": credential_adopted,
            "cas_checkpoint_adopted": checkpoint_adopted,
            "credential": {
                "configured": binding.configured,
                "generation": binding.generation,
                "account_ref": binding.account_ref(),
            },
            "research_sources_preserved": True,
        }
    if action == "configure":
        current = auto_auth.binding()
        if current.configured and not args.rebind:
            raise AutoAuthError(
                "already_configured",
                "本机已有统一凭据；如需更换账户请显式使用 --rebind。",
                action="use_rebind",
            )
        username = getpass.getpass("学号/用户名（本机隐藏输入，不进入 argv/env）：").strip()
        password = getpass.getpass("密码（本机隐藏输入）：")
        confirm = getpass.getpass("再次输入密码：")
        if password != confirm:
            raise AutoAuthError("invalid_input", "两次密码输入不一致；未保存。")
        binding = auto_auth.configure(username, password, expected=current)
        return {
            "status": "ok",
            "configured": True,
            "generation": binding.generation,
            "account_ref": binding.account_ref(),
            "cas_checkpoint_removed": True,
        }
    raise AutoAuthError("invalid_input", "未知的统一凭据动作。")


async def execute_service_portal(args, directory: Path) -> dict:
    service = ServicePortalService(
        ServicePortalStore(directory),
        auto_auth=create_auto_auth(directory),
    )
    action = args.service_portal_action
    if action == "status":
        return await service.status()
    if action == "check":
        return await service.check()
    if action == "clear":
        return await service.clear()
    raise ServicePortalError("invalid_input", "未知的 e站通本地动作。")


def main():
    args = parser().parse_args()
    # No HTTP wire or sensitive exception logging, including by third-party SDKs.
    logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)
    directory = (args.state_dir or default_state_dir()).resolve()
    try:
        if args.command == "mcp-config":
            from .settings_ui import mcp_configuration

            output = mcp_configuration(directory)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return
        if args.command == "settings":
            from .settings_ui import run_settings

            if not 0 <= args.port <= 65535 or not 1 <= args.idle_minutes <= 240:
                raise AutoAuthError("invalid_input", "端口或设置页空闲时间超出允许范围。")
            run_settings(directory, port=args.port, idle_timeout=args.idle_minutes * 60)
            return
        if args.command == "serve":
            from .server import create_server

            create_server(directory).run(transport="stdio")
            return
        if args.command == "card-balance":
            output = asyncio.run(execute_card_balance(args, directory))
        elif args.command == "credential":
            output = execute_credential(args, directory)
        elif args.command == "library":
            output = asyncio.run(execute_library(args, directory))
        elif args.command == "timetable":
            output = asyncio.run(execute_timetable(args, directory))
        elif args.command == "service-portal":
            output = asyncio.run(execute_service_portal(args, directory))
        else:
            output = asyncio.run(execute_shiguang(args, ShiguangStore(directory)))
    except (
        ShiguangError,
        CardBalanceError,
        LibraryError,
        TimetableError,
        ServicePortalError,
        AutoAuthError,
    ) as error:
        output = error.payload()
        print(json.dumps(output, ensure_ascii=False))
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130) from None
    except Exception:
        print(
            json.dumps(
                {
                    "status": "local_error",
                    "error": {"message": "本地操作失败；未输出原始响应或凭据，请检查配置。"},
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(output, ensure_ascii=False))
