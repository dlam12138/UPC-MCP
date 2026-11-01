"""Synthetic tests for the independent free-classroom adapter/service.

All room names, codes, capacities, and responses are fabricated. No school
endpoint is contacted by this module.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import sys
from urllib.parse import parse_qsl

import httpx
import pytest
from mcp import Client

from upc_mcp.card_balance_service import CardBalanceService
from upc_mcp.classroom_service import ClassroomService
from upc_mcp.classrooms import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    ClassroomError,
    ClassroomQuery,
    FreeClassroomClient,
    build_form,
    parse_response,
)
from upc_mcp.server import create_server
from upc_mcp.shiguang_service import ShiguangService

CAMPUS = "唐岛湾校区"
OTHER_CAMPUS = "古镇口校区"
DATE = "2026-10-30"
BUILDING = "东廊"
OTHER_BUILDING = "南教楼"


def room(
    code: str,
    *,
    name: str | None = None,
    building: str = BUILDING,
    campus: str = CAMPUS,
    num: object = "40",
) -> dict:
    item = {
        "code": code,
        "name": name or code,
        "jxlm": building,
        "xqm": campus,
    }
    if num is not None:
        item["num"] = num
    return item


def payload(
    *,
    all_groups: object = None,
    js_groups: object = None,
    buildings: object = None,
    e: object = 0,
    message: str = "操作成功",
) -> dict:
    if all_groups is None:
        all_groups = {BUILDING: [room("A101")]}
    if js_groups is None:
        js_groups = {}
    if buildings is None:
        buildings = (
            [{"id": key, "name": key} for key in all_groups]
            if isinstance(all_groups, dict)
            else []
        )
    return {"e": e, "m": message, "d": {"all": all_groups, "js": js_groups, "listLh": buildings}}


def json_handler(body: object, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers)

    return handler


def service_for(handler) -> ClassroomService:
    return ClassroomService(FreeClassroomClient(transport=httpx.MockTransport(handler)))


def run(service: ClassroomService, **overrides) -> dict:
    arguments = {
        "campus": CAMPUS,
        "date": DATE,
        "periods": [1],
        "building": "全部",
    }
    arguments.update(overrides)
    return asyncio.run(service.query(**arguments))


def test_form_is_utf8_sorted_and_uses_repeated_period_keys():
    query = ClassroomQuery.create(
        campus="唐岛湾",
        date=DATE,
        periods=[9, 1, 5, 1],
        building="全部",
    )
    body = build_form(query)
    pairs = parse_qsl(body, keep_blank_values=True)
    assert pairs[:3] == [("xq", CAMPUS), ("date", DATE), ("lh", "全部")]
    assert [value for key, value in pairs if key == "jc[]"] == ["1", "5", "9"]
    assert body == (
        "xq=%E5%94%90%E5%B2%9B%E6%B9%BE%E6%A0%A1%E5%8C%BA"
        "&date=2026-10-30&lh=%E5%85%A8%E9%83%A8"
        "&jc%5B%5D=1&jc%5B%5D=5&jc%5B%5D=9"
    )
    assert query.periods == (1, 5, 9)
    assert query.public()["selected_period_groups"] == ["上午", "下午"]


def test_period_boundaries_accept_one_through_thirteen():
    body = payload()
    result = run(service_for(json_handler(body)), periods=list(range(1, 14)))
    assert result["query"]["periods"] == list(range(1, 14))
    assert result["query"]["selected_period_groups"] == ["上午", "下午", "晚上"]


def test_request_is_fixed_anonymous_and_does_not_send_cookies():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = request.content
        return httpx.Response(200, json=payload())

    result = run(service_for(handler), periods=[2, 1])
    assert result["status"] == "ok"
    assert seen["method"] == "POST"
    assert seen["url"] == ENDPOINT
    headers = seen["headers"]
    assert headers["accept"] == "application/json"
    assert headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "cookie" not in headers
    assert "authorization" not in headers
    assert b"jc%5B%5D=1&jc%5B%5D=2" in seen["body"]


@pytest.mark.parametrize(
    "periods",
    [[], [0], [14], [True], [1, "2"], "1", None],
)
def test_invalid_periods_are_rejected_without_network(periods):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload())

    with pytest.raises(ClassroomError) as error:
        run(service_for(handler), periods=periods)
    assert error.value.code == "invalid_periods"
    assert calls == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"campus": "不存在"}, "invalid_campus"),
        ({"campus": None}, "invalid_campus"),
        ({"date": "2026/10/30"}, "invalid_date"),
        ({"date": "20261030"}, "invalid_date"),
        ({"date": "2026-02-30"}, "invalid_date"),
        ({"date": "1999-12-31"}, "invalid_date"),
        ({"building": "  "}, "invalid_building"),
        ({"building": None}, "invalid_building"),
    ],
)
def test_invalid_query_fields_are_rejected_without_network(overrides, code):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=payload())

    with pytest.raises(ClassroomError) as error:
        run(service_for(handler), **overrides)
    assert error.value.code == code
    assert called is False


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(-1, 10), (0, 0), (0, 501), (True, 10), (0, True)],
)
def test_invalid_pagination_is_rejected_without_network(offset, limit):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=payload())

    with pytest.raises(ClassroomError) as error:
        run(service_for(handler), offset=offset, limit=limit)
    assert error.value.code == "invalid_pagination"
    assert called is False


def test_multiple_buildings_occupancy_and_capacity():
    body = payload(
        all_groups={
            BUILDING: [room("A101"), room("A102", num="55")],
            OTHER_BUILDING: [room("B201", building=OTHER_BUILDING, num=None)],
        },
        js_groups={
            BUILDING: [{"code": "A102"}],
            OTHER_BUILDING: [{"code": "B201"}],
        },
        buildings=[
            {"id": BUILDING, "name": BUILDING},
            {"id": OTHER_BUILDING, "name": OTHER_BUILDING},
        ],
    )
    result = run(service_for(json_handler(body)), periods=[2, 1, 1])
    assert result["status"] == "ok"
    assert result["source_system"] == "classrooms"
    assert result["query"]["campus"] == CAMPUS
    assert result["query"]["date"] == DATE
    assert result["query"]["periods"] == [1, 2]
    assert result["query"]["selected_period_groups"] == ["上午"]
    assert result["query"]["only_available"] is False
    assert result["counts"] == {
        "total": 3,
        "available": 1,
        "occupied": 2,
        "matched": 3,
        "returned": 3,
    }
    assert result["truncated"] is False
    assert result["availability"] == "available"
    assert [item["code"] for item in result["rooms"]] == ["A101", "A102", "B201"]
    assert [item["occupied"] for item in result["rooms"]] == [False, True, True]
    assert [item["capacity"] for item in result["rooms"]] == [40, 55, None]
    assert result["buildings"] == ["全部", BUILDING, OTHER_BUILDING]
    assert result["building_options"][0] == {"id": "全部", "name": "全部"}
    assert result["period_groups"]["上午"] == [1, 2, 3, 4, 5]
    assert result["from_cache"] is False
    assert result["stale"] is False
    assert result["fetched_at"].endswith("+08:00")


def test_php_empty_directory_is_an_explicit_error_not_all_free():
    query = ClassroomQuery.create(campus=CAMPUS, date=DATE, periods=[1])
    for all_groups in ({}, []):
        body = payload(
            all_groups=all_groups,
            js_groups=[],
            buildings=[{"id": BUILDING, "name": BUILDING}],
        )
        directory = parse_response(json.dumps(body).encode(), query)
        assert directory.rooms == ()
        assert directory.public_buildings() == ["全部", BUILDING]

        with pytest.raises(ClassroomError) as error:
            run(service_for(json_handler(body)))
        assert error.value.code == "empty_directory"
        assert error.value.details["directory_empty"] is True
        assert error.value.details["buildings"] == ["全部", BUILDING]


def test_empty_selected_building_is_a_filter_empty_not_a_directory_error():
    body = payload(
        all_groups={},
        js_groups=[],
        buildings=[{"id": BUILDING, "name": BUILDING}],
    )
    result = run(service_for(json_handler(body)), building=BUILDING)
    assert result["status"] == "empty"
    assert result["reason"] == "no_rooms_for_building"
    assert result["availability"] == "no_rooms"
    assert result["counts"]["total"] == 0
    assert result["rooms"] == []


def test_all_occupied_is_reported_without_hiding_rooms():
    body = payload(
        all_groups={BUILDING: [room("A101"), room("A102")]},
        js_groups={BUILDING: [{"code": "A101"}, {"code": "A102"}]},
    )
    result = run(service_for(json_handler(body)))
    assert result["status"] == "ok"
    assert result["availability"] == "all_occupied"
    assert result["counts"]["available"] == 0
    assert result["counts"]["occupied"] == 2
    assert [item["occupied"] for item in result["rooms"]] == [True, True]

    filtered = run(service_for(json_handler(body)), only_available=True)
    assert filtered["status"] == "empty"
    assert filtered["reason"] == "all_occupied"
    assert filtered["counts"]["matched"] == 0
    assert filtered["rooms"] == []


def test_only_available_filters_cells_but_keeps_school_counts():
    body = payload(
        all_groups={BUILDING: [room("A101"), room("A102"), room("A103")]},
        js_groups={BUILDING: [{"code": "A102"}]},
    )
    result = run(service_for(json_handler(body)), only_available=True)
    assert result["status"] == "ok"
    assert result["query"]["only_available"] is True
    assert result["counts"] == {
        "total": 3,
        "available": 2,
        "occupied": 1,
        "matched": 2,
        "returned": 2,
    }
    assert [item["code"] for item in result["rooms"]] == ["A101", "A103"]


def test_pagination_reports_total_truncation_and_offset_errors():
    body = payload(
        all_groups={BUILDING: [room(f"A10{index}") for index in range(1, 6)]},
    )
    first = run(service_for(json_handler(body)), limit=2)
    assert first["status"] == "ok"
    assert first["counts"]["matched"] == 5
    assert first["counts"]["returned"] == 2
    assert first["pagination"] == {
        "offset": 0,
        "limit": 2,
        "total": 5,
        "returned": 2,
        "has_more": True,
        "next_offset": 2,
    }
    assert first["truncated"] is True
    assert any("分页截断" in item for item in first["limitations"])

    last = run(service_for(json_handler(body)), offset=4, limit=2)
    assert last["status"] == "ok"
    assert last["counts"]["returned"] == 1
    assert last["pagination"]["has_more"] is False
    assert last["pagination"]["next_offset"] is None
    assert last["truncated"] is False

    beyond = run(service_for(json_handler(body)), offset=10, limit=2)
    assert beyond["status"] == "empty"
    assert beyond["reason"] == "offset_out_of_range"
    assert beyond["counts"]["matched"] == 5
    assert beyond["counts"]["returned"] == 0
    assert beyond["rooms"] == []


def test_invalid_building_lists_real_options():
    body = payload(
        all_groups={BUILDING: [room("A101")]},
        buildings=[{"id": BUILDING, "name": BUILDING}],
    )
    with pytest.raises(ClassroomError) as error:
        run(service_for(json_handler(body)), building="不存在的楼")
    assert error.value.code == "invalid_building"
    assert error.value.details["buildings"] == ["全部", BUILDING]
    assert error.value.details["building_options"][1] == {"id": BUILDING, "name": BUILDING}


def test_mixed_building_response_is_schema_changed_when_filtered():
    body = payload(
        all_groups={
            BUILDING: [room("A101")],
            OTHER_BUILDING: [room("B201", building=OTHER_BUILDING)],
        },
        buildings=[
            {"id": BUILDING, "name": BUILDING},
            {"id": OTHER_BUILDING, "name": OTHER_BUILDING},
        ],
    )
    with pytest.raises(ClassroomError) as error:
        run(service_for(json_handler(body)), building=BUILDING)
    assert error.value.code == "schema_changed"


def test_capacity_missing_or_nonpositive_is_null():
    body = payload(
        all_groups={
            BUILDING: [
                room("A101", num=None),
                room("A102", num="0"),
                room("A103", num="not-a-number"),
                room("A104", num=42),
                room("A105", num="42"),
                room("A106", num={"bad": 1}),
                room("A107", num=True),
                room("A108", num=40.5),
                room("A109", num=40.0),
            ]
        },
    )
    result = run(service_for(json_handler(body)))
    capacities = {item["code"]: item["capacity"] for item in result["rooms"]}
    assert capacities == {
        "A101": None,
        "A102": None,
        "A103": None,
        "A104": 42,
        "A105": 42,
        "A106": None,
        "A107": None,
        "A108": None,
        "A109": None,
    }


def test_business_error_is_distinct_from_schema_change():
    body = {"e": 1, "m": "synthetic business failure", "d": {}}
    with pytest.raises(ClassroomError) as error:
        run(service_for(json_handler(body)))
    assert error.value.code == "business_error"
    assert error.value.details["upstream_code"] == 1
    assert "synthetic business failure" in error.value.details["upstream_message"]


def test_malformed_responses_are_schema_changed():
    valid_room = room("A101")
    cases = [
        {"m": "missing e", "d": {"all": {}, "js": {}, "listLh": []}},
        {"e": True, "m": "bool e", "d": {"all": {}, "js": {}, "listLh": []}},
        {"e": 0, "m": "missing d"},
        {"e": 0, "m": "d is list", "d": []},
        {"e": 0, "m": "missing all", "d": {"js": {}, "listLh": []}},
        {"e": 0, "m": "missing js", "d": {"all": {}, "listLh": []}},
        {"e": 0, "m": "missing listLh", "d": {"all": {}, "js": {}}},
        {
            "e": 0,
            "m": "all non-empty array",
            "d": {"all": [valid_room], "js": {}, "listLh": []},
        },
        {
            "e": 0,
            "m": "js non-empty array",
            "d": {"all": {}, "js": [{"code": "A101"}], "listLh": []},
        },
        {
            "e": 0,
            "m": "listLh object",
            "d": {"all": {}, "js": {}, "listLh": {}},
        },
        {
            "e": 0,
            "m": "listLh missing id",
            "d": {"all": {}, "js": {}, "listLh": [{"name": "东廊"}]},
        },
        {
            "e": 0,
            "m": "group is string",
            "d": {"all": {BUILDING: "bad"}, "js": {}, "listLh": []},
        },
        {
            "e": 0,
            "m": "room is string",
            "d": {"all": {BUILDING: ["bad"]}, "js": {}, "listLh": []},
        },
        {
            "e": 0,
            "m": "missing code",
            "d": {
                "all": {
                    BUILDING: [
                        {"name": "A101", "jxlm": BUILDING, "xqm": CAMPUS, "num": "40"}
                    ]
                },
                "js": {},
                "listLh": [],
            },
        },
        {
            "e": 0,
            "m": "group mismatch",
            "d": {
                "all": {BUILDING: [room("A101", building=OTHER_BUILDING)]},
                "js": {},
                "listLh": [],
            },
        },
        {
            "e": 0,
            "m": "campus mismatch",
            "d": {
                "all": {BUILDING: [room("A101", campus=OTHER_CAMPUS)]},
                "js": {},
                "listLh": [],
            },
        },
        {
            "e": 0,
            "m": "duplicate code",
            "d": {
                "all": {BUILDING: [room("A101"), room("A101")]},
                "js": {},
                "listLh": [],
            },
        },
        {
            "e": 0,
            "m": "duplicate code across groups",
            "d": {
                "all": {
                    BUILDING: [room("A101")],
                    OTHER_BUILDING: [room("A101", building=OTHER_BUILDING)],
                },
                "js": {},
                "listLh": [],
            },
        },
        {
            "e": 0,
            "m": "unknown occupied code",
            "d": {
                "all": {BUILDING: [room("A101")]},
                "js": {BUILDING: [{"code": "B999"}]},
                "listLh": [],
            },
        },
    ]
    for body in cases:
        with pytest.raises(ClassroomError) as error:
            run(service_for(json_handler(body)))
        assert error.value.code == "schema_changed", body


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (302, "redirect"),
        (401, "access_denied"),
        (403, "access_denied"),
        (429, "rate_limited"),
        (404, "upstream_error"),
        (500, "upstream_error"),
    ],
)
def test_http_statuses_are_classified_without_following_redirects(status, code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"location": "https://example.invalid/redirect"},
            json=payload(),
        )

    with pytest.raises(ClassroomError) as error:
        run(service_for(handler))
    assert error.value.code == code
    assert error.value.details["http_status"] == status
    assert error.value.details["query"]["periods"] == [1]


def test_timeout_is_reported_without_retry():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with pytest.raises(ClassroomError) as error:
        run(service_for(handler))
    assert error.value.code == "timeout"
    assert calls == 1


def test_configured_timeouts_are_ten_and_fifteen_seconds():
    client = FreeClassroomClient()
    assert client.timeout.connect == 10.0
    assert client.timeout.read == 15.0


def test_classroom_modules_do_not_import_other_systems():
    script = (
        "import sys; import upc_mcp.classrooms; import upc_mcp.classroom_service; "
        "assert not set(sys.modules).intersection({"
        "'upc_mcp.card_balance', 'upc_mcp.card_balance_service', "
        "'upc_mcp.card_balance_profile', 'upc_mcp.shiguang', "
        "'upc_mcp.shiguang_service', 'upc_mcp.shiguang_session', "
        "'upc_mcp.jhzf', 'upc_mcp.auth', 'upc_mcp.storage', 'upc_mcp.models', "
        "'upc_mcp.cdp'})"
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_network_and_tls_errors_are_distinct():
    def network_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic network failure")

    with pytest.raises(ClassroomError) as error:
        run(service_for(network_handler))
    assert error.value.code == "network_error"

    def tls_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed")

    with pytest.raises(ClassroomError) as error:
        run(service_for(tls_handler))
    assert error.value.code == "tls_error"


def test_declared_and_streamed_response_limits():
    body = json.dumps(payload()).encode()
    oversized = body + b" " * (MAX_RESPONSE_BYTES + 1 - len(body))

    def plain_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=oversized,
            headers={"content-type": "application/json"},
        )

    with pytest.raises(ClassroomError) as error:
        run(service_for(plain_handler))
    assert error.value.code == "response_too_large"

    compressed = gzip.compress(oversized)
    assert len(compressed) < MAX_RESPONSE_BYTES

    def gzip_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=compressed,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )

    with pytest.raises(ClassroomError) as error:
        run(service_for(gzip_handler))
    assert error.value.code == "response_too_large"


def test_concurrency_is_bounded_to_four_requests():
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return httpx.Response(200, json=payload())

    classroom_service = service_for(handler)

    async def scenario():
        queries = [
            classroom_service.query(campus=CAMPUS, date=DATE, periods=[1])
            for _ in range(8)
        ]
        return await asyncio.gather(*queries)

    results = asyncio.run(scenario())
    assert len(results) == 8
    assert peak == 4
    assert all(result["status"] == "ok" for result in results)


def test_mcp_classroom_error_does_not_block_other_tools(tmp_path, monkeypatch):
    async def failed_classroom(self, **kwargs):
        raise ClassroomError("network_error", "Synthetic classroom outage")

    async def working_balance(self):
        return {"status": "ok", "source_system": "card_balance", "data": {}}

    async def working_activities(self, **kwargs):
        return {"status": "ok", "items": []}

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "upc_free_classrooms" in names

            invalid = await client.call_tool(
                "upc_free_classrooms",
                {"campus": "invalid", "date": DATE, "periods": [1]},
            )
            assert invalid.is_error
            assert invalid.structured_content["status"] == "invalid_campus"

            monkeypatch.setattr(ClassroomService, "query", failed_classroom)
            monkeypatch.setattr(CardBalanceService, "query", working_balance)
            monkeypatch.setattr(ShiguangService, "activities", working_activities)

            failed = await client.call_tool(
                "upc_free_classrooms",
                {"campus": CAMPUS, "date": DATE, "periods": [1]},
            )
            assert failed.is_error
            assert failed.structured_content["status"] == "network_error"
            assert not (await client.call_tool("upc_card_balance", {})).is_error
            assert not (await client.call_tool("upc_shiguang_activities", {})).is_error

            capabilities = await client.call_tool("upc_capabilities", {})
            assert capabilities.structured_content["capabilities"]["classrooms"] == "implemented"
            assert (
                capabilities.structured_content["authentication_by_system"]["classrooms"]
                == "none_public_query"
            )

    asyncio.run(scenario())


def test_slow_classroom_call_does_not_block_balance(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_classroom(self, **kwargs):
            entered.set()
            await release.wait()
            raise ClassroomError("timeout", "Synthetic slow classroom")

        async def working_balance(self):
            return {"status": "ok", "source_system": "card_balance", "data": {}}

        monkeypatch.setattr(ClassroomService, "query", slow_classroom)
        monkeypatch.setattr(CardBalanceService, "query", working_balance)
        async with Client(create_server(tmp_path)) as client:
            pending = asyncio.create_task(
                client.call_tool(
                    "upc_free_classrooms",
                    {"campus": CAMPUS, "date": DATE, "periods": [1]},
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                balance = await asyncio.wait_for(client.call_tool("upc_card_balance", {}), 2)
                assert not balance.is_error
                assert not pending.done()
            finally:
                release.set()
                await pending

    asyncio.run(scenario())


def test_startup_and_discovery_do_not_call_the_classroom_network(tmp_path, monkeypatch):
    async def no_send(self, request, *args, **kwargs):
        raise AssertionError("startup/discovery must not access the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_send)

    async def scenario():
        async with Client(create_server(tmp_path)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "upc_free_classrooms" in names
            capabilities = await client.call_tool("upc_capabilities", {})
            assert not capabilities.is_error

    asyncio.run(scenario())
