import asyncio
import gzip
import json
import logging
from inspect import getsource
from urllib.parse import parse_qsl

import httpx
import pytest

from upc_mcp import electricity
from upc_mcp.electricity import (
    Ambiguous,
    BuildingOption,
    Candidate,
    ElectricityClient,
    ElectricityError,
    Reading,
    build_form_body,
    format_kwh,
    parse_form,
    parse_kwh,
    parse_result,
    validate_input,
)
from upc_mcp.electricity_service import ElectricityService

# All names and values are synthetic and deliberately unrelated to UPC rooms.
BUILDING = "SYNTH-BLD-900"
OTHER_BUILDING = "SYNTH-BLD-901"
ROOM = "SYNTH-ROOM-101"
OTHER_ROOM = "SYNTH-ROOM-1101"
SYSTEM = "SYNTH-SYS-0001"
OTHER_SYSTEM = "SYNTH-SYS-0002"


def form_page(
    *,
    buildings: tuple[str, ...] = (BUILDING, OTHER_BUILDING),
    view_state: str = "SYNTH-VS",
    validation: str = "SYNTH-EV",
) -> str:
    options = "".join(
        f'<option value="{building}">{building} label</option>' for building in buildings
    )
    return f"""<!doctype html><html><head><title>synthetic</title></head><body>
<form method="post" action="./Default.aspx?code=0" id="form1">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="{view_state}" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="SYNTH-GEN" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="{validation}" />
<select name="lou" id="lou">{options}</select>
<input name="yonghu" type="text" id="yonghu" />
<input type="submit" name="chaxun" value="查询" />
<input type="submit" name="chongzhi" value="重置" />
</form>
<script>function chaxunClick() {{
 if ($("#lou option").size() < 1) {{ alert("楼列表为空,请联系系统管理员"); return false; }}
}}</script>
</body></html>"""


def grid_page(
    kwh: str = "17.34度",
    *,
    room: str = ROOM,
    system_no: str = SYSTEM,
    extra_hidden: str = "",
) -> str:
    return f"""<html><body><form id="form1">
<table id="yongdianGrid">
<tr><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
<tr><td><span>当前剩余电量:</span></td><td>{kwh}</td>
<td>{system_no}</td><td>{room}</td>{extra_hidden}</tr>
</table></form></body></html>"""


def candidate_page(*rows: tuple[str, str, str]) -> str:
    body = "".join(
        f"<tr><td>{system_no}</td><td>{building}</td><td>{room}</td>"
        "<td><input type=\"submit\" value=\"选择\" /></td></tr>"
        for system_no, building, room in rows
    )
    return f"""<html><body><form id="form1">
<table id="yonghuGridView">
<tr><th>系统编号</th><th>楼号</th><th>房间号</th><th>查询</th></tr>
{body}
</table></form></body></html>"""


def test_parse_form_reads_live_style_hidden_fields_and_buildings():
    form = parse_form(form_page(view_state="VS&amp;TOKEN"))
    assert form.view_state == "VS&TOKEN"
    assert form.view_state_generator == "SYNTH-GEN"
    assert form.event_validation == "SYNTH-EV"
    assert [option.id for option in form.buildings] == [BUILDING, OTHER_BUILDING]
    assert [option.name for option in form.buildings] == [
        f"{BUILDING} label",
        f"{OTHER_BUILDING} label",
    ]


def test_parse_form_accepts_value_before_name_and_selected_option():
    page = form_page().replace(
        '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="SYNTH-VS" />',
        '<input type="hidden" value="VS-2" name="__VIEWSTATE" />',
    ).replace(f'<option value="{BUILDING}">', f'<option selected value="{BUILDING}">')
    form = parse_form(page)
    assert form.view_state == "VS-2"
    assert form.buildings[0].id == BUILDING


def test_parse_form_wechat_redirect_is_auth_required_not_empty_form():
    with pytest.raises(ElectricityError) as error:
        parse_form('<html><body>redirecting to https://open.weixin.qq.com/oauth</body></html>')
    assert error.value.code == "auth_required"


