"""Verify failure boundaries through MCP, without making school requests."""

import asyncio
import subprocess
import sys

import httpx
from mcp import Client

from upc_mcp.card_balance import CardBalanceError
from upc_mcp.card_balance_profile import CardBalanceStore
from upc_mcp.card_balance_service import CardBalanceService
from upc_mcp.server import create_server
from upc_mcp.shiguang import ShiguangError
from upc_mcp.shiguang_service import ShiguangService
from upc_mcp.shiguang_session import ShiguangStore


def test_corrupt_session_does_not_prevent_startup_or_discovery(tmp_path, monkeypatch):
    (tmp_path / "shiguang-self.dpapi").write_bytes(b"invalid-dpapi")

    def no_network(*args, **kwargs):
        raise AssertionError("Startup and local discovery must not access the network")

    monkeypatch.setattr(httpx.Client, "send", no_network)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "upc_shiguang_activities" in names
            assert "upc_card_transactions" not in names
            capabilities = await client.call_tool("upc_capabilities", {})
            assert not capabilities.is_error
            assert capabilities.structured_content["capabilities"]["card_transactions"] == "retired"
            status = await client.call_tool("upc_session_status", {"system": "shiguang"})
            assert status.is_error

    asyncio.run(scenario())


def test_both_system_errors_are_confined_to_the_called_tool(tmp_path, monkeypatch):
    async def failed_balance(self):
        raise CardBalanceError("network_error", "Synthetic balance outage")

    async def working_activities(self, **kwargs):
        return {"status": "ok", "items": []}

    async def failed_activities(self, **kwargs):
        raise ShiguangError("auth_required", "Synthetic expired login")

    async def working_balance(self):
        return {"status": "ok", "data": {"balance": "1.23"}}

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            monkeypatch.setattr(CardBalanceService, "query", failed_balance)
            monkeypatch.setattr(ShiguangService, "activities", working_activities)
            assert (await client.call_tool("upc_card_balance", {})).is_error
            assert not (await client.call_tool("upc_shiguang_activities", {})).is_error
            monkeypatch.setattr(CardBalanceService, "query", working_balance)
            monkeypatch.setattr(ShiguangService, "activities", failed_activities)
            assert (await client.call_tool("upc_shiguang_activities", {})).is_error
            assert not (await client.call_tool("upc_card_balance", {})).is_error

    asyncio.run(scenario())


def test_clear_only_changes_its_own_system_files(tmp_path):
    balance = CardBalanceStore(tmp_path)
    shiguang = ShiguangStore(tmp_path)
    balance.path.write_bytes(b"synthetic-balance-state")
    shiguang.path.write_bytes(b"synthetic-shiguang-state")
    balance.clear()
    assert shiguang.path.read_bytes() == b"synthetic-shiguang-state"
    balance.path.write_bytes(b"synthetic-balance-state")
    shiguang.clear()
    assert balance.path.read_bytes() == b"synthetic-balance-state"
    assert balance.lock_path != shiguang.lock_path
    assert balance.generation_path != shiguang.generation_path


def test_slow_shiguang_call_does_not_block_balance_dispatch(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_activities(self, **kwargs):
            entered.set()
            await release.wait()
            return {"status": "ok", "items": []}

        async def working_balance(self):
            return {"status": "ok", "data": {"balance": "1.23"}}

        monkeypatch.setattr(ShiguangService, "activities", slow_activities)
        monkeypatch.setattr(CardBalanceService, "query", working_balance)
        async with Client(create_server(tmp_path)) as client:
            pending = asyncio.create_task(client.call_tool("upc_shiguang_activities", {}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                balance = await asyncio.wait_for(client.call_tool("upc_card_balance", {}), 2)
                assert not balance.is_error
                assert not pending.done()
            finally:
                release.set()
                await pending

    asyncio.run(scenario())


def test_runtime_imports_do_not_load_retired_bill_flow():
    script = (
        "import sys; import upc_mcp.cli; import upc_mcp.server; "
        "assert not set(sys.modules).intersection({"
        "'upc_mcp.jhzf', 'upc_mcp.auth', 'upc_mcp.cdp', 'upc_mcp.storage', 'upc_mcp.models'})"
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_one_parser_failure_does_not_disable_other_shiguang_tools(tmp_path, monkeypatch):
    async def broken_credits(self):
        raise ShiguangError("parse_error", "Synthetic changed credit page")

    async def working_activities(self, **kwargs):
        return {"status": "ok", "items": [], "source_system": "shiguang"}

    monkeypatch.setattr(ShiguangService, "credits", broken_credits)
    monkeypatch.setattr(ShiguangService, "activities", working_activities)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            failed = await client.call_tool("upc_shiguang_credits", {})
            assert failed.is_error
            assert failed.structured_content["status"] == "parse_error"
            working = await client.call_tool("upc_shiguang_activities", {})
            assert not working.is_error
            assert working.structured_content["items"] == []
            assert not (await client.call_tool("upc_capabilities", {})).is_error

    asyncio.run(scenario())
