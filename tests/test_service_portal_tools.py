from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from upc_mcp.service_portal_protocol import ServicePortalError
from upc_mcp.service_portal_tools import register_service_portal_tools


class FakeService:
    def __init__(self):
        self.calls = []

    async def search(self, *, keyword, page, page_size):
        self.calls.append(("search", keyword, page, page_size))
        return {
            "status": "ok",
            "source_system": "service_portal",
            "session_ref": "anonymous",
            "items": [],
            "total": 0,
        }

    async def guide(self, service_id):
        self.calls.append(("guide", service_id))
        return {
            "status": "ok",
            "source_system": "service_portal",
            "session_ref": "anonymous",
            "service_id": service_id,
            "materials": None,
        }

    async def my_todos(self, *, page):
        self.calls.append(("todos", page))
        if page != 1:
            raise ServicePortalError("invalid_input", "only page 1")
        return {
            "status": "ok",
            "source_system": "service_portal",
            "session_ref": "self",
            "empty": True,
            "items": [],
        }

    async def my_applications(self, *, page):
        self.calls.append(("applications", page))
        return {
            "status": "ok",
            "source_system": "service_portal",
            "session_ref": "self",
            "empty": False,
            "items": [],
        }


def build_server(service=None):
    server = MCPServer("service-portal-test")
    fake = service or FakeService()
    register_service_portal_tools(server, Path("."), service=fake)
    return server, fake


def test_inprocess_discovery_and_schemas():
    async def scenario():
        server, _ = build_server()
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            by_name = {tool.name: tool for tool in tools}
            assert set(by_name) == {
                "upc_service_search",
                "upc_service_guide",
                "upc_service_my_todos",
                "upc_service_my_applications",
            }
            search = by_name["upc_service_search"]
            assert set(search.input_schema["properties"]) == {
                "keyword",
                "page",
                "page_size",
            }
            assert search.input_schema.get("required", []) == []
            assert search.annotations.read_only_hint is True
            assert search.annotations.destructive_hint is False
            guide = by_name["upc_service_guide"]
            assert set(guide.input_schema["properties"]) == {"service_id"}
            assert guide.input_schema["required"] == ["service_id"]
            for name in ("upc_service_my_todos", "upc_service_my_applications"):
                tool = by_name[name]
                assert set(tool.input_schema["properties"]) == {"page", "session_ref"}
                assert tool.input_schema["properties"]["page"]["maximum"] == 1
                assert tool.input_schema["properties"]["page"]["minimum"] == 1
                schema = json.dumps(tool.input_schema).lower()
                for forbidden in ("url", "header", "cookie", "account", "token"):
                    assert forbidden not in schema

    asyncio.run(scenario())


def test_calls_and_fixed_error_mapping():
    async def scenario():
        server, fake = build_server()
        async with Client(server) as client:
            search = await client.call_tool(
                "upc_service_search", {"keyword": "事项", "page": 2, "page_size": 10}
            )
            assert not search.is_error
            assert search.structured_content["session_ref"] == "anonymous"
            guide = await client.call_tool("upc_service_guide", {"service_id": 603})
            assert not guide.is_error
            todos = await client.call_tool("upc_service_my_todos", {"page": 1})
            assert not todos.is_error
            assert todos.structured_content["empty"] is True
            apps = await client.call_tool("upc_service_my_applications", {"page": 1})
            assert not apps.is_error
            assert fake.calls == [
                ("search", "事项", 2, 10),
                ("guide", 603),
                ("todos", 1),
                ("applications", 1),
            ]
            # In-process schema bounds reject page=2 before the service call.
            invalid = await client.call_tool("upc_service_my_todos", {"page": 2})
            assert invalid.is_error

    asyncio.run(scenario())


def test_tool_error_never_echoes_upstream_message():
    class BadService(FakeService):
        async def my_todos(self, *, page):
            raise ServicePortalError(
                "schema_changed", "raw upstream private message"
            )

    async def scenario():
        server, _ = build_server(BadService())
        async with Client(server) as client:
            result = await client.call_tool("upc_service_my_todos", {"page": 1})
            assert result.is_error
            assert "raw upstream" not in json.dumps(
                result.structured_content, ensure_ascii=False
            )

    asyncio.run(scenario())
