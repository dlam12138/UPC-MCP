import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from upc_mcp.library_seat import LibraryError, LibrarySeatClient, LibraryUser, identity_hash
from upc_mcp.library_service import LibraryService
from upc_mcp.library_session import LibraryBookingLock, LibrarySession, LibraryStore

USER = LibraryUser(id="synthetic-user-id", username="synthetic-username", name="Synthetic")
TOKEN = "synthetic-seat-token"
DATE = "2026-11-01"


def make_store(tmp_path):
    return LibraryStore(tmp_path)


def put_session(store: LibraryStore, user: LibraryUser = USER, token: str = TOKEN):
    digest = identity_hash(user)
    session = LibrarySession(
        token=token,
        user_id=user.id,
        username=user.username,
        name=user.name,
        identity_hash=digest,
        identity_stable=True,
        binding_id=digest,
        state="stored",
    )
    store.replace(session)
    return session


def ok(data):
    return httpx.Response(200, json={"status": True, "data": data})


def empty_reservations():
    return {"status": True, "data": None}


class FakeApi:
    def __init__(self):
        self.requests = []
        self.reservations = []
        self.book_calls = 0
        self.book_entered = None
        self.book_block = None
        self.book_exception = None
        self.book_response = None
        self.book_append = True
        self.captcha = False
        self.seat_status = "FREE"
        self.seat_enabled = "1"
        self.seat_local = "0"
        self.seat_name = "A01"
        self.room_names = {"r1": "3F", "r2": "4F", "r3": "5F", "r4": "6F", "r5": "7F"}
        self.book_status = "RESERVE"
        self.book_extra = {}
        self.book_location = "3F-A01"
        self.reservations_response = None
        self.filters_response = None
        self.user = USER
        self.user_exception = None

    async def __call__(self, request: httpx.Request):
        self.requests.append(request)
        path = request.url.path
        if path == "/rest/v2/user":
            if self.user_exception:
                raise self.user_exception
            return ok(
                {"id": self.user.id, "username": self.user.username, "name": self.user.name}
            )
        if path == "/rest/ssoAuth":
            return ok({"token": "exchanged-seat-token"})
        if path == "/rest/v2/free/filters":
            if self.filters_response is not None:
                return self.filters_response
            return ok(
                {
                    "dates": [DATE, "2026-11-02"],
                    "buildings": [["b1", "图书馆"], {"id": "b2", "name": "东馆"}],
                }
            )
        if path.startswith("/rest/v2/room/floors/"):
            return ok([{"floorNum": 3, "floorName": "三层"}])
        if path.startswith("/rest/v2/room/stats2/"):
            if "/b2/" in path:
                room_ids = ["r4", "r5"]
            else:
                room_ids = ["r1", "r2", "r3"]
            return ok(
                [
                    {"roomId": room_id, "room": self.room_names[room_id], "floor": 3}
                    for room_id in room_ids
                ]
            )
        if path.startswith("/rest/v2/room/layoutByDate/"):
            return ok(
                {
                    "rows": 2,
                    "cols": 2,
                    "layout": {
                        "1001": {
                            "id": "s1",
                            "name": self.seat_name,
                            "type": "seat",
                            "enabled": self.seat_enabled,
                            "status": self.seat_status,
                            "local": self.seat_local,
                        },
                        "1002": {
                            "id": "s2",
                            "name": "A02",
                            "type": "seat",
                            "enabled": "1",
                            "status": "FREE",
                            "local": "0",
                        },
                        "2001": {
                            "id": "s3",
                            "name": "B01",
                            "type": "seat",
                            "enabled": "1",
                            "status": "BOOKED",
                            "local": "0",
                        },
                    },
                }
            )
        if path.startswith("/rest/v2/startTimesForSeat/"):
            return ok(
                {
                    "startTimes": [
                        {"id": "t1", "value": "08:00"},
                        {"id": "t2", "value": "09:00"},
                        {"id": "now", "value": "08:00"},
                    ]
                }
            )
        if path.startswith("/rest/v2/endTimesForSeat/"):
            return ok(
                {
                    "endTimes": [
                        {"id": "e1", "value": "10:00"},
                        {"id": "e2", "value": "11:00"},
                    ]
                }
            )
        if path == "/rest/v2/user/reservations":
            if self.reservations_response is not None:
                return self.reservations_response
            return ok(self.reservations or None)
        if path == "/rest/v2/settings":
            return ok({"isCaptchaOpen": "1" if self.captcha else "0"})
        if path == "/rest/v2/freeBook":
            self.book_calls += 1
            if self.book_entered is not None:
                self.book_entered.set()
            if self.book_block is not None:
                await self.book_block.wait()
            if self.book_exception is not None:
                raise self.book_exception
            if self.book_response is not None:
                return self.book_response
            form = {
                key: values[0]
                for key, values in parse_qs(request.content.decode()).items()
            }
            if self.book_append:
                self.reservations.append(
                    {
                        "id": "res-new",
                        "date": form["date"],
                        "begin": "08:00",
                        "end": "10:00",
                        "location": self.book_location,
                        "stat": self.book_status,
                        **self.book_extra,
                    }
                )
            return ok(None)
        return httpx.Response(404, text="synthetic unknown path")


