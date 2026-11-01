import asyncio
import json

import pytest

from upc_mcp import library_auth, library_seat
from upc_mcp.library_auth import (
    _is_library_page,
    _preferred_token,
    _token_from_url,
    local_endpoint,
)
from upc_mcp.library_seat import (
    LibraryError,
    LibrarySeatClient,
    minutes,
    reservation_active,
    reservation_matches,
    sign,
    signed_headers,
    signing_key,
)

UUID = "00000000-0000-4000-8000-000000000000"
TIMESTAMP = 1700000000000


def test_public_aes_config_and_fixed_hmac_vector():
    key = signing_key()
    assert key == b"leos3cr3t"
    assert (
        sign("GET", UUID, TIMESTAMP, key)
        == "d96340212c47c2f7c2c843bbf99e0a53f90d41fa8e0505c4ffc16c407f968897"
    )
    headers = signed_headers(
        "synthetic-seat-token",
        "GET",
        request_id=UUID,
        timestamp=TIMESTAMP,
        key=key,
    )
    assert headers["Authorization"] == "synthetic-seat-token"
    assert headers["loginType"] == "PC"
    assert headers["X-request-id"] == UUID
    assert headers["X-request-date"] == str(TIMESTAMP)
    assert headers["X-hmac-request-key"] == sign("GET", UUID, TIMESTAMP, key)
    assert "Bearer" not in headers["Authorization"]


def test_each_signed_header_gets_a_new_request_id():
    first = signed_headers("token", "GET")
    second = signed_headers("token", "GET")
    assert first["X-request-id"] != second["X-request-id"]
    assert first["X-request-id"] != second["X-request-id"]
    assert first["X-hmac-request-key"] != second["X-hmac-request-key"]


def test_filters_accept_lists_and_objects_but_reject_bad_dates():
    body = json.dumps(
        {
            "status": True,
            "data": {
                "dates": ["2026-11-01", "2026-11-02"],
                "buildings": [["b1", "图书馆"], {"id": "b2", "name": "东馆"}],
            },
        }
    )
    dates, buildings = library_seat.parse_filters(body)
    assert dates == ["2026-11-01", "2026-11-02"]
    assert [(item.id, item.name) for item in buildings] == [
        ("b1", "图书馆"),
        ("b2", "东馆"),
    ]
    with pytest.raises(LibraryError, match="日期"):
        library_seat.parse_filters(
            json.dumps(
                {
                    "status": True,
                    "data": {"dates": ["not-a-date"], "buildings": [["b1", "馆"]]},
                }
            )
        )


def test_empty_filters_are_empty_not_schema_error():
    dates, buildings = library_seat.parse_filters(
        json.dumps({"status": True, "data": {"dates": [], "buildings": []}})
    )
    assert dates == []
    assert buildings == []


def test_layout_uses_public_coordinates_and_kotlin_selectable_rule():
    body = json.dumps(
        {
            "status": True,
            "data": {
                "rows": 2,
                "cols": 2,
                "layout": {
                    "1001": {
                        "id": "s1",
                        "name": "A01",
                        "type": "seat",
                        "enabled": "1",
                        "status": "FREE",
                        "local": "0",
                    },
                    "1002": {
                        "id": "s2",
                        "name": "A02",
                        "type": "seat",
                        "enabled": "1",
                        "status": "YESSELECT",
                        "local": "1",
                    },
                    "2001": {
                        "id": "s3",
                        "name": "B01",
                        "type": "seat",
                        "enabled": "0",
                        "status": "FREE",
                    },
                    "2002": {
                        "id": "s4",
                        "name": "B02",
                        "type": "desk",
                        "enabled": "1",
                        "status": "FREE",
                    },
                },
            },
        }
    )
    layout = library_seat.parse_layout(body)
    assert (layout.rows, layout.cols) == (2, 2)
    seats = {seat.id: seat for seat in layout.seats}
    assert (seats["s1"].row, seats["s1"].col) == (1, 1)
    assert seats["s1"].selectable is True
    assert seats["s2"].selectable is False
    assert seats["s3"].selectable is False
    assert seats["s4"].selectable is False


def test_current_reservations_distinguish_null_from_missing_and_array():
    null_result = library_seat.parse_reservations(
        json.dumps({"status": True, "data": None})
    )
    assert null_result.items == ()
    assert null_result.empty_state == "data_null"
    array_result = library_seat.parse_reservations(
        json.dumps({"status": True, "data": []})
    )
    assert array_result.empty_state == "array_empty"
    with pytest.raises(LibraryError) as error:
        library_seat.parse_reservations(json.dumps({"status": True}))
    assert error.value.code == "schema_changed"
    with pytest.raises(LibraryError):
        library_seat.parse_reservations(json.dumps({"status": True, "data": {}}))


