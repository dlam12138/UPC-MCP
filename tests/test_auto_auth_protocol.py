"""Offline contract and lifecycle regression checks for unified authentication."""

from __future__ import annotations

import gzip
import json
from urllib.parse import parse_qs

import httpx
import pytest

from upc_mcp.auto_auth import Binding, Credential, CredentialStore
from upc_mcp.auto_auth.cas import (
    CAS_HOST,
    CasAuth,
    CasSessionStore,
    _ticket_from_location,
    bounded_request,
    cookies_from_client,
    jar_from_cookies,
    validate_cas_url,
)
from upc_mcp.auto_auth.credentials import _protect, account_hash
from upc_mcp.auto_auth.errors import AutoAuthError
from upc_mcp.auto_auth.locks import FileLock
from upc_mcp.auto_auth.providers.http import check_location, follow
from upc_mcp.auto_auth.state import RecoveryStateStore


def binding() -> Binding:
    return Binding(True, 1, account_hash("SYNTHETIC_USER"))


def cookies() -> list[dict]:
    return [{
        "name": "CASTGC", "value": "SYNTHETIC_COOKIE", "domain": CAS_HOST,
        "path": "/cas/", "secure": True, "http_only": True, "host_only": True,
        "expires": None,
    }]


def form(action: str = "") -> str:
    return (
        f'<form action="{action}"><input name="username">'
        '<input name="password" type="password">'
        '<input name="execution" value="SYNTHETIC_EXECUTION">'
        '<input name="_eventId" value="submit"></form>'
    )


@pytest.mark.parametrize("system", ["shiguang", "timetable", "library"])
def test_exact_current_service_urls_are_supported(system):
    assert validate_cas_url(CasAuth.login_url(system), system)


@pytest.mark.parametrize("action", [
    "https://cas.upc.edu.cn/not-login?service=http://dekt.upc.edu.cn/caslogin.jsp",
    "http://cas.upc.edu.cn/cas/login?service=http://dekt.upc.edu.cn/caslogin.jsp",
    "https://cas.upc.edu.cn:80/cas/login?service=http://dekt.upc.edu.cn/caslogin.jsp",
    "https://evil.invalid/cas/login?service=http://dekt.upc.edu.cn/caslogin.jsp",
    "https://cas.upc.edu.cn/cas/login?service=https://evil.invalid/callback",
])
def test_password_never_submitted_to_changed_action_or_service(action):
    submitted = []

    def handler(request):
        if request.method == "POST":
            submitted.append(request)
            raise AssertionError("unexpected credential submission")
        return httpx.Response(200, text=form(action))

    auth = CasAuth(transport=httpx.MockTransport(handler))
    with pytest.raises(AutoAuthError):
        auth.password_login("shiguang", Credential("SYNTHETIC_USER", "SYNTHETIC_PASS", binding()))
    assert submitted == []


def test_fresh_dynamic_execution_and_relative_official_action():
    executions = []
    gets = 0

    def handler(request):
        nonlocal gets
        if request.method == "GET":
            gets += 1
            body = form("/cas/login?service=http://dekt.upc.edu.cn/caslogin.jsp")
            return httpx.Response(200, text=body.replace("SYNTHETIC_EXECUTION", f"fresh-{gets}"))
        posted = parse_qs(request.content.decode())
        executions.append(posted["execution"][0])
        return httpx.Response(302, headers={
            "Location": "http://dekt.upc.edu.cn/caslogin.jsp?ticket=ST-synthetic",
            "Set-Cookie": "CASTGC=synthetic; Path=/cas/; HttpOnly; Secure",
        })

    auth = CasAuth(transport=httpx.MockTransport(handler))
    credential = Credential("SYNTHETIC_USER", "SYNTHETIC_PASS", binding())
    for _ in range(2):
        result = auth.password_login("shiguang", credential)
        assert result.ticket.system == "shiguang"
        assert "ST-synthetic" not in repr(result)
    assert executions == ["fresh-1", "fresh-2"]


@pytest.mark.parametrize("url", [
    "http://dekt.upc.edu.cn:443/caslogin.jsp?ticket=ST-synthetic",
    "http://dekt.upc.edu.cn/caslogin.jsp?ticket=ST-one&ticket=ST-two",
    "http://dekt.upc.edu.cn/caslogin.jsp?ticket=ST-synthetic&next=https://evil.invalid",
    "http://dekt.upc.edu.cn/caslogin.jsp?ticket=ST-synthetic#fragment",
])
def test_ticket_callback_rejects_ambiguity_and_wrong_port(url):
    with pytest.raises(AutoAuthError):
        _ticket_from_location(url, "shiguang", "sso")


@pytest.mark.parametrize("compress", [False, True])
def test_decompressed_size_bound(compress):
    raw = b"x" * 1001
    body = gzip.compress(raw) if compress else raw
    headers = {"Content-Encoding": "gzip"} if compress else {}
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=body, headers=headers)
    )) as http:
        with pytest.raises(AutoAuthError) as caught:
            bounded_request(http, "GET", "https://cas.upc.edu.cn/cas/login", limit=1000)
    assert caught.value.code == "cas_response_too_large"


