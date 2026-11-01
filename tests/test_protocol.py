import asyncio
import json

import httpx
import pytest
from conftest import ACCOUNT

from upc_mcp.errors import UPCError
from upc_mcp.jhzf import BILL_ENDPOINT, JhzfClient, month_form, parse_bill


def test_integer_cents_and_identical_transactions_are_preserved(bill):
    result = parse_bill(json.dumps(bill), ACCOUNT, 1)
    assert len(result["transactions"]) == 2
    assert result["transactions"][0]["amount_cents"] == -125
    assert result["upstream_timezone"] is None


@pytest.mark.parametrize(
    "change", ["wrong_account", "decimal_money", "invalid_time", "looping_page", "missing_total"]
)
def test_contract_failure_never_becomes_empty_or_zero(bill, change):
    if change == "wrong_account":
        bill["total"][0]["account"] = "other"
    elif change == "decimal_money":
        bill["total"][0]["sign_tranamt"] = "1.25"
    elif change == "invalid_time":
        bill["total"][0]["occtime"] = "20260230120000"
    elif change == "looping_page":
        bill["nextpage"] = "1"
    else:
        del bill["total"]
    with pytest.raises(UPCError) as error:
        parse_bill(json.dumps(bill), ACCOUNT, 1)
    assert error.value.code == "schema_changed"


def test_business_failure_not_successful_empty(bill):
    bill["retcode"] = "60006"
    with pytest.raises(UPCError) as error:
        parse_bill(json.dumps(bill), ACCOUNT, 1)
    assert error.value.code == "upstream_error"


def test_month_boundary_uses_calendar_not_fixed_day_count(session):
    _, form = month_form(session, "2024-02", 1)
    assert form["query_end"] == "20240229"
    with pytest.raises(UPCError):
        month_form(session, "2024-13", 1)


@pytest.mark.parametrize(
    "status,code", [(500, "upstream_error"), (403, "auth_expired"), (302, "upstream_error")]
)
def test_http_classification_and_no_redirect_retry(session, status, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid/"})

    with pytest.raises(UPCError) as error:
        asyncio.run(JhzfClient(httpx.MockTransport(respond)).bill(session, session.seed))
    assert error.value.code == code
    assert len(calls) == 1
    assert str(calls[0].url) == BILL_ENDPOINT


def test_text_plain_json_accepted_and_only_bound_identity_sent(session, bill):
    def respond(request):
        assert request.headers["Accept"] == "*/*"
        assert request.headers["Cookie"] == session.cookie.get_secret_value()
        return httpx.Response(200, text=json.dumps(bill), headers={"Content-Type": "text/plain"})

    result = asyncio.run(JhzfClient(httpx.MockTransport(respond)).bill(session, session.seed))
    assert len(result["transactions"]) == 2