def test_current_reservations_parse_optional_stable_ids_without_requiring_them():
    body = json.dumps(
        {
            "status": True,
            "data": [
                {
                    "id": "r1",
                    "date": "2026-11-01",
                    "begin": "08:00",
                    "end": "10:00",
                    "location": "3F-A01",
                    "stat": "RESERVE",
                    "roomId": "room-1",
                    "seatId": "seat-1",
                },
                {
                    "id": "r2",
                    "date": "2026-11-01",
                    "begin": "10:00",
                    "end": "12:00",
                    "location": "3F-A02",
                    "stat": "RESERVE",
                },
            ],
        }
    )
    result = library_seat.parse_reservations(body)
    assert result.items[0].room_id == "room-1"
    assert result.items[0].seat_id == "seat-1"
    assert result.items[1].room_id is None
    assert result.items[1].seat_id is None
    assert result.items[0].public()["room_id"] == "room-1"
    assert result.items[1].public()["seat_id"] is None


def test_business_codes_and_statuses_are_not_flattened():
    with pytest.raises(LibraryError) as expired:
        library_seat.ensure_success({"code": 12})
    assert expired.value.code == "auth_expired"
    with pytest.raises(LibraryError) as signature:
        library_seat.ensure_success({"code": 20, "message": "school text"})
    assert signature.value.code == "upstream_error"
    assert signature.value.details["upstream_code"] == 20
    with pytest.raises(LibraryError) as rejected:
        library_seat.ensure_success({"status": False})
    assert rejected.value.code == "business_rejected"
    with pytest.raises(LibraryError) as unknown:
        library_seat.ensure_success({"code": 99})
    assert unknown.value.code == "upstream_unknown"
    library_seat.ensure_success({"status": True, "data": None})
    library_seat.ensure_success({"status": "true", "data": None})
    library_seat.ensure_success({"status": "success", "data": None})
    library_seat.ensure_success({"code": 200, "data": None})


def test_reservation_match_requires_room_and_full_seat_boundary():
    reservation = library_seat.Reservation(
        id="r1",
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        location="3F-A01",
        status="RESERVE",
    )
    assert reservation_matches(
        reservation,
        date="2026-11-01",
        begin="8:00",
        end="10:00:00",
        seat_name="A01",
        room_name="3F",
        room_name_unique=True,
        seat_name_unique=True,
    ) == (True, "location")
    assert reservation_matches(
        reservation,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        room_name="3F",
        room_name_unique=False,
        seat_name_unique=True,
    ) == (False, None)
    assert reservation_matches(
        reservation,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        room_name="4F",
        room_name_unique=True,
        seat_name_unique=True,
    ) == (False, None)
    room_only = library_seat.Reservation(
        id="r1",
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        location="3F-A01",
        status="RESERVE",
        room_id="r1",
    )
    assert reservation_matches(
        room_only,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        room_name="3F",
        room_id="r1",
        room_name_unique=True,
        seat_name_unique=True,
    ) == (True, "room_id_location")
    assert reservation_matches(
        room_only,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        room_name="3F",
        room_id="r2",
        room_name_unique=True,
        seat_name_unique=True,
    ) == (False, None)
    assert reservation_matches(
        reservation,
        date="2026-11-02",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        room_name="3F",
        room_name_unique=True,
        seat_name_unique=True,
    ) == (False, None)
    assert library_seat.overlaps(
        reservation, date="2026-11-01", begin="09:00", end="11:00"
    ) is True
    assert library_seat.overlaps(
        reservation, date="2026-11-01", begin="10:00", end="11:00"
    ) is False
    assert minutes("8:05") == 485
    assert minutes("bad") is None


def test_reservation_match_does_not_accept_a01_a010_or_adjacent_suffixes():
    def location_matches(location):
        return reservation_matches(
            library_seat.Reservation(
                id="r1",
                date="2026-11-01",
                begin="08:00",
                end="10:00",
                location=location,
                status="RESERVE",
            ),
            date="2026-11-01",
            begin="08:00",
            end="10:00",
            seat_name="A01",
            room_name="3F",
            room_name_unique=True,
            seat_name_unique=True,
        )[0]

    assert location_matches("3F-A01") is True
    assert location_matches("3F-A010") is False
    assert location_matches("3F-A01-1") is False
    assert location_matches("3F-A01_1") is False
    assert location_matches("3F-A01.1") is False
    assert location_matches("3F-A01B") is False
    assert location_matches("4F-A01") is False
    assert location_matches("A01-3F") is False