def test_decompressed_response_is_not_decompressed_twice():
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=gzip.compress(b"valid"), headers={
            "Content-Encoding": "gzip", "Set-Cookie": "CASTGC=x; Path=/cas/; Secure",
        })
    )) as http:
        response = bounded_request(http, "GET", "https://cas.upc.edu.cn/cas/login")
        assert response.text == "valid"
        assert "content-encoding" not in response.headers
        assert cookies_from_client(http)[0]["host_only"] is True


def test_cookie_host_only_secure_and_expiry_survive_roundtrip():
    jar = jar_from_cookies(cookies(), domains={CAS_HOST})
    item = next(iter(jar.jar))
    assert item.domain_specified is False
    assert item.secure is True
    assert item.path == "/cas/"
    assert item.discard is True
    with httpx.Client(cookies=jar, trust_env=False) as http:
        request = http.build_request("GET", "http://cas.upc.edu.cn/cas/login")
        assert "Cookie" not in request.headers


def test_business_callback_cannot_follow_side_effect_path_or_external_host():
    for url in (
        "https://libspace.upc.edu.cn/rest/v2/cancel/123",
        "https://libspace.upc.edu.cn:80/libseat/",
        "https://evil.invalid/libseat/",
    ):
        with pytest.raises(AutoAuthError):
            check_location(url, "libspace.upc.edu.cn")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/rest/v2/cancel/123"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AutoAuthError):
            follow(http, "https://libspace.upc.edu.cn/libseat/", "libspace.upc.edu.cn")
    assert len(seen) == 1


@pytest.mark.parametrize("field,value", [
    ("blocked_until", float("nan")), ("blocked_until", float("inf")),
    ("blocked_until", -1), ("updated_at", float("nan")),
    ("updated_at", True), ("system_backoff", {"library": float("inf")}),
])
def test_corrupt_backoff_fails_closed_and_is_preserved(tmp_path, field, value):
    state = RecoveryStateStore(tmp_path)
    state.reset(binding())
    payload = json.loads(state.path.read_text(encoding="utf-8"))
    payload[field] = value
    original = json.dumps(payload).encode()
    state.path.write_bytes(original)
    with pytest.raises(AutoAuthError) as caught:
        state.load(binding())
    assert caught.value.code == "auto_auth_state_corrupt"
    assert state.path.read_bytes() == original


def test_credential_payload_cannot_be_rebound_by_plain_metadata(tmp_path):
    store = CredentialStore(tmp_path)
    first = store.store("SYNTHETIC_A", "SYNTHETIC_PASS")
    assert store.load().binding.matches(first)
    meta = json.loads(store.meta_path.read_text(encoding="utf-8"))
    meta["generation"] += 1
    store.meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(AutoAuthError) as caught:
        store.load()
    assert caught.value.code == "credential_unreadable"


def test_locked_credential_lifecycle_rejects_old_expected_binding(tmp_path):
    store = CredentialStore(tmp_path)
    first = store.store("SYNTHETIC_A", "SYNTHETIC_PASS")
    with store.guard():
        store.clear_locked()
        with pytest.raises(AutoAuthError) as caught:
            store.store_locked("SYNTHETIC_B", "SYNTHETIC_PASS", expected=first)
    assert caught.value.code == "credential_changed"
    assert store.snapshot().configured is False


def test_checkpoint_inner_binding_rejects_torn_or_modified_metadata(tmp_path):
    store = CasSessionStore(tmp_path)
    store.save(cookies(), binding())
    assert store.load(binding()) == cookies()
    forged = Binding(True, 2, account_hash("SYNTHETIC_B"))
    meta = json.loads(store.meta_path.read_text(encoding="utf-8"))
    meta.update(generation=forged.generation, account_hash=forged.account_hash)
    store.meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(AutoAuthError) as caught:
        store.load(forged)
    assert caught.value.code == "cas_checkpoint_corrupt"


def test_checkpoint_adoption_refuses_unknown_or_other_account(tmp_path):
    store = CasSessionStore(tmp_path)
    candidate = tmp_path / "candidate.dpapi"
    for account in (None, account_hash("SYNTHETIC_OTHER")):
        candidate.write_bytes(_protect(json.dumps({
            "version": 1, "cookies": cookies(), "account_hash": account,
        }).encode()))
        with pytest.raises(AutoAuthError) as caught:
            store.adopt_from(candidate, binding())
        assert caught.value.code == "candidate_mismatch"
    assert not store.path.exists()


def test_checkpoint_adoption_is_no_overwrite_under_lock(tmp_path):
    store = CasSessionStore(tmp_path)
    store.save(cookies(), binding())
    original = store.path.read_bytes()
    with pytest.raises(AutoAuthError) as caught:
        store.save(cookies(), binding(), require_absent=True)
    assert caught.value.code == "already_configured"
    assert store.path.read_bytes() == original


def test_same_lock_object_can_be_reused_without_leaking_lock(tmp_path):
    lock = FileLock(tmp_path / "test.lock", timeout=0.1)
    with lock.held():
        pass
    with lock.held():
        pass
    with FileLock(tmp_path / "test.lock", timeout=0.1).held():
        pass
