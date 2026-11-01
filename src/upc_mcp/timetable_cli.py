"""Timetable CLI subparser and dispatch, independent of the shared cli.py.

Integration contract for ``src/upc_mcp/cli.py``:

    commands.add_parser(...) set exists as ``commands``
    add_timetable_parser(commands)
    ...
    elif args.command == "timetable":
        output = asyncio.run(execute_timetable(args, directory))
    ...
    except TimetableError as error:
        ...

The module can also be invoked directly:
``python -m upc_mcp.timetable_cli --state-dir E:/... timetable status``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .auto_auth.coordinator import create_auto_auth
from .config import default_state_dir
from .timetable_protocol import TimetableError
from .timetable_service import TimetableService
from .timetable_store import TimetableStore


def add_timetable_parser(commands: argparse._SubParsersAction) -> argparse.ArgumentParser:
    timetable = commands.add_parser(
        "timetable",
        help="Campus-app timetable session and read-only week queries",
    )
    actions = timetable.add_subparsers(dest="timetable_action", required=True)
    for action in ("status", "check", "clear"):
        actions.add_parser(action)
    login = actions.add_parser(
        "login",
        help="Open an owned Edge profile at the official timetable page",
    )
    login.add_argument("--profile", type=Path, default=None)
    login.add_argument("--timeout", type=float, default=180.0)
    login.add_argument("--rebind", action="store_true")
    adopt_cdp = actions.add_parser(
        "adopt-cdp",
        help="Passively adopt cookies from an existing loopback CDP timetable page",
    )
    adopt_cdp.add_argument("--endpoint", required=True)
    adopt_cdp.add_argument("--rebind", action="store_true")
    adopt_candidate = actions.add_parser(
        "adopt-candidate",
        help="Adopt a local DPAPI-encrypted timetable cookie candidate",
    )
    adopt_candidate.add_argument("--source", type=Path, required=True)
    adopt_candidate.add_argument("--rebind", action="store_true")
    return timetable


async def execute_timetable(
    args: argparse.Namespace,
    directory: Path,
    *,
    service: TimetableService | None = None,
) -> dict:
    timetable_service = service or TimetableService(
        TimetableStore(directory), auto_auth=create_auto_auth(directory)
    )
    action = args.timetable_action
    if action == "status":
        return await timetable_service.status()
    if action == "check":
        return await timetable_service.check()
    if action == "clear":
        return await timetable_service.clear()
    if action == "login":
        profile = args.profile or (directory / "timetable-browser-profile")
        return await timetable_service.login(
            profile,
            wait_seconds=args.timeout,
            rebind=args.rebind,
        )
    if action == "adopt-cdp":
        return await timetable_service.adopt_cdp(args.endpoint, rebind=args.rebind)
    if action == "adopt-candidate":
        return await timetable_service.adopt_candidate(args.source, rebind=args.rebind)
    raise TimetableError("invalid_input", "未知的课表命令。")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="upc-mcp timetable")
    parser.add_argument("--state-dir", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    add_timetable_parser(commands)
    args = parser.parse_args(argv)
    directory = (args.state_dir or default_state_dir()).resolve()
    try:
        output = asyncio.run(execute_timetable(args, directory))
    except TimetableError as error:
        print(json.dumps(error.payload(), ensure_ascii=False))
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130) from None
    except Exception:
        print(
            json.dumps(
                {
                    "status": "local_error",
                    "source_system": "timetable",
                    "session_ref": "self",
                    "error": {"message": "本地课表操作失败；未输出原始响应或凭据。"},
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