def test_reservation_match_prefers_stable_ids_over_text():
    reservation = library_seat.Reservation(
        id="r1",
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        location="different room",
        status="RESERVE",
        seat_id="s1",
        room_id="r1",
    )
    assert reservation_matches(
        reservation,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        seat_id="s1",
        room_id="r1",
    ) == (True, "seat_room_id")
    assert reservation_matches(
        reservation,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        seat_id="s1",
        room_id="r2",
    ) == (False, None)
    assert reservation_matches(
        reservation,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        seat_id="s2",
        room_id="r1",
    ) == (False, None)

    seat_only = library_seat.Reservation(
        id="r1",
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        location="3F-A01",
        status="RESERVE",
        seat_id="s1",
    )
    assert reservation_matches(
        seat_only,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        seat_id="s1",
        room_name="3F",
        room_name_unique=True,
        seat_name_unique=True,
    ) == (True, "seat_id_location")
    seat_only_ambiguous = library_seat.Reservation(
        id="r1",
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        location="different",
        status="RESERVE",
        seat_id="s1",
    )
    assert reservation_matches(
        seat_only_ambiguous,
        date="2026-11-01",
        begin="08:00",
        end="10:00",
        seat_name="A01",
        seat_id="s1",
        seat_name_unique=True,
    ) == (False, None)


def test_terminal_reservation_status_is_not_active():
    assert reservation_active("RESERVE") is True
    assert reservation_active("CHECK_IN") is True
    assert reservation_active("AWAY") is True
    assert reservation_active("CANCEL") is False
    assert reservation_active("cancel") is False
    assert reservation_active("STOP") is False
    assert reservation_active("COMPLETE") is False
    assert reservation_active("MISS") is False
    assert reservation_active("") is None
    assert reservation_active("SOMETHING_NEW") is None


def test_completed_page_seat_token_wins_over_url_app_token():
    href = "https://libspace.upc.edu.cn/libseat/#/login?token=old-app-token"
    assert _preferred_token(href, "seat-token", href) == ("seat_token", "seat-token")
    assert _preferred_token(href, "", href) == ("app_token", "old-app-token")
    assert _preferred_token(
        "https://cas.upc.edu.cn/cas/login?token=wrong",
        "seat-token",
        "https://libspace.upc.edu.cn/libseat/?token=fallback-app",
    ) == ("app_token", "fallback-app")


def test_capture_target_reads_storage_before_url_token(monkeypatch):
    async def fake_evaluate(ws_url):
        return {
            "href": "https://libspace.upc.edu.cn/libseat/?token=old-app-token#/login",
            "token": "seat-token",
        }

    monkeypatch.setattr(library_auth, "_evaluate", fake_evaluate)
    monkeypatch.setattr(
        library_auth,
        "_target_ws",
        lambda target: "ws://127.0.0.1:9222/devtools/page/synthetic",
    )
    target = {
        "type": "page",
        "url": "https://libspace.upc.edu.cn/libseat/?token=old-app-token#/login",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/synthetic",
    }
    assert asyncio.run(library_auth._capture_target(target)) == (
        "seat_token",
        "seat-token",
    )


def test_cdp_token_source_is_limited_to_libseat_pages():
    assert _is_library_page("https://libspace.upc.edu.cn/libseat/#/login")
    assert not _is_library_page("https://cas.upc.edu.cn/cas/login?token=x")
    assert not _is_library_page("http://libspace.upc.edu.cn/libseat/")
    token = _token_from_url(
        "https://libspace.upc.edu.cn/libseat/?token=synthetic#/login?token=fragment-token"
    )
    assert token == "synthetic"
    with pytest.raises(LibraryError):
        local_endpoint("http://127.0.0.1:9222/devtools")
    with pytest.raises(LibraryError):
        local_endpoint("http://example.com:9222")


def test_client_rejects_unsafe_path_ids_and_never_includes_token_in_errors():
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            return library_seat.httpx.Response(500)

        client = LibrarySeatClient(transport=library_seat.httpx.MockTransport(handler))
        try:
            await client.rooms("synthetic-seat-token", "../bad", "2026-11-01")
        except LibraryError as error:
            assert "synthetic-seat-token" not in json.dumps(error.payload())
            assert error.code == "invalid_input"
        assert calls == []

    import asyncio

    asyncio.run(scenario())


def test_client_sends_token_in_query_and_authorization_but_not_in_error():
    import asyncio

    async def scenario():
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers.get("authorization")
            return library_seat.httpx.Response(
                200, json={"status": True, "data": {"id": "1", "username": "u"}}
            )

        client = LibrarySeatClient(transport=library_seat.httpx.MockTransport(handler))
        user = await client.user("synthetic-seat-token")
        assert user.id == "1"
        assert "token=synthetic-seat-token" in seen["url"]
        assert seen["authorization"] == "synthetic-seat-token"
        assert "synthetic-seat-token" not in json.dumps(user.public())

    asyncio.run(scenario())
