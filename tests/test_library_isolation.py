"""Library failure boundaries without making school requests."""

import asyncio
import subprocess
import sys

import httpx
from mcp import Client

from upc_mcp.card_balance_profile import CardBalanceStore
from upc_mcp.card_balance_service import CardBalanceService
from upc_mcp.library_seat import LibraryError
from upc_mcp.library_service import LibraryService
from upc_mcp.library_session import LibraryStore
from upc_mcp.server import create_server
from upc_mcp.shiguang_service import ShiguangService
from upc_mcp.shiguang_session import ShiguangStore


def test_corrupt_library_session_does_not_prevent_startup_or_other_tools(
    tmp_path, monkeypatch
):
    (tmp_path / "library-self.dpapi").write_bytes(b"invalid-dpapi")

    def no_network(*args, **kwargs):
        raise AssertionError("Startup and local discovery must not access the network")

    monkeypatch.setattr(httpx.Client, "send", no_network)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "upc_library_filters" in names
            assert "upc_card_transactions" not in names
            capabilities = await client.call_tool("upc_capabilities", {})
            assert not capabilities.is_error
            assert capabilities.structured_content["capabilities"]["library"] == "implemented"
            status = await client.call_tool("upc_session_status", {"system": "library"})
            assert status.is_error
            query = await client.call_tool("upc_library_filters", {})
            assert query.is_error
            assert query.structured_content["status"] == "auth_required"

    asyncio.run(scenario())


def test_clear_only_changes_its_own_system_files(tmp_path):
    balance = CardBalanceStore(tmp_path)
    shiguang = ShiguangStore(tmp_path)
    library = LibraryStore(tmp_path)
    balance.path.write_bytes(b"synthetic-balance-state")
    shiguang.path.write_bytes(b"synthetic-shiguang-state")
    library.path.write_bytes(b"synthetic-library-state")
    balance.clear()
    assert shiguang.path.read_bytes() == b"synthetic-shiguang-state"
    assert library.path.read_bytes() == b"synthetic-library-state"
    balance.path.write_bytes(b"synthetic-balance-state")
    shiguang.clear()
    assert balance.path.read_bytes() == b"synthetic-balance-state"
    assert library.path.read_bytes() == b"synthetic-library-state"
    balance.path.write_bytes(b"synthetic-balance-state")
    shiguang.path.write_bytes(b"synthetic-shiguang-state")
    library.clear()
    assert balance.path.read_bytes() == b"synthetic-balance-state"
    assert shiguang.path.read_bytes() == b"synthetic-shiguang-state"
    assert len({balance.lock_path, shiguang.lock_path, library.lock_path}) == 3
    assert (
        len({balance.generation_path, shiguang.generation_path, library.generation_path})
        == 3
    )


def test_slow_library_call_does_not_block_balance_or_shiguang_dispatch(
    tmp_path, monkeypatch
):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_filters(self):
            entered.set()
            await release.wait()
            return {"status": "ok", "dates": [], "buildings": []}

        async def working_balance(self):
            return {"status": "ok", "data": {"balance": "1.23"}}

        async def working_activities(self, **kwargs):
            return {"status": "ok", "items": [], "source_system": "shiguang"}

        monkeypatch.setattr(LibraryService, "filters", slow_filters)
        monkeypatch.setattr(CardBalanceService, "query", working_balance)
        monkeypatch.setattr(ShiguangService, "activities", working_activities)
        async with Client(create_server(tmp_path)) as client:
            pending = asyncio.create_task(client.call_tool("upc_library_filters", {}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                balance = await asyncio.wait_for(client.call_tool("upc_card_balance", {}), 2)
                activities = await asyncio.wait_for(
                    client.call_tool("upc_shiguang_activities", {}), 2
                )
                assert not balance.is_error
                assert not activities.is_error
                assert not pending.done()
            finally:
                release.set()
                await pending

    asyncio.run(scenario())


def test_library_modules_do_not_import_other_systems():
    script = (
        "import sys; import upc_mcp.library_service, upc_mcp.library_auth; "
        "assert not set(sys.modules).intersection({"
        "'upc_mcp.shiguang', 'upc_mcp.shiguang_service', 'upc_mcp.shiguang_session', "
        "'upc_mcp.card_balance', 'upc_mcp.card_balance_service', "
        "'upc_mcp.card_balance_profile', 'upc_mcp.jhzf', 'upc_mcp.auth', "
        "'upc_mcp.cdp', 'upc_mcp.storage', 'upc_mcp.models'})"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_one_library_parser_failure_does_not_disable_other_library_tools(
    tmp_path, monkeypatch
):
    async def broken_filters(self):
        raise LibraryError("schema_changed", "Synthetic changed library shape")

    async def working_reservations(self):
        return {
            "status": "empty",
            "source_system": "library",
            "empty_state": "data_null",
            "total": 0,
            "reservations": [],
        }

    monkeypatch.setattr(LibraryService, "filters", broken_filters)
    monkeypatch.setattr(LibraryService, "reservations", working_reservations)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            failed = await client.call_tool("upc_library_filters", {})
            assert failed.is_error
            assert failed.structured_content["status"] == "schema_changed"
            working = await client.call_tool("upc_library_reservations", {})
            assert not working.is_error
            assert working.structured_content["empty_state"] == "data_null"
            assert not (await client.call_tool("upc_capabilities", {})).is_error

    asyncio.run(scenario())
