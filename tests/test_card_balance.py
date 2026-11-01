import asyncio
import json

import httpx
import pytest

from upc_mcp.card_balance import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    CardBalanceClient,
    CardBalanceError,
    build_form,
    format_amount,
    parse_card_balance,
    valid_student_id,
)

STUDENT = "0" * 10
OTHER = "1" * 10


def body(**changes):
    card = {
        "sno": STUDENT,
        "db_balance": "1234",
        "unsettle_amount": "-1",
    }
    card.update(changes)
    return {"retcode": "0", "card": [card]}


def encode(payload):
    return json.dumps(payload, ensure_ascii=False)


def test_student_id_limit_and_exact_form():
    assert valid_student_id("0" * 8)
    assert valid_student_id("0" * 16)
    assert not valid_student_id("0" * 7)
    assert not valid_student_id("0" * 17)
    assert not valid_student_id("０" * 8)
    assert not valid_student_id(12345678)
    assert build_form(STUDENT) == (
        f"sno={STUDENT}&xxbh=synjones&idtype=sno&id={STUDENT}"
    )
    with pytest.raises(CardBalanceError) as error:
        build_form("not-a-student")
    assert error.value.code == "invalid_input"


def test_exact_cents_and_decimal_serialization():
    result = parse_card_balance(
        encode(body(db_balance="0", unsettle_amount="120")),
        STUDENT,
    )
    assert format_amount(result.balance) == "0.00"
    assert format_amount(result.unsettle_amount) == "1.20"
    public = result.public()
    assert public["balance"] == "0.00"
    assert public["unsettle_amount"] == "1.20"
    assert public["upstream_amount_unit"] == "CNY_cent"
    assert public["card_count"] == 1
    assert "sno" not in public


def test_negative_and_large_cents_are_exact():
    result = parse_card_balance(
        encode(
            body(
                db_balance=str(2**63 - 1),
                unsettle_amount=str(-(2**63)),
            )
        ),
        STUDENT,
    )
    assert format_amount(result.balance) == "92233720368547758.07"
    assert format_amount(result.unsettle_amount) == "-92233720368547758.08"

    result = parse_card_balance(
        encode(body(db_balance="-1", unsettle_amount="-0")),
        STUDENT,
    )
    assert format_amount(result.balance) == "-0.01"
    assert format_amount(result.unsettle_amount) == "0.00"


def test_retcode_zero_accepts_string_or_integer():
    for value in ("0", 0):
        payload = body()
        payload["retcode"] = value
        assert parse_card_balance(encode(payload), STUDENT).public()["balance"] == "12.34"


def test_business_failure_is_not_zero():
    payload = body(db_balance="0")
    payload["retcode"] = "60006"
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode(payload), STUDENT)
    assert error.value.code == "upstream_error"
    assert "0.00" not in str(error.value)


def test_empty_card_is_not_zero():
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode({"retcode": "0", "card": []}), STUDENT)
    assert error.value.code == "card_empty"


def test_multiple_cards_are_not_guessed():
    payload = body()
    payload["card"].append({"sno": STUDENT, "db_balance": "1", "unsettle_amount": "0"})
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode(payload), STUDENT)
    assert error.value.code == "card_multiple"


def test_identity_mismatch_is_not_a_balance():
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode(body(sno=OTHER)), STUDENT)
    assert error.value.code == "identity_mismatch"
    assert STUDENT not in str(error.value)
    assert STUDENT not in json.dumps(error.value.payload(), ensure_ascii=False)


@pytest.mark.parametrize(
    "payload",
    [
        {"retcode": "0"},
        {"retcode": "0", "card": {}},
        {"retcode": "0", "card": [{"db_balance": "1", "unsettle_amount": "0"}]},
        {"retcode": "0", "card": [{"sno": STUDENT, "unsettle_amount": "0"}]},
        {"retcode": "0", "card": [{"sno": STUDENT, "db_balance": "1"}]},
    ],
)
def test_missing_required_fields_are_schema_errors(payload):
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode(payload), STUDENT)
    assert error.value.code == "schema_changed"


@pytest.mark.parametrize(
    "amount",
    ["1.25", 1.25, 1.0, "+1", " 1", "", None, True, [], {}, "-", "1e2"],
)
def test_non_integer_cents_are_not_rounded_or_zero(amount):
    key = "db_balance"
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode(body(**{key: amount})), STUDENT)
    assert error.value.code == "schema_changed"


def test_amount_outside_signed_64_bit_is_rejected():
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(encode(body(db_balance=str(2**63))), STUDENT)
    assert error.value.code == "schema_changed"


@pytest.mark.parametrize(
    "text",
    ["", "not-json", "[]", "null", '{"retcode": NaN}', '{"retcode": "0", "card": null}'],
)
def test_malformed_json_is_schema_error(text):
    with pytest.raises(CardBalanceError) as error:
        parse_card_balance(text, STUDENT)
    assert error.value.code in {"schema_changed"}


def test_client_uses_fixed_endpoint_form_and_no_cookie_or_json_accept():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["content"] = request.content.decode("ascii")
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            text=encode(body()),
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )

    result = asyncio.run(
        CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT)
    )
    assert seen["method"] == "POST"
    assert seen["url"] == ENDPOINT
    assert seen["content"] == f"sno={STUDENT}&xxbh=synjones&idtype=sno&id={STUDENT}"
    assert seen["headers"]["accept"] == "*/*"
    assert "application/json" not in seen["headers"]["accept"]
    assert "cookie" not in seen["headers"]
    assert seen["headers"]["content-type"] == (
        "application/x-www-form-urlencoded; charset=UTF-8"
    )
    assert seen["headers"]["x-requested-with"] == "XMLHttpRequest"
    assert result.public()["balance"] == "12.34"


def test_utf8_bom_response_is_accepted():
    def handler(request):
        return httpx.Response(200, content=("\ufeff" + encode(body())).encode("utf-8"))

    result = asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT))
    assert result.public()["balance"] == "12.34"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "access_denied"),
        (403, "access_denied"),
        (429, "rate_limited"),
        (500, "upstream_error"),
        (302, "upstream_error"),
    ],
)
def test_http_classification_does_not_follow_redirects(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid/"})

    with pytest.raises(CardBalanceError) as error:
        asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT))
    assert error.value.code == code
    assert len(calls) == 1
    assert str(calls[0].url) == ENDPOINT


def test_network_timeout_is_structured_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("synthetic timeout", request=request)

    with pytest.raises(CardBalanceError) as error:
        asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT))
    assert error.value.code == "network_error"
    assert len(calls) == 1


def test_declared_oversize_is_rejected_before_body_read():
    def handler(request):
        return httpx.Response(
            200,
            content=b"x",
            headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
        )

    with pytest.raises(CardBalanceError) as error:
        asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT))
    assert error.value.code == "response_too_large"


def test_streamed_oversize_is_rejected():
    def handler(request):
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    with pytest.raises(CardBalanceError) as error:
        asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT))
    assert error.value.code == "response_too_large"


def test_invalid_utf8_body_is_network_error():
    def handler(request):
        return httpx.Response(200, content=b"\xff\xfe\xff")

    with pytest.raises(CardBalanceError) as error:
        asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch(STUDENT))
    assert error.value.code == "network_error"


def test_invalid_student_id_sends_no_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=encode(body()))

    with pytest.raises(CardBalanceError) as error:
        asyncio.run(CardBalanceClient(httpx.MockTransport(handler)).fetch("bad"))
    assert error.value.code == "invalid_input"
    assert calls == []