def service_for(tmp_path, api: FakeApi):
    store = make_store(tmp_path)
    put_session(store)
    return LibraryService(store, LibrarySeatClient(transport=httpx.MockTransport(api)))


def test_query_surface_uses_real_ids_and_pagination(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        filters = await service.filters()
        assert filters["status"] == "ok"
        assert filters["dates"] == [DATE, "2026-11-02"]
        assert filters["captcha_required"] is False

        rooms = await service.rooms("b1", DATE, limit=2)
        assert rooms["status"] == "ok"
        assert rooms["total"] == 3
        assert rooms["returned"] == 2
        assert rooms["truncated"] is True
        assert rooms["rooms"][0]["floor_name"] == "三层"

        seats = await service.seats("r1", DATE, only_available=True)
        assert seats["available_total"] == 2
        assert {item["seat_id"] for item in seats["seats"]} == {"s1", "s2"}

        options = await service.times("s1", DATE)
        assert [item["id"] for item in options["starts"]] == ["t1", "t2", "now"]
        ends = await service.times("s1", DATE, start_id="t1")
        assert ends["start"]["value"] == "08:00"
        assert [item["id"] for item in ends["ends"]] == ["e1", "e2"]

        reservations = await service.reservations()
        assert reservations["status"] == "empty"
        assert reservations["empty_state"] == "data_null"

        assert all(
            request.headers["authorization"] == TOKEN for request in api.requests
        )
        assert all(
            request.url.params.get("token") == TOKEN for request in api.requests
        )

    asyncio.run(scenario())


def test_missing_current_reservation_data_is_schema_error(tmp_path):
    api = FakeApi()
    api.reservations_response = httpx.Response(200, json={"status": True})
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.reservations()
        assert error.value.code == "schema_changed"

    asyncio.run(scenario())


def test_auth_expiry_marks_session_and_does_not_retry(tmp_path):
    api = FakeApi()
    api.reservations_response = httpx.Response(200, json={"code": 12})
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as first:
            await service.reservations()
        assert first.value.code == "auth_expired"
        with pytest.raises(LibraryError) as second:
            await service.reservations()
        assert second.value.code == "auth_required"

    asyncio.run(scenario())
    assert service.store.load().state == "auth_expired"
    assert len(api.requests) == 1


def test_book_success_is_confirmed_by_new_reservation(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "booked"
        assert result["submitted"] is True
        assert result["verified"] is True
        assert result["reservation"]["reservation_id"] == "res-new"
        assert api.book_calls == 1
        assert not any(
            "cancel" in request.url.path for request in api.requests
        )
        receipts = json.loads(service.receipts_path.read_text(encoding="utf-8"))
        assert list(receipts["records"].values())[0]["status"] == "success"

    asyncio.run(scenario())


def test_identical_existing_reservation_is_not_resubmitted(tmp_path):
    api = FakeApi()
    api.reservations = [
        {
            "id": "res-existing",
            "date": DATE,
            "begin": "08:00",
            "end": "10:00",
            "location": "3F-A01",
            "stat": "RESERVE",
        }
    ]
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "already_booked"
        assert result["submitted"] is False
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_overlapping_existing_reservation_blocks_write(tmp_path):
    api = FakeApi()
    api.reservations = [
        {
            "id": "res-existing",
            "date": DATE,
            "begin": "09:00",
            "end": "11:00",
            "location": "A02",
            "stat": "RESERVE",
        }
    ]
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "reservation_conflict"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_captcha_blocks_before_any_post(tmp_path):
    api = FakeApi()
    api.captcha = True
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "requires_user_action"
        assert error.value.details["action"] == "complete_captcha_in_official_page"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_seat_without_stable_name_blocks_before_any_post(tmp_path):
    api = FakeApi()
    api.seat_name = ""
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "seat_not_available"
        assert error.value.details["reason"] == "seat_name_unavailable"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_now_start_is_rejected_before_any_post(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="now",
                end_id="e1",
            )
        assert error.value.code == "unsupported"
        assert error.value.details["action"] == "choose_explicit_start"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_same_room_name_in_two_rooms_keeps_pending(tmp_path):
    api = FakeApi()
    api.room_names["r2"] = "3F"
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "verification_mismatch"
        assert api.book_calls == 1

    asyncio.run(scenario())


def test_terminal_identical_reservation_is_not_already_booked(tmp_path):
    api = FakeApi()
    api.reservations = [
        {
            "id": "res-cancelled",
            "date": DATE,
            "begin": "08:00",
            "end": "10:00",
            "location": "3F-A01",
            "stat": "CANCEL",
        }
    ]
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "booked"
        assert result["submitted"] is True
        assert api.book_calls == 1

    asyncio.run(scenario())


def test_new_terminal_record_does_not_count_as_booked(tmp_path):
    api = FakeApi()
    api.book_status = "CANCEL"
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "verification_inactive"
        assert result["submitted"] is None

    asyncio.run(scenario())


def test_exact_room_and_seat_ids_confirm_ambiguous_location(tmp_path):
    api = FakeApi()
    api.book_location = "A010"
    api.book_extra = {"roomId": "r1", "seatId": "s1"}
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "booked"
        assert result["reservation"]["room_id"] == "r1"
        assert result["reservation"]["seat_id"] == "s1"

    asyncio.run(scenario())


def test_existing_reservation_with_exact_ids_is_already_booked(tmp_path):
    api = FakeApi()
    api.reservations = [
        {
            "id": "res-existing",
            "date": DATE,
            "begin": "08:00",
            "end": "10:00",
            "location": "A010",
            "stat": "RESERVE",
            "roomId": "r1",
            "seatId": "s1",
        }
    ]
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "already_booked"
        assert result["submitted"] is False
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_same_date_time_other_room_reservation_blocks(tmp_path):
    api = FakeApi()
    api.reservations = [
        {
            "id": "res-other-room",
            "date": DATE,
            "begin": "08:00",
            "end": "10:00",
            "location": "4F-A02",
            "stat": "RESERVE",
        }
    ]
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "reservation_conflict"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_a01_a010_and_adjacent_suffix_are_not_already_booked(tmp_path):
    for location in ("3F-A010", "3F-A01-1"):
        api = FakeApi()
        api.reservations = [
            {
                "id": "res-other",
                "date": DATE,
                "begin": "08:00",
                "end": "10:00",
                "location": location,
                "stat": "RESERVE",
            }
        ]
        service = service_for(tmp_path / location.replace("-", "_"), api)

        async def scenario(service=service, api=api):
            with pytest.raises(LibraryError) as error:
                await service.book(
                    room_id="r1",
                    seat_id="s1",
                    day=DATE,
                    start_id="t1",
                    end_id="e1",
                )
            assert error.value.code == "reservation_conflict"
            assert api.book_calls == 0

        asyncio.run(scenario())


def test_unselectable_seat_blocks_before_any_post(tmp_path):
    api = FakeApi()
    api.seat_local = "1"
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "seat_not_available"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_http_500_is_pending_and_second_call_does_not_resend(tmp_path):
    api = FakeApi()
    api.book_response = httpx.Response(500)
    service = service_for(tmp_path, api)

    async def scenario():
        first = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert first["status"] == "pending_confirmation"
        assert first["submitted"] is None
        assert first["retry"] is False
        assert first["response_success"] is False
        assert api.book_calls == 1
        second = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert second["status"] == "pending_confirmation"
        assert second["reason"] == "previous_unknown"
        assert api.book_calls == 1

    asyncio.run(scenario())
    receipts = json.loads(service.receipts_path.read_text(encoding="utf-8"))
    assert list(receipts["records"].values())[0]["status"] == "unknown"


def test_timeout_is_pending_and_not_retried(tmp_path):
    api = FakeApi()
    api.book_exception = httpx.ReadTimeout("synthetic timeout")
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "timeout"
        assert result["retry"] is False
        assert api.book_calls == 1

    asyncio.run(scenario())


def test_success_response_without_matching_reservation_is_pending(tmp_path):
    api = FakeApi()
    api.book_append = False
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "verification_mismatch"
        assert result["response_success"] is True
        assert result["submitted"] is None

    asyncio.run(scenario())


def test_non_json_and_unknown_business_code_are_pending(tmp_path):
    api = FakeApi()
    api.book_response = httpx.Response(200, text="not-json")
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "invalid_response"

    asyncio.run(scenario())

    api2 = FakeApi()
    api2.book_response = httpx.Response(200, json={"code": 99})
    service2 = service_for(tmp_path / "second", api2)

    async def scenario2():
        result = await service2.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "pending_confirmation"
        assert result["reason"] == "unknown_business_code"

    asyncio.run(scenario2())


def test_business_rejection_clears_receipt(tmp_path):
    api = FakeApi()
    api.book_response = httpx.Response(200, json={"status": False})
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "business_rejected"
        assert api.book_calls == 1

    asyncio.run(scenario())
    receipts = json.loads(service.receipts_path.read_text(encoding="utf-8"))
    assert receipts["records"] == {}


def test_corrupt_receipt_refuses_write_but_queries_still_work(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)
    service.receipts_path.write_bytes(b"not-json")
    original = service.receipts_path.read_bytes()

    async def scenario():
        with pytest.raises(LibraryError) as error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert error.value.code == "local_error"
        assert error.value.details["submitted"] is None
        assert api.book_calls == 0
        reservations = await service.reservations()
        assert reservations["status"] == "empty"

    asyncio.run(scenario())
    assert service.receipts_path.read_bytes() == original


def test_receipt_is_written_before_the_post(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        api.book_entered = asyncio.Event()
        api.book_block = asyncio.Event()
        task = asyncio.create_task(
            service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        )
        await asyncio.wait_for(api.book_entered.wait(), 2)
        receipts = json.loads(service.receipts_path.read_text(encoding="utf-8"))
        assert list(receipts["records"].values())[0]["status"] == "in_flight"
        api.book_block.set()
        result = await task
        assert result["status"] == "booked"

    asyncio.run(scenario())


def test_cancelled_booking_keeps_lock_until_post_finishes(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        api.book_entered = asyncio.Event()
        api.book_block = asyncio.Event()
        first = asyncio.create_task(
            service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        )
        await asyncio.wait_for(api.book_entered.wait(), 2)
        second = asyncio.create_task(
            service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        )
        await asyncio.sleep(0.1)
        assert not second.done()
        first.cancel()
        await asyncio.sleep(0.1)
        assert not second.done()
        assert api.book_calls == 1
        api.book_block.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await second
        assert result["status"] in {"booked", "already_booked"}
        assert api.book_calls == 1

    asyncio.run(scenario())


def test_cancelled_while_waiting_for_file_lock_releases_late_acquire(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        holder = await asyncio.to_thread(LibraryBookingLock(tmp_path).acquire)
        first = asyncio.create_task(
            service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        )
        await asyncio.sleep(0.1)
        first.cancel()
        await asyncio.to_thread(holder.release)
        with pytest.raises(asyncio.CancelledError):
            await first
        # Cancellation during acquire must not start the write path.
        assert api.book_calls == 0
        assert not service.receipts_path.exists()
        second = await asyncio.wait_for(
            service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            ),
            5,
        )
        assert second["status"] == "booked"
        assert api.book_calls == 1

    asyncio.run(scenario())


def test_clear_during_read_discards_result(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        original = api.__call__

        async def blocking(request):
            if request.url.path == "/rest/v2/free/filters":
                started.set()
                await release.wait()
            return await original(request)

        service.client = LibrarySeatClient(transport=httpx.MockTransport(blocking))
        task = asyncio.create_task(service.filters())
        await asyncio.wait_for(started.wait(), 2)
        service.store.clear()
        release.set()
        with pytest.raises(LibraryError) as error:
            await task
        assert error.value.code == "auth_required"
        assert service.store.load() is None

    asyncio.run(scenario())


def test_clear_during_adoption_rejects_late_token(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        original = api.__call__

        async def blocking(request):
            if request.url.path == "/rest/v2/user":
                started.set()
                await release.wait()
            return await original(request)

        service.client = LibrarySeatClient(transport=httpx.MockTransport(blocking))
        task = asyncio.create_task(service.adopt_seat_token("new-token"))
        await asyncio.wait_for(started.wait(), 2)
        service.store.clear()
        release.set()
        with pytest.raises(LibraryError) as error:
            await task
        assert error.value.code == "auth_required"
        assert service.store.load() is None

    asyncio.run(scenario())


def test_rebind_requires_explicit_flag_and_stable_identity(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)
    other = LibraryUser(id="other-id", username="other-user", name="Other")

    async def scenario():
        # Same identity refreshes the token without rebind.
        refreshed = await service.adopt_seat_token("refreshed-token")
        assert refreshed["status"] == "ok"
        assert service.store.load().token.get_secret_value() == "refreshed-token"

        api.user = other
        with pytest.raises(LibraryError) as blocked:
            await service.adopt_seat_token("other-token")
        assert blocked.value.code == "already_bound"
        assert service.store.load().token.get_secret_value() == "refreshed-token"

        rebound = await service.adopt_seat_token("other-token", rebind=True)
        assert rebound["status"] == "ok"
        session = service.store.load()
        assert session.token.get_secret_value() == "other-token"
        assert session.identity_hash == identity_hash(other)

    asyncio.run(scenario())


def test_exchange_app_token_uses_post_and_verifies_identity(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.adopt_app_token("synthetic-app-token")
        assert result["status"] == "ok"
        assert service.store.load().token.get_secret_value() == "exchanged-seat-token"
        exchange = next(
            request for request in api.requests if request.url.path == "/rest/ssoAuth"
        )
        assert exchange.method == "POST"
        assert exchange.headers["authorization"] == "synthetic-app-token"
        assert exchange.url.params.get("token") == "synthetic-app-token"
        assert parse_qs(exchange.content.decode())["token"] == ["synthetic-app-token"]

    asyncio.run(scenario())


def test_identity_mismatch_blocks_query_and_write(tmp_path):
    api = FakeApi()
    api.user = LibraryUser(id="other-id", username="other-user", name="Other")
    service = service_for(tmp_path, api)

    async def scenario():
        with pytest.raises(LibraryError) as check_error:
            await service.check()
        assert check_error.value.code == "auth_required"
        with pytest.raises(LibraryError) as book_error:
            await service.book(
                room_id="r1",
                seat_id="s1",
                day=DATE,
                start_id="t1",
                end_id="e1",
            )
        assert book_error.value.code == "auth_required"
        assert api.book_calls == 0

    asyncio.run(scenario())


def test_unknown_post_result_can_be_confirmed_by_read_only_verification(tmp_path):
    api = FakeApi()
    service = service_for(tmp_path, api)

    async def handler(request):
        if request.url.path == "/rest/v2/freeBook":
            api.book_calls += 1
            api.reservations.append(
                {
                    "id": "res-after-timeout",
                    "date": DATE,
                    "begin": "08:00",
                    "end": "10:00",
                    "location": "3F-A01",
                    "stat": "RESERVE",
                }
            )
            raise httpx.ReadTimeout("synthetic timeout")
        return await api(request)

    service.client = LibrarySeatClient(transport=httpx.MockTransport(handler))

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        assert result["status"] == "booked"
        assert result["submitted"] is True
        assert result["verified"] is True
        assert result["reservation"]["reservation_id"] == "res-after-timeout"
        assert api.book_calls == 1

    asyncio.run(scenario())


def test_dpapi_hides_token_and_identity(tmp_path):
    store = make_store(tmp_path)
    put_session(store)
    encrypted = store.path.read_bytes()
    assert TOKEN.encode() not in encrypted
    assert USER.id.encode() not in encrypted
    assert USER.username.encode() not in encrypted
    loaded = store.load()
    assert loaded is not None
    assert loaded.token.get_secret_value() == TOKEN
    assert loaded.public()["identity_verified"] is True
    assert TOKEN not in json.dumps(loaded.public())


def test_error_payload_never_contains_token(tmp_path):
    api = FakeApi()
    api.book_response = httpx.Response(500)
    service = service_for(tmp_path, api)

    async def scenario():
        result = await service.book(
            room_id="r1",
            seat_id="s1",
            day=DATE,
            start_id="t1",
            end_id="e1",
        )
        serialized = json.dumps(result)
        assert TOKEN not in serialized
        assert USER.username not in serialized

    asyncio.run(scenario())
