from __future__ import annotations

import asyncio
import copy
import gzip
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from upc_mcp.timetable_protocol import (
    TimetableClient,
    TimetableCookie,
    TimetableError,
    parse_index,
    parse_week,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def term():
    return parse_index(fixture("timetable_index.json")).find("2026-2027", "1")


def cookies() -> list[TimetableCookie]:
    return [
        TimetableCookie(
            name="eai-sess",
            value=SecretStr("synthetic-session"),
            domain="app.upc.edu.cn",
            path="/",
            secure=True,
            http_only=True,
            same_site="Lax",
        )
    ]


def test_index_terms_and_current_week():
    index = parse_index(fixture("timetable_index.json"))
    assert [item.term for item in index.terms] == ["1", "2"]
    assert index.current_week == 8
    assert index.current_weekday == 6
    assert index.current_weekdays[0].isoformat() == "2026-10-25"
    assert index.find("2026-2027", "1").total_weeks == 19


def test_week_sunday_first_and_weekday_from_date():
    grid = parse_week(fixture("timetable_week_08.json"), term(), 8)
    assert [day.isoformat() for day in grid.dates] == [
        "2026-10-25",
        "2026-10-26",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
        "2026-10-31",
    ]
    sunday = next(item for item in grid.meetings if item.course_name == "示例课程日")
    assert sunday.weekday == 7
    assert sunday.start_period == 10


def test_noncontiguous_periods_are_split_with_source():
    grid = parse_week(fixture("timetable_week_08.json"), term(), 8)
    split = [item for item in grid.meetings if item.course_name == "示例课程C"]
    assert len(split) == 2
    assert [item.periods for item in split] == [(1,), (3,)]
    assert {item.sections_source for item in split} == {"lessArr_split_noncontiguous"}
    assert all(item.source_periods == (1, 3) for item in split)


def test_same_name_with_different_identity_is_not_merged():
    grid = parse_week(fixture("timetable_week_08.json"), term(), 8)
    same_name = [item for item in grid.meetings if item.course_name == "示例课程A"]
    assert len(same_name) == 2
    assert {item.course_id for item in same_name} == {"SYN-A", "SYN-A2"}
    assert {item.teacher for item in same_name} == {"教师甲", "教师丁"}


def test_empty_week_is_valid_empty_result():
    grid = parse_week(fixture("timetable_week_09_empty.json"), term(), 9)
    assert grid.empty is True
    assert grid.dates[0].isoformat() == "2026-11-01"


def test_requested_week_must_match_returned_dates():
    with pytest.raises(TimetableError) as caught:
        parse_week(fixture("timetable_week_08.json"), term(), 9)
    assert caught.value.code == "schema_changed"


def test_course_week_list_must_contain_requested_week():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"][1]["c_6"][0]["week"] = "9"
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_missing_course_name_fails():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    del payload["d"]["classes"][1]["c_6"][0]["course_name"]
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_missing_week_field_fails():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    del payload["d"]["classes"][1]["c_6"][0]["week"]
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_missing_lessarr_fails_without_fallback():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    del payload["d"]["classes"][1]["c_6"][0]["lessArr"]
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_duplicate_periods_fail():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"][1]["c_6"][0]["lessArr"] = [6, 6]
    payload["d"]["classes"][1]["c_6"][0]["totalLength"] = 2
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_total_length_conflict_fails():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"][1]["c_6"][0]["totalLength"] = 1
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_order_conflict_fails():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"][1]["c_6"][0]["order"] = 7
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_cell_key_conflict_fails():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    entry = payload["d"]["classes"][1].pop("c_6")[0]
    payload["d"]["classes"][1]["c_7"] = [entry]
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_weekday_conflict_fails():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"][1]["c_6"][0]["weekday"] = "2"
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_nonconsecutive_dates_fail():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["weekdays"][6] = "2026-11-02"
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_classes_must_have_seven_days():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"] = payload["d"]["classes"][:6]
    with pytest.raises(TimetableError) as caught:
        parse_week(payload, term(), 8)
    assert caught.value.code == "schema_changed"


def test_teacher_and_location_are_nullable_and_credit_is_unknown():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    entry = payload["d"]["classes"][1]["c_6"][0]
    del entry["teacher"]
    del entry["location"]
    grid = parse_week(payload, term(), 8)
    meeting = next(item for item in grid.meetings if item.course_name == "示例课程A")
    assert meeting.teacher is None
    assert meeting.location is None
    assert meeting.credit is None
    assert meeting.exam_type is None


def test_week_type_is_preserved_without_inference():
    payload = copy.deepcopy(fixture("timetable_week_08.json"))
    payload["d"]["classes"][1]["c_6"][0]["week_type"] = "7"
    grid = parse_week(payload, term(), 8)
    meeting = next(item for item in grid.meetings if item.course_name == "示例课程A")
    assert meeting.week_type == "7"


def test_index_10013_is_auth_required():
    with pytest.raises(TimetableError) as caught:
        parse_index({"e": 10013, "m": "expired", "d": {}})
    assert caught.value.code == "auth_required"


def test_index_403_like_business_error_is_not_auth_required():
    with pytest.raises(TimetableError) as caught:
        parse_index({"e": 403, "m": "forbidden", "d": {}})
    assert caught.value.code == "upstream_error"


def cookie_scope() -> TimetableCookie:
    return TimetableCookie(
        name="scoped",
        value=SecretStr("synthetic"),
        domain="app.upc.edu.cn",
        path="/timetable",
        secure=True,
        http_only=True,
        same_site="Lax",
    )


def test_cookie_scope_path_and_secure_boundaries():
    cookie = cookie_scope()
    assert cookie.matches(
        "app.upc.edu.cn", "/timetable/wap/default/get-index", secure=True
    )
    assert not cookie.matches("app.upc.edu.cn", "/timetablefoo", secure=True)
    assert not cookie.matches("app.upc.edu.cn", "/timetable/wap", secure=False)
    assert not cookie.matches("example.com", "/timetable/wap", secure=True)


def run(coro):
    return asyncio.run(coro)


def test_client_sends_fixed_request_shape_and_cookie():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": request.content.decode(),
            }
        )
        if request.method == "GET":
            return httpx.Response(200, json=fixture("timetable_index.json"))
        return httpx.Response(200, json=fixture("timetable_week_08.json"))

    client = TimetableClient(cookies(), transport=httpx.MockTransport(handler))
    index = run(client.get_index())
    week = run(client.get_week(index.find("2026-2027", "1"), 8))
    get_request = next(item for item in seen if item["method"] == "GET")
    post_request = next(item for item in seen if item["method"] == "POST")
    assert get_request["url"] == "https://app.upc.edu.cn/timetable/wap/default/get-index"
    assert post_request["url"] == (
        "https://app.upc.edu.cn/timetable/wap/default/get-datatmp"
    )
    assert get_request["headers"]["x-requested-with"] == "XMLHttpRequest"
    cookie_name = "eai" + "-sess"
    expected_cookie = cookie_name + "=" + "synthetic-session"
    assert get_request["headers"]["cookie"] == expected_cookie
    assert "year=2026-2027" in post_request["body"]
    assert "type=2" in post_request["body"]
    assert len(week.meetings) == 6


