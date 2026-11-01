from __future__ import annotations

import asyncio

import httpx
from mcp import Client

from upc_mcp.server import create_server


def test_corrupt_service_session_is_local_and_does_not_block_discovery(
    tmp_path, monkeypatch
):
    (tmp_path / "service-portal-self.dpapi").write_bytes(b"invalid-dpapi")

    def no_network(*args, **kwargs):
        raise AssertionError("startup/discovery/status must not access the network")

    monkeypatch.setattr(httpx.Client, "send", no_network)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {
                "upc_service_search",
                "upc_service_guide",
                "upc_service_my_todos",
                "upc_service_my_applications",
            } <= names
            assert "upc_card_transactions" not in names
            capabilities = await client.call_tool("upc_capabilities", {})
            assert not capabilities.is_error
            assert (
                capabilities.structured_content["capabilities"][
                    "service_portal_search"
                ]
                == "implemented"
            )
            assert (
                capabilities.structured_content["capabilities"][
                    "service_portal_writes"
                ]
                == "not_implemented"
            )
            status = await client.call_tool(
                "upc_session_status", {"system": "service_portal"}
            )
            assert not status.is_error
            assert status.structured_content["status"] == "auth_required"
            assert status.structured_content["identity_verified"] is False
            # Other authenticated systems remain discoverable and independent.
            assert "upc_timetable_week" in names
            assert "upc_library_filters" in names

    asyncio.run(scenario())
