from __future__ import annotations

import pytest

from upc_mcp.timetable_auth import (
    _cookies_from_cdp,
    _is_app_page,
    local_endpoint,
)
from upc_mcp.timetable_protocol import TimetableError
from upc_mcp.timetable_store import TimetableAuthLock


def test_loopback_endpoint_validation():
    assert local_endpoint("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    assert local_endpoint("http://[::1]:9222/") == "http://[::1]:9222"
    for value in (
        "https://127.0.0.1:9222",
        "http://example.com:9222",
        "http://127.0.0.1",
        "http://127.0.0.1:9222/path",
        "http://user:pass@127.0.0.1:9222",
    ):
        with pytest.raises(TimetableError) as caught:
            local_endpoint(value)
        assert caught.value.code == "invalid_input"


def test_app_page_filter_does_not_accept_cas_or_other_hosts():
    assert _is_app_page("https://app.upc.edu.cn/site/weekschedule/index")
    assert not _is_app_page("https://cas.upc.edu.cn/cas/login")
    assert not _is_app_page("http://app.upc.edu.cn/site/weekschedule/index")
    assert not _is_app_page("https://app.upc.edu.cn.evil.test/site/weekschedule/index")


def test_cdp_cookie_conversion_preserves_scope_and_filters_hosts():
    cookies = _cookies_from_cdp(
        [
            {
                "name": "eai-sess",
                "value": "synthetic-session",
                "domain": "app.upc.edu.cn",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
                "expires": -1,
            },
            {
                "name": "shared",
                "value": "synthetic-shared",
                "domain": ".upc.edu.cn",
                "path": "/site",
                "secure": False,
                "httpOnly": False,
                "expires": 1900000000.0,
            },
            {
                "name": "app-shared",
                "value": "synthetic-app-shared",
                "domain": ".app.upc.edu.cn",
                "path": "/timetable",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Strict",
            },
            {
                "name": "cas-ticket",
                "value": "synthetic-ticket",
                "domain": "cas.upc.edu.cn",
                "path": "/cas",
                "secure": False,
                "httpOnly": True,
            },
        ]
    )
    assert [cookie.name for cookie in cookies] == ["eai-sess", "shared", "app-shared"]
    assert cookies[0].secure is True
    assert cookies[0].http_only is True
    assert cookies[0].same_site == "Lax"
    assert cookies[1].path == "/site"
    assert cookies[1].expires_at == 1900000000.0
    assert cookies[2].domain == ".app.upc.edu.cn"
    assert cookies[2].path == "/timetable"


def test_auth_lock_contention_is_bounded(tmp_path):
    first = TimetableAuthLock(tmp_path, timeout=1.0)
    second = TimetableAuthLock(tmp_path, timeout=0.05)
    held = first.acquire()
    try:
        with pytest.raises(TimetableError) as caught:
            second.acquire()
    finally:
        held.release()
    assert caught.value.code == "auth_in_progress"