def test_parse_form_broken_or_empty_is_schema_changed():
    with pytest.raises(ElectricityError) as error:
        parse_form("<html><body>no form</body></html>")
    assert error.value.code == "schema_changed"
    with pytest.raises(ElectricityError) as error:
        parse_form(form_page().replace('name="__EVENTVALIDATION"', 'name="__OTHER"'))
    assert error.value.code == "schema_changed"
    with pytest.raises(ElectricityError) as error:
        parse_form(form_page(buildings=()))
    assert error.value.code == "schema_changed"


def test_parse_form_rejects_duplicate_inconsistent_building_ids():
    page = form_page().replace(
        f'<option value="{OTHER_BUILDING}">{OTHER_BUILDING} label</option>',
        f'<option value="{BUILDING}">different label</option>',
    )
    with pytest.raises(ElectricityError) as error:
        parse_form(page)
    assert error.value.code == "schema_changed"


def test_form_body_has_exact_fields_and_never_includes_reset_or_recharge():
    form = parse_form(form_page())
    body = build_form_body(form, BUILDING, ROOM)
    fields = parse_qsl(body, keep_blank_values=True)
    assert [key for key, _value in fields] == [
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "lou",
        "yonghu",
        "chaxun",
    ]
    assert dict(fields)["chaxun"] == "查询"
    assert "chongzhi" not in body
    assert "chong" not in body.lower()


def test_unique_grid_reading_is_kwh_not_money():
    reading = parse_result(grid_page(), BUILDING, ROOM)
    assert isinstance(reading, Reading)
    assert reading.room == ROOM
    assert reading.system_no == SYSTEM
    assert format_kwh(reading.kwh) == "17.34"
    public = reading.public()
    assert public["reading_kind"] == "remaining_electricity"
    assert public["remaining_kwh"] == "17.34"
    assert public["unit"] == "kWh"
    assert public["unit_label"] == "度"
    assert public["is_money_balance"] is False
    assert public["money_balance"] is None
    assert public["arrears"] is None
    assert public["upstream_updated_at"] is None


@pytest.mark.parametrize("raw,expected", [
    ("0度", "0"),
    ("146度", "146"),
    ("0.00度", "0.00"),
    ("17.3400度", "17.3400"),
    ("17.34 度", "17.34"),
])
def test_parse_kwh_accepts_observed_degree_forms(raw, expected):
    assert format_kwh(parse_kwh(raw)) == expected


@pytest.mark.parametrize(
    "raw",
    ["17.34元", "-1度", "1,234度", "", "NaN度", "1e2度", "17.34kWh", "  "],
)
def test_parse_kwh_rejects_money_negative_and_unknown_units(raw):
    with pytest.raises(ElectricityError) as error:
        parse_kwh(raw)
    assert error.value.code == "schema_changed"


def test_grid_without_system_and_room_attribution_is_not_a_reading():
    page = """<form id="form1"><table id="yongdianGrid">
    <tr><td>当前剩余电量:</td><td>9.9度</td></tr></table></form>"""
    with pytest.raises(ElectricityError) as error:
        parse_result(page, BUILDING, ROOM)
    assert error.value.code == "schema_changed"


def test_exact_candidate_and_matching_grid_returns_reading():
    page = grid_page() + candidate_page((SYSTEM, BUILDING, ROOM))
    reading = parse_result(page, BUILDING, ROOM)
    assert isinstance(reading, Reading)
    assert reading.system_no == SYSTEM
    assert reading.kwh == parse_kwh("17.34度")


def test_fuzzy_candidate_never_adopts_first_room_reading():
    page = grid_page(room=OTHER_ROOM, system_no=OTHER_SYSTEM) + candidate_page(
        (OTHER_SYSTEM, BUILDING, OTHER_ROOM),
        (SYSTEM, BUILDING, ROOM),
    )
    outcome = parse_result(page, BUILDING, ROOM)
    assert isinstance(outcome, Ambiguous)
    assert outcome.reason == "reading_ownership_mismatch"
    assert [candidate.room for candidate in outcome.candidates] == [OTHER_ROOM, ROOM]
    assert "kwh" not in outcome.public()


