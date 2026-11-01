import json
from urllib.parse import urlencode

import pytest
from pydantic import SecretStr

from upc_mcp.models import Session

# All values in this module are synthetic; never copied from school responses.
STUDENT = "0" * 10
ACCOUNT = "9" * 6
COOKIE = "JSESSIONID=SYNTHETIC_TEST_SESSION"


@pytest.fixture
def form():
    return {
        "sno": STUDENT,
        "xxbh": "synjones",
        "curpage": "1",
        "pagesize": "10",
        "account": ACCOUNT,
        "acctype": "",
        "query_start": "20260101",
        "query_end": "20260131",
    }


@pytest.fixture
def bill():
    row = {
        "account": ACCOUNT,
        "occtime": "20260102120000",
        "sign_tranamt": "-125",
        "tranname": "synthetic purchase",
        "mercname": "synthetic merchant",
        "acctype": "1",
    }
    return {"retcode": "0", "total": [row, dict(row)], "nextpage": "0", "rowcount": "2"}


@pytest.fixture
def session(form):
    return Session(
        student_id=STUDENT,
        account=ACCOUNT,
        cookie=SecretStr(COOKIE),
        headers={"Accept": "*/*"},
        seed=form,
        source="normal_cdp",
    )


@pytest.fixture
def capture_record(form, bill):
    return [
        {
            "request": {
                "method": "POST",
                "url": "http://jhzf.upc.edu.cn:20081/wechat/callinterface/queryHisTotal.html",
                "postData": urlencode(form),
                "headers": {"Accept": "*/*"},
            },
            "extra": {"headers": {"Cookie": COOKIE}},
            "response": {"status": 200},
            "body": json.dumps(bill),
        }
    ]
