import asyncio
import json
import os
import sys

import pytest
from mcp import Client, StdioServerParameters


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_stdio_discovery_and_structured_auth_error(tmp_path, mode):
    async def scenario():
        launch = StdioServerParameters(
            command=sys.executable, args=["-m", "upc_mcp", "--state-dir", str(tmp_path), "serve"]
        )
        async with Client(launch, mode=mode) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {
                "upc_capabilities",
                "upc_session_status",
                "upc_card_balance",
                "upc_free_classrooms",
                "upc_power_buildings",
                "upc_power_remaining",
                "upc_library_filters",
                "upc_library_rooms",
                "upc_library_seats",
                "upc_library_times",
                "upc_library_reservations",
                "upc_library_book",
                "upc_service_search",
                "upc_service_guide",
                "upc_service_my_todos",
                "upc_service_my_applications",
                "upc_shiguang_activities",
                "upc_shiguang_activity_detail",
                "upc_shiguang_my_participations",
                "upc_shiguang_credits",
                "upc_shiguang_report_card",
                "upc_shiguang_register_activity",
                "upc_timetable_terms",
                "upc_timetable_week",
                "upc_timetable_range",
            }
            assert "upc_card_transactions" not in {t.name for t in tools}
            assert "upc_timetable_semester" not in {t.name for t in tools}
            service_search = next(t for t in tools if t.name == "upc_service_search")
            assert service_search.annotations.read_only_hint is True
            assert service_search.annotations.destructive_hint is False
            assert set(service_search.input_schema["properties"]) == {
                "keyword",
                "page",
                "page_size",
            }
            assert service_search.input_schema["properties"]["page"]["maximum"] == 50
            assert service_search.input_schema["properties"]["page_size"]["maximum"] == 50
            assert "service5" not in (service_search.description or "").lower()
            service_guide = next(t for t in tools if t.name == "upc_service_guide")
            assert set(service_guide.input_schema["properties"]) == {"service_id"}
            assert service_guide.input_schema["required"] == ["service_id"]
            for name in ("upc_service_my_todos", "upc_service_my_applications"):
                service_tool = next(t for t in tools if t.name == name)
                assert service_tool.annotations.read_only_hint is True
                assert set(service_tool.input_schema["properties"]) == {
                    "page",
                    "page_size",
                    "session_ref",
                }
                assert service_tool.input_schema["properties"]["page"]["maximum"] == 50
                assert service_tool.input_schema["properties"]["page_size"]["maximum"] == 50
            balance = next(t for t in tools if t.name == "upc_card_balance")
            assert balance.annotations.read_only_hint is True
            assert set(balance.input_schema["properties"]) == {"session_ref"}
            balance_error = await client.call_tool("upc_card_balance", {})
            assert balance_error.is_error
            assert balance_error.structured_content["status"] == "not_configured"
            balance_status = await client.call_tool(
                "upc_session_status", {"system": "card_balance"}
            )
            assert balance_status.structured_content["configured"] is False
            classrooms = next(t for t in tools if t.name == "upc_free_classrooms")
            assert classrooms.annotations.read_only_hint is True
            assert classrooms.annotations.destructive_hint is False
            assert set(classrooms.input_schema["properties"]) == {
                "campus",
                "date",
                "periods",
                "building",
                "buildings",
                "mode",
                "only_available",
                "include_conflicts",
                "offset",
                "limit",
            }
            assert set(classrooms.input_schema["required"]) == {
                "campus",
                "date",
                "periods",
            }
            classroom_error = await client.call_tool(
                "upc_free_classrooms",
                {"campus": "synthetic-invalid", "date": "2026-10-30", "periods": [1]},
            )
            assert classroom_error.is_error
            assert classroom_error.structured_content["status"] == "invalid_campus"
            power_buildings = next(t for t in tools if t.name == "upc_power_buildings")
            assert power_buildings.annotations.read_only_hint is True
            assert power_buildings.annotations.destructive_hint is False
            assert power_buildings.input_schema.get("properties", {}) == {}
            assert power_buildings.input_schema.get("required", []) == []
            power_remaining = next(t for t in tools if t.name == "upc_power_remaining")
            assert power_remaining.annotations.read_only_hint is True
            assert power_remaining.annotations.destructive_hint is False
            assert set(power_remaining.input_schema["properties"]) == {
                "building",
                "room",
            }
            assert set(power_remaining.input_schema["required"]) == {
                "building",
                "room",
            }
            power_schema = json.dumps(
                power_remaining.input_schema, ensure_ascii=False
            ).lower()
            for forbidden in (
                "campus",
                "url",
                "cookie",
                "password",
                "session_ref",
                "state_dir",
                "default_room",
                "guzhenkou",
                "古镇口",
            ):
                assert forbidden not in power_schema
            missing_power = await client.call_tool(
                "upc_power_remaining",
                {"building": "   ", "room": "SYNTH-ROOM-101"},
            )
            assert missing_power.is_error
            assert missing_power.structured_content["status"] == "missing_input"
            empty_power = await client.call_tool(
                "upc_power_remaining",
                {"building": "", "room": "SYNTH-ROOM-101"},
            )
            assert empty_power.is_error
            assert empty_power.structured_content["status"] == "missing_input"
            omitted_power = await client.call_tool(
                "upc_power_remaining", {"building": "SYNTH-BLD-900"}
            )
            assert omitted_power.is_error
            filters = next(t for t in tools if t.name == "upc_library_filters")
            assert filters.annotations.read_only_hint is True
            assert set(filters.input_schema["properties"]) == {"session_ref"}
            filters_error = await client.call_tool("upc_library_filters", {})
            assert filters_error.is_error
            assert filters_error.structured_content["status"] == "auth_required"
            rooms = next(t for t in tools if t.name == "upc_library_rooms")
            assert rooms.annotations.read_only_hint is True
            assert set(rooms.input_schema["properties"]) == {
                "building_id",
                "date",
                "floor",
                "offset",
                "limit",
                "session_ref",
            }
            assert set(rooms.input_schema["required"]) == {"building_id", "date"}
            seats = next(t for t in tools if t.name == "upc_library_seats")
            assert seats.annotations.read_only_hint is True
            assert set(seats.input_schema["properties"]) == {
                "room_id",
                "date",
                "only_available",
                "offset",
                "limit",
                "session_ref",
            }
            times_tool = next(t for t in tools if t.name == "upc_library_times")
            assert times_tool.annotations.read_only_hint is True
            assert set(times_tool.input_schema["properties"]) == {
                "seat_id",
                "date",
                "start_id",
                "session_ref",
            }
            reservations_tool = next(
                t for t in tools if t.name == "upc_library_reservations"
            )
            assert reservations_tool.annotations.read_only_hint is True
            assert set(reservations_tool.input_schema["properties"]) == {"session_ref"}
            book = next(t for t in tools if t.name == "upc_library_book")
            assert book.annotations.read_only_hint is False
            assert book.annotations.destructive_hint is False
            assert book.annotations.idempotent_hint is False
            assert set(book.input_schema["properties"]) == {
                "room_id",
                "seat_id",
                "date",
                "start_id",
                "end_id",
                "session_ref",
            }
            assert set(book.input_schema["required"]) == {
                "room_id",
                "seat_id",
                "date",
                "start_id",
                "end_id",
            }
            book_error = await client.call_tool(
                "upc_library_book",
                {
                    "room_id": "room-1",
                    "seat_id": "seat-1",
                    "date": "2026-11-01",
                    "start_id": "start-1",
                    "end_id": "end-1",
                },
            )
            assert book_error.is_error
            assert book_error.structured_content["status"] == "auth_required"
            timetable_terms = next(t for t in tools if t.name == "upc_timetable_terms")
            assert timetable_terms.annotations.read_only_hint is True
            assert timetable_terms.annotations.destructive_hint is False
            assert set(timetable_terms.input_schema["properties"]) == {"session_ref"}
            timetable_week = next(t for t in tools if t.name == "upc_timetable_week")
            assert timetable_week.annotations.read_only_hint is True
            assert timetable_week.annotations.destructive_hint is False
            assert set(timetable_week.input_schema["properties"]) == {
                "year",
                "term",
                "week",
                "session_ref",
            }
            assert set(timetable_week.input_schema["required"]) == {
                "year",
                "term",
                "week",
            }
            assert timetable_week.input_schema["properties"]["week"]["minimum"] == 1
            timetable_range = next(t for t in tools if t.name == "upc_timetable_range")
            assert timetable_range.annotations.read_only_hint is True
            assert set(timetable_range.input_schema["properties"]) == {
                "year",
                "term",
                "start_date",
                "end_date",
                "session_ref",
            }
            assert set(timetable_range.input_schema["required"]) == {
                "year",
                "term",
                "start_date",
                "end_date",
            }
            timetable_schema = json.dumps(
                timetable_week.input_schema, ensure_ascii=False
            ).lower()
            assert "url" not in timetable_schema
            assert "cookie" not in timetable_schema
            assert "password" not in timetable_schema
            timetable_terms_error = await client.call_tool("upc_timetable_terms", {})
            assert timetable_terms_error.is_error
            assert timetable_terms_error.structured_content["status"] == "auth_required"
            timetable_week_error = await client.call_tool(
                "upc_timetable_week",
                {"year": "2026-2027", "term": "1", "week": 1},
            )
            assert timetable_week_error.is_error
            assert timetable_week_error.structured_content["status"] == "auth_required"
            timetable_range_error = await client.call_tool(
                "upc_timetable_range",
                {
                    "year": "2026-2027",
                    "term": "1",
                    "start_date": "2026-11-08",
                    "end_date": "2026-11-14",
                },
            )
            assert timetable_range_error.is_error
            assert (
                timetable_range_error.structured_content["status"]
                == "auth_required"
            )
            assert json.loads(timetable_week_error.content[0].text) == (
                timetable_week_error.structured_content
            )
            activities = next(t for t in tools if t.name == "upc_shiguang_activities")
            assert activities.annotations.read_only_hint is True
            assert set(activities.input_schema["properties"]) == {
                "page",
                "keyword",
                "online_type",
                "base_type",
                "session_ref",
            }
            detail = next(t for t in tools if t.name == "upc_shiguang_activity_detail")
            assert detail.annotations.read_only_hint is True
            assert set(detail.input_schema["properties"]) == {
                "activity_id",
                "kind",
                "session_ref",
            }
            mine = next(t for t in tools if t.name == "upc_shiguang_my_participations")
            assert mine.annotations.read_only_hint is True
            assert set(mine.input_schema["properties"]) == {"page", "session_ref"}
            credits = next(t for t in tools if t.name == "upc_shiguang_credits")
            assert credits.annotations.read_only_hint is True
            assert set(credits.input_schema["properties"]) == {"session_ref"}
            report_card = next(t for t in tools if t.name == "upc_shiguang_report_card")
            assert report_card.annotations.read_only_hint is True
            assert set(report_card.input_schema["properties"]) == {"session_ref"}
            register = next(t for t in tools if t.name == "upc_shiguang_register_activity")
            assert register.annotations.read_only_hint is False
            assert register.annotations.destructive_hint is False
            assert register.annotations.idempotent_hint is False
            assert set(register.input_schema["properties"]) == {
                "activity_id",
                "kind",
                "invite_code",
                "reason",
                "session_ref",
            }
            status_tool = next(t for t in tools if t.name == "upc_session_status")
            assert set(status_tool.input_schema["properties"]) == {"session_ref", "system"}
            assert "timetable" in status_tool.input_schema["properties"]["system"]["enum"]
            shiguang_error = await client.call_tool("upc_shiguang_activities", {})
            assert shiguang_error.is_error
            assert shiguang_error.structured_content["status"] == "auth_required"
            credits_error = await client.call_tool("upc_shiguang_credits", {})
            assert credits_error.is_error
            assert credits_error.structured_content["status"] == "auth_required"
            report_error = await client.call_tool("upc_shiguang_report_card", {})
            assert report_error.is_error
            assert report_error.structured_content["status"] == "auth_required"
            register_error = await client.call_tool(
                "upc_shiguang_register_activity", {"activity_id": "1"}
            )
            assert register_error.is_error
            assert register_error.structured_content["status"] == "auth_required"
            status = await client.call_tool("upc_session_status", {"system": "shiguang"})
            assert status.structured_content["status"] == "auth_required"
            assert status.structured_content["source_system"] == "shiguang"
            library_status = await client.call_tool(
                "upc_session_status", {"system": "library"}
            )
            assert library_status.structured_content["status"] == "auth_required"
            assert library_status.structured_content["source_system"] == "library"
            timetable_status = await client.call_tool(
                "upc_session_status", {"system": "timetable"}
            )
            assert timetable_status.structured_content["status"] == "auth_required"
            assert timetable_status.structured_content["source_system"] == "timetable"
            assert timetable_status.structured_content["session"] is None
            assert timetable_status.is_error is False
            assert json.loads(shiguang_error.content[0].text) == shiguang_error.structured_content
            capabilities = await client.call_tool("upc_capabilities", {})
            settings = capabilities.structured_content["settings_ui"]
            assert settings["implemented"] is True
            assert settings["args"][-1] == "settings"
            assert settings["loopback_only"] is True
            assert not capabilities.is_error
            assert capabilities.structured_content["capabilities"]["card_transactions"] == "retired"
            assert capabilities.structured_content["capabilities"]["power"] == "implemented"
            assert (
                capabilities.structured_content["capabilities"]["power_campus"]
                == "唐岛湾"
            )
            assert (
                capabilities.structured_content["capabilities"]["power_unit"] == "kWh"
            )
            assert (
                capabilities.structured_content["capabilities"]["power_persistence"]
                == "none"
            )
            assert (
                capabilities.structured_content["capabilities"][
                    "power_other_campuses"
                ]
                == "not_implemented"
            )
            assert (
                capabilities.structured_content["authentication_by_system"]["power"]
                == "none_public_form"
            )
            assert (
                capabilities.structured_content[
                    "automatic_session_refresh_by_system"
                ]["power"]
                == "not_required"
            )
            assert "upc_power_buildings" in capabilities.structured_content["read_only_tools"]
            assert "upc_power_remaining" in capabilities.structured_content["read_only_tools"]
            assert capabilities.structured_content["capabilities"]["classrooms"] == "implemented"
            assert (
                capabilities.structured_content["capabilities"]["classrooms_overview"]
                == "directory_only"
            )
            assert (
                capabilities.structured_content["capabilities"][
                    "classrooms_occupancy_source"
                ]
                == "detail_only"
            )
            assert (
                capabilities.structured_content["capabilities"][
                    "classrooms_search_all_occupancy"
                ]
                == "ignored"
            )
            assert (
                capabilities.structured_content["capabilities"]["classrooms_conflicts"]
                == "none"
            )
            assert capabilities.structured_content["capabilities"]["library"] == "implemented"
            assert (
                capabilities.structured_content["capabilities"]["library_booking"]
                == "implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["library_cancel"]
                == "not_implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["library_checkin"]
                == "not_implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["library_auto_grab"]
                == "not_implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["timetable"]
                == "implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["timetable_terms"]
                == "implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["timetable_week"]
                == "implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["timetable_semester"]
                == "not_implemented"
            )
            assert (
                capabilities.structured_content["authentication_by_system"]["timetable"]
                == "local_dpapi_business_cookie_or_unified_cas_auto_auth"
            )
            assert (
                capabilities.structured_content["authentication_by_system"]["classrooms"]
                == "none_public_query"
            )
            assert "upc_free_classrooms" in capabilities.structured_content["read_only_tools"]
            assert "upc_library_filters" in capabilities.structured_content["read_only_tools"]
            assert "upc_timetable_terms" in capabilities.structured_content["read_only_tools"]
            assert "upc_timetable_week" in capabilities.structured_content["read_only_tools"]
            assert (
                "upc_service_search"
                in capabilities.structured_content["read_only_tools"]
            )
            assert "upc_service_guide" in capabilities.structured_content["read_only_tools"]
            assert (
                "upc_service_my_todos"
                in capabilities.structured_content["read_only_tools"]
            )
            assert (
                "upc_service_my_applications"
                in capabilities.structured_content["read_only_tools"]
            )
            assert (
                capabilities.structured_content["service_portal_identity_verified"]
                is False
            )
            assert (
                capabilities.structured_content["capabilities"][
                    "service_portal_search_scope"
                ]
                == "saved_identity_catalog_or_explicit_public_catalog"
            )
            assert "upc_library_book" in capabilities.structured_content["write_tools"]
            assert capabilities.structured_content["read_only"] is False
            assert capabilities.structured_content["query_tools_read_only"] is True
            assert capabilities.structured_content["write_tools"] == [
                "upc_library_book",
                "upc_shiguang_register_activity",
            ]
            assert (
                capabilities.structured_content["capabilities"][
                    "shiguang_register_activity"
                ]
                == "implemented"
            )
            assert (
                capabilities.structured_content["interactions"]["checkin"]
                == "not_implemented"
            )
            assert capabilities.structured_content["automatic_session_refresh"] is False
            assert (
                capabilities.structured_content["automatic_session_refresh_by_system"][
                    "library"
                ]
                == "auto_auth_after_auth_expiry"
            )
            assert (
                capabilities.structured_content["automatic_session_refresh_by_system"][
                    "timetable"
                ]
                == "auto_auth_after_10013"
            )
            assert (
                capabilities.structured_content["credential_login"]["timetable"]
                == "implemented"
            )
            assert capabilities.structured_content["timetable_identity_verified"] is False
            assert (
                capabilities.structured_content["capabilities"]["shiguang_credits"]
                == "implemented"
            )
            assert (
                capabilities.structured_content["capabilities"]["shiguang_report_card"]
                == "implemented"
            )
            assert (
                capabilities.structured_content["automatic_session_refresh_by_system"]["shiguang"]
                == "conditional_cas_sso_or_auto_auth"
            )

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_stdio_lists_tools_with_corrupt_timetable_session(tmp_path):
    (tmp_path / "timetable-self.dpapi").write_bytes(b"not a dpapi blob")

    async def scenario():
        launch = StdioServerParameters(
            command=sys.executable, args=["-m", "upc_mcp", "--state-dir", str(tmp_path), "serve"]
        )
        async with Client(launch, mode="auto") as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 25
            assert "upc_timetable_terms" in {t.name for t in tools}
            status = await client.call_tool(
                "upc_session_status", {"system": "timetable"}
            )
            assert status.is_error is True
            assert status.structured_content["status"] == "auth_required"
            assert status.structured_content["source_system"] == "timetable"
            assert (
                status.structured_content["error"]["action"]
                == "complete_official_login"
            )
            capabilities = await client.call_tool("upc_capabilities", {})
            assert not capabilities.is_error
            assert capabilities.structured_content["status"] == "ok"

    asyncio.run(scenario())
