from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from upc_mcp.cli import parser as shared_parser
from upc_mcp.timetable_cli import add_timetable_parser, execute_timetable
from upc_mcp.timetable_service import TimetableService
from upc_mcp.timetable_store import TimetableStore


class FakeService:
    def __init__(self):
        self.calls = []

    async def status(self):
        self.calls.append(("status",))
        return {"status": "auth_required"}

    async def check(self):
        self.calls.append(("check",))
        return {"status": "ok"}

    async def clear(self):
        self.calls.append(("clear",))
        return {"status": "ok", "local_session_removed": True}

    async def login(self, profile, *, wait_seconds, rebind):
        self.calls.append(("login", Path(profile), wait_seconds, rebind))
        return {"status": "ok"}

    async def adopt_cdp(self, endpoint, *, rebind):
        self.calls.append(("adopt_cdp", endpoint, rebind))
        return {"status": "ok"}

    async def adopt_candidate(self, path, *, rebind):
        self.calls.append(("adopt_candidate", Path(path), rebind))
        return {"status": "ok"}


def parse(*args):
    root = argparse.ArgumentParser()
    root.add_argument("--state-dir", type=Path, default=None)
    commands = root.add_subparsers(dest="command", required=True)
    add_timetable_parser(commands)
    return root.parse_args(list(args))


def run(args, service, tmp_path):
    return asyncio.run(execute_timetable(args, tmp_path, service=service))


def test_parser_exposes_only_session_and_read_actions():
    args = parse("timetable", "status")
    assert args.command == "timetable"
    assert args.timetable_action == "status"
    login = parse("timetable", "login", "--timeout", "5", "--rebind")
    assert login.timeout == 5
    assert login.rebind is True
    assert not hasattr(login, "password")
    assert not hasattr(login, "cookie")
    assert not hasattr(login, "url")


def test_execute_status_check_clear(tmp_path):
    service = FakeService()
    assert run(parse("timetable", "status"), service, tmp_path)["status"] == "auth_required"
    assert run(parse("timetable", "check"), service, tmp_path)["status"] == "ok"
    assert run(parse("timetable", "clear"), service, tmp_path)["local_session_removed"] is True
    assert service.calls == [("status",), ("check",), ("clear",)]


def test_execute_login_uses_state_directory_default_profile(tmp_path):
    service = FakeService()
    args = parse("timetable", "login", "--timeout", "3", "--rebind")
    run(args, service, tmp_path)
    assert service.calls[0][0] == "login"
    assert service.calls[0][1] == tmp_path / "timetable-browser-profile"
    assert service.calls[0][2] == 3
    assert service.calls[0][3] is True


def test_execute_adopt_paths(tmp_path):
    service = FakeService()
    run(parse("timetable", "adopt-cdp", "--endpoint", "http://127.0.0.1:9222"), service, tmp_path)
    run(
        parse("timetable", "adopt-candidate", "--source", str(tmp_path / "candidate.dpapi")),
        service,
        tmp_path,
    )
    assert service.calls[0][0] == "adopt_cdp"
    assert service.calls[0][1] == "http://127.0.0.1:9222"
    assert service.calls[1][0] == "adopt_candidate"
    assert service.calls[1][1] == tmp_path / "candidate.dpapi"


def test_shared_cli_exposes_timetable_status(tmp_path):
    args = shared_parser().parse_args(
        ["--state-dir", str(tmp_path), "timetable", "status"]
    )
    assert args.command == "timetable"
    assert args.timetable_action == "status"


def test_status_does_not_construct_network_client(tmp_path):
    def factory(_cookies):
        raise AssertionError("status must not construct a network client")

    service = TimetableService(TimetableStore(tmp_path), client_factory=factory)
    payload = run(parse("timetable", "status"), service, tmp_path)
    assert payload["status"] == "auth_required"
    assert payload["source_system"] == "timetable"
    assert payload["session"] is None
