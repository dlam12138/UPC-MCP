import asyncio
import json
import subprocess
import sys

import pytest

from upc_mcp.cli import execute_library, parser
from upc_mcp.library_seat import LibraryError


def args_for(*values):
    return parser().parse_args(["library", *values])


def test_library_cli_accepts_status_check_clear_login_adopt_and_cdp():
    assert args_for("status").library_action == "status"
    assert args_for("check").library_action == "check"
    assert args_for("clear").library_action == "clear"
    login = args_for("login", "--profile", "E:/synthetic/profile", "--timeout", "30")
    assert login.library_action == "login"
    assert login.timeout == 30
    adopt = args_for("adopt", "--source", "E:/synthetic/candidate.dpapi")
    assert adopt.library_action == "adopt"
    cdp = args_for("adopt-cdp", "--endpoint", "http://127.0.0.1:9222")
    assert cdp.library_action == "adopt-cdp"


def test_library_status_and_clear_local_entry(tmp_path):
    async def scenario():
        status = await execute_library(args_for("status"), tmp_path)
        assert status["status"] == "auth_required"
        assert status["source_system"] == "library"
        cleared = await execute_library(args_for("clear"), tmp_path)
        assert cleared["status"] == "ok"
        assert cleared["local_session_removed"] is True
        assert cleared["remote_session_revoked"] is False

    asyncio.run(scenario())


def test_library_cdp_entry_rejects_non_loopback_before_network(tmp_path):
    async def scenario():
        with pytest.raises(LibraryError) as error:
            await execute_library(
                args_for("adopt-cdp", "--endpoint", "http://example.com:9222"),
                tmp_path,
            )
        assert error.value.code == "invalid_input"

    asyncio.run(scenario())


def test_library_adopt_rejects_missing_candidate(tmp_path):
    async def scenario():
        with pytest.raises(LibraryError) as error:
            await execute_library(
                args_for("adopt", "--source", str(tmp_path / "missing.dpapi")),
                tmp_path,
            )
        assert error.value.code == "invalid_input"

    asyncio.run(scenario())


def test_library_status_through_module_cli(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "upc_mcp",
            "--state-dir",
            str(tmp_path),
            "library",
            "status",
        ],
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert payload["status"] == "auth_required"
    assert payload["source_system"] == "library"
    assert "token" not in completed.stdout.decode("utf-8").lower()
