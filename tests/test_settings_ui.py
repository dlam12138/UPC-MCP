"""Actual loopback HTTP and DPAPI, using only synthetic local credentials."""

import threading
from contextlib import contextmanager

import httpx
import pytest

from upc_mcp.auto_auth.errors import AutoAuthError
from upc_mcp.settings_ui import MAX_BODY, SettingsApplication, SettingsServer


@pytest.fixture
def site(tmp_path, monkeypatch):
    original = httpx.Client.send

    def local_only(client, request, *args, **kwargs):
        assert request.url.host == "127.0.0.1", "No school request in UI tests"
        return original(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", local_only)
    app = SettingsApplication(tmp_path)
    server = SettingsServer(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


@contextmanager
def browser(server):
    with httpx.Client(base_url=server.origin, trust_env=False, timeout=5) as client:
        response = client.post("/api/bootstrap", json={}, headers={
            "Origin": server.origin, "X-UPC-Launch": server.launch_token,
        })
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=Strict" in response.headers["set-cookie"]
        data = client.get("/api/status").json()
        client.headers.update({"Origin": server.origin, "X-UPC-Settings": data["csrf"]})
        yield client


def payload(generation=0, **changes):
    data = {"username": "synthetic-student", "password": "synthetic-password",
            "password_confirm": "synthetic-password", "expected_generation": generation}
    data.update(changes)
    return data


def test_ui_static_assets_and_status_make_no_school_request(site):
    app, server = site
    with httpx.Client(base_url=server.origin, trust_env=False) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "https://" not in page.text
        for asset in ("settings.js", "settings.css", "body.ttf", "title.ttf"):
            assert client.get("/assets/" + asset).status_code == 200
        assert client.get("/api/status").status_code == 401
    with browser(server) as client:
        data = client.get("/api/status").json()
        assert data["credential"] == {
            "configured": False, "generation": 0, "error": None,
            "requires_user_action": False,
        }
        assert len(data["systems"]) == 4
        assert all(item["state"] == "absent" for item in data["systems"])
    assert not app.auto_auth.credentials.path.exists()


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
    {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_host_and_origin_restrictions_apply_even_with_session_cookie(site, headers):
    app, server = site
    with browser(server) as client:
        assert client.get("/api/status", headers=headers).status_code == 403
        assert client.post("/api/credentials", json=payload(), headers=headers).status_code == 403
    assert not app.auto_auth.binding().configured


def test_one_time_launch_capability_cannot_be_replayed_or_skipped(site):
    _app, server = site
    with httpx.Client(base_url=server.origin, trust_env=False) as client:
        assert client.post("/api/bootstrap", json={}, headers={
            "Origin": server.origin, "X-UPC-Launch": "wrong",
        }).status_code == 401
    with browser(server) as client:
        assert client.post("/api/bootstrap", json={}, headers={
            "X-UPC-Launch": server.launch_token,
        }).status_code == 401


def test_expired_launch_capability_does_not_open_settings(site):
    _app, server = site
    server.started_at -= 121
    with httpx.Client(base_url=server.origin, trust_env=False) as client:
        assert client.post("/api/bootstrap", json={}, headers={
            "Origin": server.origin, "X-UPC-Launch": server.launch_token,
        }).status_code == 401


def test_two_settings_windows_keep_independent_browser_sessions(site):
    app, first = site
    second = SettingsServer(app)
    thread = threading.Thread(target=second.serve_forever, daemon=True)
    thread.start()
    try:
        with browser(first) as client:
            response = client.post(second.origin + "/api/bootstrap", json={}, headers={
                "Origin": second.origin, "X-UPC-Launch": second.launch_token,
            })
            assert response.status_code == 200
            assert client.get("/api/status").status_code == 200
            assert client.get(second.origin + "/api/status", headers={
                "Origin": second.origin,
            }).status_code == 200
    finally:
        second.shutdown()
        second.server_close()
        thread.join(3)


def test_mutations_require_csrf_and_exact_origin(site):
    app, server = site
    with browser(server) as client:
        assert client.post("/api/credentials", json=payload(), headers={
            "X-UPC-Settings": "wrong",
        }).status_code == 403
        origin = client.headers.pop("Origin")
        assert client.post("/api/credentials", json=payload()).status_code == 403
        client.headers["Origin"] = origin
        assert client.get("/api/credentials").status_code == 404
    assert not app.auto_auth.binding().configured


def test_denied_post_body_is_drained_and_does_not_reset_browser_connection(site):
    app, server = site
    with browser(server) as client:
        for _ in range(20):
            response = client.post("/api/credentials", json=payload(), headers={
                "X-UPC-Settings": "wrong",
            })
            assert response.status_code == 403
            assert response.json()["status"] == "forbidden"
        assert client.get("/api/status").status_code == 200
    assert not app.auto_auth.binding().configured


def test_save_update_clear_and_no_secrets_in_responses(site):
    app, server = site
    with browser(server) as client:
        saved = client.post("/api/credentials", json=payload())
        assert saved.status_code == 200
        assert app.auto_auth.credentials.load().username == "synthetic-student"
        assert b"synthetic-password" not in app.auto_auth.credentials.path.read_bytes()
        status = client.get("/api/status")
        for response in (saved, status):
            assert "synthetic-password" not in response.text
            assert "synthetic-student" not in response.text
        assert status.json()["credential"]["generation"] == 1
        # A stale browser and an unacknowledged replacement cannot overwrite.
        assert client.post("/api/credentials", json=payload()).status_code == 409
        assert client.post("/api/credentials", json=payload(1)).status_code == 409
        updated = client.post("/api/credentials", json=payload(
            1, username="synthetic-replacement", replace=True,
        ))
        assert updated.status_code == 200
        assert client.post("/api/credentials/clear", json={
            "expected_generation": 1,
        }).status_code == 409
        assert app.auto_auth.credentials.load().username == "synthetic-replacement"
        assert client.post("/api/credentials/clear", json={
            "expected_generation": 2,
        }).status_code == 200
        assert not app.auto_auth.binding().configured


@pytest.mark.parametrize("changes", [
    {"username": ""}, {"username": "x" * 129}, {"password": ""},
    {"password": "x" * 513}, {"password_confirm": "different"},
    {"expected_generation": True}, {"username": ["invalid"]},
])
def test_invalid_form_never_writes_credential(site, changes):
    app, server = site
    with browser(server) as client:
        response = client.post("/api/credentials", json=payload(**changes))
        assert response.status_code == 400
        assert "synthetic-password" not in response.text
        assert "different" not in response.text
    assert not app.auto_auth.binding().configured


def test_bad_content_types_and_body_limits_are_rejected(site):
    app, server = site
    with browser(server) as client:
        assert client.post("/api/credentials", content="x", headers={
            "Content-Type": "text/plain",
        }).status_code == 415
        assert client.post("/api/credentials", content=b"x" * (MAX_BODY + 1), headers={
            "Content-Type": "application/json",
        }).status_code == 413
        for data in ("{broken", "[]", '"string"'):
            assert client.post("/api/credentials", content=data, headers={
                "Content-Type": "application/json",
            }).status_code == 400
    assert not app.auto_auth.binding().configured


def test_checks_do_not_return_business_payloads_or_raw_exception(site, monkeypatch):
    from upc_mcp.library_service import LibraryService

    _app, server = site

    async def good(_self):
        return {"status": "ok", "private_title": "synthetic-private-business-record"}

    async def bad(_self):
        raise RuntimeError("synthetic-private-password-in-error")

    with browser(server) as client:
        monkeypatch.setattr(LibraryService, "check", good)
        response = client.post("/api/check", json={"system": "library"})
        assert response.status_code == 200
        assert "private" not in response.text
        monkeypatch.setattr(LibraryService, "check", bad)
        response = client.post("/api/check", json={"system": "library"})
        assert response.status_code == 400
        assert "private" not in response.text
        assert client.post("/api/check", json={"system": "arbitrary"}).status_code == 400


def test_guarded_clear_cannot_remove_concurrently_reconfigured_credentials(tmp_path):
    app = SettingsApplication(tmp_path)
    old = app.auto_auth.credentials.store("synthetic-a", "synthetic-password")
    app.auto_auth.credentials.store("synthetic-b", "synthetic-password")
    with pytest.raises(AutoAuthError) as error:
        app.auto_auth.clear_credential(expected=old)
    assert error.value.code == "credential_changed"
    assert app.auto_auth.credentials.load().username == "synthetic-b"


def test_settings_parser_never_accepts_password_or_public_bind_address():
    from upc_mcp.cli import parser

    args = parser().parse_args(["settings", "--port", "0"])
    assert args.port == 0 and args.idle_minutes == 30
    for arguments in (["--password", "synthetic-secret"], ["--host", "0.0.0.0"]):
        with pytest.raises(SystemExit):
            parser().parse_args(["settings", *arguments])