def test_client_10013_is_auth_required():
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"e": 10013, "m": "expired", "d": {}})
        ),
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "auth_required"


def test_client_302_is_auth_required():
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "https://cas.upc.edu.cn/"})
        ),
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "auth_required"


def test_client_307_is_upstream_error():
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(307, headers={"location": "https://example.invalid/"})
        ),
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "upstream_error"


def test_client_403_is_not_auth_required():
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text="forbidden")),
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "upstream_error"


def test_client_non_json_is_schema_changed():
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>login</html>")
        ),
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "schema_changed"


def test_client_streaming_size_limit():
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b'{"e":0,"d":{}}' + b" " * 100,
                headers={"content-type": "application/json"},
            )
        ),
        max_bytes=20,
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "response_too_large"


def test_client_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client = TimetableClient(cookies(), transport=httpx.MockTransport(handler))
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "timeout"


def test_client_size_limit_is_after_decompression():
    payload = json.dumps(fixture("timetable_index.json")).encode("utf-8") + b" " * 500
    compressed = gzip.compress(payload)
    client = TimetableClient(
        cookies(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=compressed,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
            )
        ),
        max_bytes=100,
    )
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "response_too_large"


def test_client_does_not_retry_and_does_not_echo_body_or_url():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="SECRET-UPSTREAM-BODY")

    client = TimetableClient(cookies(), transport=httpx.MockTransport(handler))
    with pytest.raises(TimetableError) as caught:
        run(client.get_index())
    assert caught.value.code == "upstream_error"
    assert calls == 1
    text = str(caught.value)
    assert "SECRET-UPSTREAM-BODY" not in text
    assert "app.upc.edu.cn" not in text
    assert "synthetic-session" not in text