def test_exact_candidate_without_grid_requires_manual_confirmation():
    outcome = parse_result(candidate_page((SYSTEM, BUILDING, ROOM)), BUILDING, ROOM)
    assert isinstance(outcome, Ambiguous)
    assert outcome.reason == "selection_required"
    assert outcome.public()["candidate_count"] == 1


def test_candidate_with_same_room_in_other_building_is_not_exact():
    outcome = parse_result(
        candidate_page((SYSTEM, OTHER_BUILDING, ROOM)),
        BUILDING,
        ROOM,
    )
    assert isinstance(outcome, Ambiguous)
    assert outcome.reason == "no_exact_candidate"


def test_multiple_exact_candidates_are_ambiguous():
    outcome = parse_result(
        candidate_page(
            (SYSTEM, BUILDING, ROOM),
            (OTHER_SYSTEM, BUILDING, ROOM),
        ),
        BUILDING,
        ROOM,
    )
    assert isinstance(outcome, Ambiguous)
    assert outcome.reason == "multiple_exact_candidates"


def test_grid_other_room_without_candidates_is_ambiguous():
    outcome = parse_result(grid_page(room=OTHER_ROOM), BUILDING, ROOM)
    assert isinstance(outcome, Ambiguous)
    assert outcome.reason == "room_mismatch"
    assert outcome.candidates[0].room == OTHER_ROOM


def test_no_result_page_with_live_javascript_is_room_not_found_not_schema_change():
    # The real form page always contains "楼列表为空" inside chaxunClick().
    page = form_page()
    with pytest.raises(ElectricityError) as error:
        parse_result(page, BUILDING, ROOM)
    assert error.value.code == "room_not_found"
    assert "楼列表为空" not in str(error.value)


def test_unrelated_or_truncated_page_is_schema_changed():
    with pytest.raises(ElectricityError) as error:
        parse_result("<html><body>unrelated</body></html>", BUILDING, ROOM)
    assert error.value.code == "schema_changed"
    with pytest.raises(ElectricityError) as error:
        parse_result('<table id="yongdianGrid">', BUILDING, ROOM)
    assert error.value.code == "schema_changed"


def test_duplicate_result_tables_are_schema_changed():
    with pytest.raises(ElectricityError) as error:
        parse_result(grid_page() + grid_page(), BUILDING, ROOM)
    assert error.value.code == "schema_changed"


@pytest.mark.parametrize(
    "building,room,code",
    [
        (None, ROOM, "missing_input"),
        (BUILDING, None, "missing_input"),
        ("", ROOM, "missing_input"),
        (BUILDING, "   ", "missing_input"),
        (123, ROOM, "invalid_input"),
        (BUILDING, 123, "invalid_input"),
        ("B" * 101, ROOM, "invalid_input"),
        (BUILDING, "R\n101", "invalid_input"),
    ],
)
def test_input_validation_never_supplies_a_default(building, room, code):
    with pytest.raises(ElectricityError) as error:
        validate_input(building, room)
    assert error.value.code == code


def test_input_validation_trims_but_does_not_rewrite_room_identity():
    assert validate_input(f"  {BUILDING}  ", f"  {ROOM}  ") == (BUILDING, ROOM)


def test_client_uses_one_fixed_get_and_one_fixed_post_without_cookie():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "cookie" not in request.headers
        if request.method == "GET":
            return httpx.Response(
                200,
                text=form_page(),
                headers={"Set-Cookie": "ASP.NET_SessionId=synthetic"},
            )
        return httpx.Response(200, text=grid_page())

    outcome = asyncio.run(
        ElectricityClient(httpx.MockTransport(handler)).query(BUILDING, ROOM)
    )
    assert isinstance(outcome, Reading)
    assert len(calls) == 2
    assert str(calls[0].url) == "http://dfcz.upc.edu.cn/ShiYou/Default.aspx?code=0"
    assert str(calls[1].url) == "http://dfcz.upc.edu.cn/ShiYou/Default.aspx"
    fields = parse_qsl(calls[1].content.decode("utf-8"), keep_blank_values=True)
    assert dict(fields)["lou"] == BUILDING
    assert dict(fields)["yonghu"] == ROOM
    assert "chongzhi" not in calls[1].content.decode("utf-8")


