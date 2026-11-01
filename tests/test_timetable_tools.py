from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from upc_mcp.timetable_protocol import TimetableError
from upc_mcp.timetable_tools import register_timetable_tools


class FakeService:
    async def terms(self):
        return {
            "status": "ok",
            "source_system": "timetable",
            "session_ref": "self",
            "terms": [{"year": "2026-2027", "term": "1"}],
            "current": {"year": "2026-2027", "term": "1", "week": 8},
        }

    async def week(self, year: str, term: str, week: int):
        if year != "2026-2027":
            raise TimetableError("invalid_input", "no such term")
        return {
            "status": "ok",
            "source_system": "timetable",
            "session_ref": "self",
            "term": {"year": year, "term": term},
            "week": week,
            "weekdays": ["2026-10-25"],
            "courses": [{"course_name": "示例课程", "weekday": 7}],
            "counts": {"courses": 1, "unique_courses": 1},
        }

    async def range(self, year: str, term: str, start_date: str, end_date: str):
        if year != "2026-2027":
            raise TimetableError("invalid_input", "no such term")
        return {
            "status": "ok",
            "source_system": "timetable",
            "session_ref": "self",
            "term": {"year": year, "term": term},
            "range": {"start_date": start_date, "end_date": end_date, "complete": True},
            "courses": [{"course_name": "示例课程", "date": start_date}],
            "counts": {"total": 1, "returned": 1, "complete": True},
            "complete": True,
        }


def build_server():
    server = MCPServer("timetable-test")
    register_timetable_tools(server, Path("."), service=FakeService())
    return server


def test_inprocess_tool_discovery_and_schemas():
    async def scenario():
        async with Client(build_server()) as client:
            tools = (await client.list_tools()).tools
            by_name = {tool.name: tool for tool in tools}
            assert set(by_name) == {
                "upc_timetable_terms",
                "upc_timetable_week",
                "upc_timetable_range",
            }
            week = by_name["upc_timetable_week"]
            assert week.annotations.read_only_hint is True
            assert week.annotations.destructive_hint is False
            assert set(week.input_schema["properties"]) == {
                "year",
                "term",
                "week",
                "session_ref",
            }
            assert set(week.input_schema["required"]) == {"year", "term", "week"}
            assert week.input_schema["properties"]["week"]["minimum"] == 1
            range_tool = by_name["upc_timetable_range"]
            assert range_tool.annotations.read_only_hint is True
            assert set(range_tool.input_schema["properties"]) == {
                "year",
                "term",
                "start_date",
                "end_date",
                "session_ref",
            }
            assert set(range_tool.input_schema["required"]) == {
                "year",
                "term",
                "start_date",
                "end_date",
            }
            assert "url" not in json.dumps(week.input_schema).lower()
            assert "cookie" not in json.dumps(week.input_schema).lower()

    asyncio.run(scenario())


def test_inprocess_tool_calls_and_error_mapping():
    async def scenario():
        async with Client(build_server()) as client:
            terms = await client.call_tool("upc_timetable_terms", {})
            assert terms.is_error is False
            assert terms.structured_content["terms"][0]["year"] == "2026-2027"
            week = await client.call_tool(
                "upc_timetable_week",
                {"year": "2026-2027", "term": "1", "week": 8},
            )
            assert week.is_error is False
            assert week.structured_content["counts"]["courses"] == 1
            ranged = await client.call_tool(
                "upc_timetable_range",
                {
                    "year": "2026-2027",
                    "term": "1",
                    "start_date": "2026-10-25",
                    "end_date": "2026-10-31",
                },
            )
            assert ranged.is_error is False
            assert ranged.structured_content["counts"]["total"] == 1
            assert ranged.structured_content["complete"] is True
            bad = await client.call_tool(
                "upc_timetable_week",
                {"year": "2099-2100", "term": "1", "week": 1},
            )
            assert bad.is_error is True
            assert bad.structured_content["status"] == "invalid_input"
            assert "no such term" in bad.structured_content["error"]["message"]

    asyncio.run(scenario())


def test_tool_does_not_echo_unexpected_exception_text():
    class BrokenService:
        async def terms(self):
            raise RuntimeError("sensitive upstream body")

        async def week(self, year: str, term: str, week: int):
            raise RuntimeError("sensitive upstream body")

    server = MCPServer("timetable-test")
    register_timetable_tools(
        server, Path("."), service=BrokenService()
    )

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool("upc_timetable_terms", {})
            assert result.is_error is True
            payload = json.dumps(result.structured_content, ensure_ascii=False)
            assert "sensitive upstream body" not in payload
            assert result.structured_content["status"] == "local_error"

    asyncio.run(scenario())


def test_timetable_failure_does_not_disable_other_systems(tmp_path, monkeypatch):
    from upc_mcp.server import create_server
    from upc_mcp.timetable_service import TimetableService

    async def broken_terms(self):
        raise TimetableError("schema_changed", "synthetic timetable change")

    monkeypatch.setattr(TimetableService, "terms", broken_terms)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            failed = await client.call_tool("upc_timetable_terms", {})
            assert failed.is_error is True
            assert failed.structured_content["status"] == "schema_changed"
            other = await client.call_tool(
                "upc_session_status", {"system": "shiguang"}
            )
            assert other.is_error is False
            assert other.structured_content["status"] == "auth_required"
            capabilities = await client.call_tool("upc_capabilities", {})
            assert capabilities.is_error is False
            assert capabilities.structured_content["status"] == "ok"

    asyncio.run(scenario())