def test_client_buildings_only_gets_the_form():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=form_page())

    options = asyncio.run(
        ElectricityClient(httpx.MockTransport(handler)).fetch_buildings()
    )
    assert [option.id for option in options] == [BUILDING, OTHER_BUILDING]
    assert len(calls) == 1
    assert calls[0].method == "GET"


def test_invalid_building_is_rejected_before_post():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=form_page())

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).query("not-a-building", ROOM)
        )
    assert error.value.code == "invalid_building"
    assert error.value.details["available_buildings"] == [BUILDING, OTHER_BUILDING]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,location,code",
    [
        (500, None, "upstream_error"),
        (403, None, "access_denied"),
        (429, None, "rate_limited"),
        (302, "https://example.invalid/", "redirect"),
        (302, "https://open.weixin.qq.com/connect/oauth2", "auth_required"),
    ],
)
def test_http_failures_are_classified_and_not_retried(status, location, code):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        headers = {"Location": location} if location else {}
        return httpx.Response(status, headers=headers)

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).query(BUILDING, ROOM)
        )
    assert error.value.code == code
    assert len(calls) == 1
    assert "SYNTH-ROOM-101" not in json.dumps(error.value.payload(), ensure_ascii=False)


def test_post_failure_is_not_retried_or_turned_into_zero():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=form_page())
        return httpx.Response(500)

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).query(BUILDING, ROOM)
        )
    assert error.value.code == "upstream_error"
    assert len(calls) == 2


def test_post_no_result_is_room_not_found_not_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=form_page())
        return httpx.Response(200, text=form_page())

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).query(BUILDING, ROOM)
        )
    assert error.value.code == "room_not_found"
    assert error.value.details["query"]["room"] == ROOM


@pytest.mark.parametrize("failure,code", [
    (httpx.ConnectError("synthetic network"), "network_error"),
    (httpx.ReadTimeout("synthetic timeout"), "timeout"),
])
def test_network_failures_are_not_retried(failure, code):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise failure

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).query(BUILDING, ROOM)
        )
    assert error.value.code == code
    assert len(calls) == 1
    assert "SYNTH-ROOM-101" not in json.dumps(error.value.payload(), ensure_ascii=False)


def test_decoded_response_size_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(electricity, "MAX_RESPONSE_BYTES", 100)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(b"x" * 101),
            headers={"Content-Encoding": "gzip", "Content-Length": "30"},
        )

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).fetch_buildings()
        )
    assert error.value.code == "response_too_large"


def test_error_does_not_echo_request_url_or_upstream_body():
    secret = "https://untrusted.invalid/synthetic?room=SYNTH-ROOM-101"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=secret)

    with pytest.raises(ElectricityError) as error:
        asyncio.run(
            ElectricityClient(httpx.MockTransport(handler)).query(BUILDING, ROOM)
        )
    serialized = json.dumps(error.value.payload(), ensure_ascii=False)
    assert "untrusted.invalid" not in serialized
    assert "SYNTH-ROOM-101" not in serialized
    assert "untrusted.invalid" not in str(error.value)


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome

    async def fetch_buildings(self):
        return (BuildingOption(id=BUILDING, name="Synthetic Building"),)

    async def query(self, building, room):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_service_result_separates_electricity_from_money_and_arrears():
    reading = Reading(
        building=BUILDING,
        room=ROOM,
        system_no=SYSTEM,
        kwh=parse_kwh("17.34度"),
    )
    payload = asyncio.run(ElectricityService(FakeClient(reading)).query(BUILDING, ROOM))
    assert payload["status"] == "ok"
    assert payload["source_system"] == "electricity"
    assert payload["campus"] == "唐岛湾"
    assert payload["query"]["building"] == BUILDING
    assert payload["query"]["room"] == ROOM
    assert payload["query"]["scope"] == "explicit_single_room"
    assert payload["persistence"] == "none"
    assert payload["from_cache"] is False
    assert payload["stale"] is False
    assert payload["fetched_at"].endswith("+08:00")
    data = payload["data"]
    assert data["remaining_kwh"] == "17.34"
    assert data["unit"] == "kWh"
    assert data["is_money_balance"] is False
    assert data["money_balance"] is None
    assert data["money_balance_status"] == "not_provided_by_source"
    assert data["arrears"] is None
    assert data["arrears_status"] == "not_provided_by_source"
    assert data["upstream_updated_at"] is None
    assert "balance" not in data
    assert payload["source"]["transport"] == "http"
    assert payload["source"]["retries"] == 0


def test_service_ambiguous_never_contains_a_reading():
    outcome = Ambiguous(
        candidates=(Candidate(system_no=SYSTEM, building=BUILDING, room=ROOM),),
        reason="selection_required",
    )
    payload = asyncio.run(ElectricityService(FakeClient(outcome)).query(BUILDING, ROOM))
    assert payload["status"] == "ambiguous"
    assert payload["reading_available"] is False
    assert payload["data"] is None
    assert payload["candidate_count"] == 1
    assert payload["persistence"] == "none"
    assert "remaining_kwh" not in json.dumps(payload)


def test_service_buildings_returns_no_rooms():
    payload = asyncio.run(ElectricityService(FakeClient(None)).buildings())
    assert payload["status"] == "ok"
    assert payload["campus"] == "唐岛湾"
    assert payload["count"] == 1
    assert payload["buildings"] == [{"id": BUILDING, "name": "Synthetic Building"}]
    assert payload["rooms_enumerated"] is False
    assert "rooms" not in payload


def test_service_input_is_validated_before_any_client_call():
    calls: list[tuple[object, object]] = []

    class RecordingClient:
        async def fetch_buildings(self):
            calls.append(("buildings", None))
            return ()

        async def query(self, building, room):
            calls.append((building, room))
            raise AssertionError("client must not be called")

    service = ElectricityService(RecordingClient())
    for building, room, code in (
        (None, ROOM, "missing_input"),
        (BUILDING, "", "missing_input"),
        ("B" * 101, ROOM, "invalid_input"),
    ):
        with pytest.raises(ElectricityError) as error:
            asyncio.run(service.query(building, room))
        assert error.value.code == code
    assert calls == []


def test_service_does_not_write_or_log_the_room(monkeypatch, tmp_path, caplog):
    reading = Reading(
        building=BUILDING,
        room=ROOM,
        system_no=SYSTEM,
        kwh=parse_kwh("1.00度"),
    )
    monkeypatch.chdir(tmp_path)
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    service = ElectricityService(FakeClient(reading))
    payload = asyncio.run(service.query(BUILDING, ROOM))
    assert payload["status"] == "ok"
    assert list(tmp_path.iterdir()) == []
    assert not any(
        ROOM in record.getMessage() or BUILDING in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        name in service.__dict__
        for name in ("store", "directory", "cache", "history", "default_room")
    )


def test_electricity_source_has_no_persistence_or_logging_tokens():
    for module in (electricity, __import__("upc_mcp.electricity_service", fromlist=["x"])):
        source = getsource(module)
        for token in (
            "write_text",
            "write_bytes",
            "pickle",
            "sqlite",
            "logging.",
            "Path(",
            "json.dump",
        ):
            assert token not in source, (module.__name__, token)


def test_transport_safety_constants_are_fixed_and_bounded():
    assert electricity.SCHEME == "http"
    assert electricity.HOST == "dfcz.upc.edu.cn"
    assert electricity.PAGE_PATH == "/ShiYou/Default.aspx"
    assert electricity.GET_URL.endswith("/ShiYou/Default.aspx?code=0")
    assert electricity.MAX_RESPONSE_BYTES == 1024 * 1024
    assert electricity.CONNECT_TIMEOUT_SECONDS == 10.0
    assert electricity.READ_TIMEOUT_SECONDS == 20.0
    source = getsource(electricity)
    assert "trust_env=False" in source
    assert "follow_redirects=False" in source
    assert "max_keepalive_connections=0" in source
    assert "verify=False" not in source


def test_only_tangdaowan_and_no_other_campus_contract():
    assert electricity.CAMPUS == "唐岛湾"
    assert "gzkdf" not in getsource(electricity).lower()
    assert "guzhenkou" not in getsource(electricity).lower()
