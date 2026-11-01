# ruff: noqa: E501
import asyncio
import gzip
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from upc_mcp.shiguang import ShiguangError
from upc_mcp.shiguang_auth import ShiguangAuth
from upc_mcp.shiguang_service import MAX_RECEIPT_BYTES, ShiguangService
from upc_mcp.shiguang_session import (
    Cookie,
    RecoveryLock,
    ShiguangSession,
    ShiguangStore,
    identity_hash_from_html,
    jar_from_cookies,
    protect,
)

LIST_HTML = """
<div class="active_2_list"><div class="top clear"><div class="li clear">
<a class="ellipsis" href="/actvMng/actvMngFront!jumpActvCampViewPageForWeb.action?actvModel.activityId=1">示例活动</a>
</div></div></div>
<div class="page-mod"><span>共 1 页</span></div>
"""


def session(cookies=None, state="stored"):
    return ShiguangSession(
        cookies=cookies
        or [
            Cookie(
                name="SESSION",
                value=SecretStr("synthetic-dekt-session"),
                domain="dekt.upc.edu.cn",
                path="/",
                http_only=True,
                session=True,
            ),
            Cookie(
                name="CASTGC",
                value=SecretStr("synthetic-castgc"),
                domain="cas.upc.edu.cn",
                path="/cas/",
                http_only=True,
                session=True,
            ),
        ],
        state=state,
        source="adopted_dpapi",
    )


def test_dpapi_roundtrip_public_has_no_secret(tmp_path):
    store = ShiguangStore(tmp_path)
    original = session()
    store.replace(original)
    raw = store.path.read_bytes()
    assert b"synthetic-dekt-session" not in raw
    assert b"synthetic-castgc" not in raw
    loaded = store.load()
    assert loaded is not None
    assert loaded.revision == original.revision
    public = json.dumps(loaded.public(), ensure_ascii=False)
    assert "synthetic" not in public
    assert "value" not in public
    assert "cookie_name" not in public
    assert store.path.name == "shiguang-self.dpapi"
    assert not (tmp_path / "jhzf-self.dpapi").exists()


def test_cookie_jar_preserves_secure_and_domain_boundaries():
    cookies = [
        Cookie(
            name="SESSION",
            value=SecretStr("dekt-value"),
            domain="dekt.upc.edu.cn",
            path="/",
            secure=False,
        ),
        Cookie(
            name="CASTGC",
            value=SecretStr("cas-value"),
            domain="cas.upc.edu.cn",
            path="/cas/",
            secure=True,
        ),
    ]
    jar = jar_from_cookies(cookies)

    def handler(request):
        return httpx.Response(200, text=request.headers.get("cookie", ""))

    with httpx.Client(cookies=jar, transport=httpx.MockTransport(handler)) as client:
        assert client.get("http://dekt.upc.edu.cn/").text == "SESSION=dekt-value"
        assert "CASTGC" not in client.get("http://dekt.upc.edu.cn/").text
        assert client.get("https://cas.upc.edu.cn/cas/login").text == "CASTGC=cas-value"
        assert "SESSION" not in client.get("https://cas.upc.edu.cn/cas/login").text


def test_clear_rejects_late_finish(tmp_path):
    store = ShiguangStore(tmp_path)
    original = session()
    store.replace(original)
    store.clear()
    with pytest.raises(ShiguangError) as caught:
        store.finish(original.revision)
    assert caught.value.code == "auth_required"
    assert store.load() is None


def test_rebinding_rejects_old_revision(tmp_path):
    store = ShiguangStore(tmp_path)
    first = session()
    second = session()
    store.replace(first)
    store.replace(second)
    with pytest.raises(ShiguangError) as caught:
        store.finish(first.revision)
    assert caught.value.code == "auth_required"
    assert store.load().revision == second.revision


def test_generation_survives_clear_as_tombstone(tmp_path):
    store = ShiguangStore(tmp_path)
    assert store.generation() == 0
    store.replace(session())
    assert store.generation() == 1
    store.clear()
    assert store.generation() == 2
    assert store.load() is None
    store.replace(session())
    assert store.generation() == 3


def test_candidate_filters_artifact_cookies(tmp_path):
    candidate = tmp_path / "candidate.dpapi"
    payload = {
        "version": 1,
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-dekt",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
                "httpOnly": True,
                "secure": False,
                "session": True,
            },
            {
                "name": "CASTGC",
                "value": "synthetic-cas",
                "domain": "cas.upc.edu.cn",
                "path": "/cas/",
                "httpOnly": True,
                "secure": False,
                "session": True,
            },
            {
                "name": "HttpOnly",
                "value": "",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
            },
        ],
    }
    candidate.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    parsed = ShiguangStore.candidate(candidate)
    assert {cookie.name for cookie in parsed.cookies} == {"SESSION", "CASTGC"}
    assert parsed.has_dekt_session()
    assert parsed.has_cas_sso()


def test_candidate_without_dekt_session_is_rejected(tmp_path):
    candidate = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "CASTGC",
                "value": "synthetic-cas",
                "domain": "cas.upc.edu.cn",
                "path": "/cas/",
            }
        ]
    }
    candidate.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    with pytest.raises(ShiguangError) as caught:
        ShiguangStore.candidate(candidate)
    assert caught.value.code == "auth_required"


def test_malformed_candidate_cookie_is_invalid_input(tmp_path):
    candidate = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-dekt",
                "domain": "dekt.upc.edu.cn",
                "path": "/" + "x" * 300,
            }
        ]
    }
    candidate.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    with pytest.raises(ShiguangError) as caught:
        ShiguangStore.candidate(candidate)
    assert caught.value.code == "invalid_input"


def cas_handler(callback_status=200, gzip_callback=False, seen=None):
    seen = seen if seen is not None else {}

    def handler(request):
        host = request.url.host
        path = request.url.path
        if host == "dekt.upc.edu.cn" and path == "/caslogin.jsp" and "ticket" not in request.url.params:
            return httpx.Response(
                302,
                headers={
                    "location": "https://cas.upc.edu.cn/cas/login?service=x",
                    "set-cookie": "SESSION=anonymous; Path=/; HttpOnly",
                },
            )
        if host == "cas.upc.edu.cn" and path == "/cas/login":
            return httpx.Response(
                302,
                headers={"location": "http://dekt.upc.edu.cn/caslogin.jsp?ticket=ST-new"},
            )
        if host == "dekt.upc.edu.cn" and path == "/caslogin.jsp" and "ticket" in request.url.params:
            if callback_status == 500:
                return httpx.Response(
                    500,
                    text="synthetic callback error",
                    headers={"set-cookie": "SESSION=recovered; Path=/; HttpOnly"},
                )
            html = (
                '<html><body><script>var pcRedirectUrl = '
                '"/sysmng/login!loginByUserNoForCas.action?userName=opaque";</script></body></html>'
            )
            if gzip_callback:
                body = gzip.compress(html.encode("utf-8"))
                return httpx.Response(
                    200,
                    content=body,
                    headers={
                        "content-type": "text/html;charset=UTF-8",
                        "content-encoding": "gzip",
                        "content-length": str(len(body)),
                    },
                )
            return httpx.Response(200, text=html)
        if host == "dekt.upc.edu.cn" and path == "/sysmng/login!loginByUserNoForCas.action":
            seen["second_stage"] = True
            return httpx.Response(
                302,
                headers={
                    "location": "http://dekt.upc.edu.cn/web/index.html",
                    "set-cookie": "SESSION=recovered; Path=/; HttpOnly",
                },
            )
        if host == "dekt.upc.edu.cn" and path == "/web/index.html":
            return httpx.Response(200, text="<html>ok</html>")
        if host == "dekt.upc.edu.cn" and path.endswith("jumpActvCampListPageForWeb.action"):
            return httpx.Response(
                200,
                text=LIST_HTML,
                headers={"content-type": "text/html;charset=UTF-8"},
            )
        return httpx.Response(404)

    return handler


def test_auth_recovery_follows_second_stage():
    candidate = session()
    recovered = ShiguangAuth(transport=httpx.MockTransport(cas_handler())).recover(candidate)
    assert recovered.state == "stored"
    assert recovered.source == "cas_sso_recovery"
    assert recovered.has_cas_sso()
    assert recovered.has_dekt_session()
    assert recovered.identity_verified is False


def test_auth_callback_500_still_verifies_business_read():
    candidate = session()
    recovered = ShiguangAuth(transport=httpx.MockTransport(cas_handler(500))).recover(candidate)
    assert recovered.has_dekt_session()


def test_auth_recovery_handles_gzip_transition_page():
    seen = {}
    candidate = session()
    recovered = ShiguangAuth(
        transport=httpx.MockTransport(cas_handler(gzip_callback=True, seen=seen))
    ).recover(candidate)
    assert recovered.has_dekt_session()
    assert seen.get("second_stage") is True


def identity_handler(value: str):
    html = LIST_HTML.replace(
        '<div class="active_2_list">',
        f'<input name="currId" value="{value}"><div class="active_2_list">',
    )
    base = cas_handler()

    def handler(request):
        if request.url.host == "dekt.upc.edu.cn" and request.url.path.endswith(
            "jumpActvCampListPageForWeb.action"
        ):
            return httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html;charset=UTF-8"},
            )
        return base(request)

    return handler


def test_identity_is_not_promoted_without_existing_verified_binding():
    candidate = session()
    candidate.identity_hash = None
    candidate.identity_verified = False
    recovered = ShiguangAuth(
        transport=httpx.MockTransport(identity_handler("page-value"))
    ).recover(candidate)
    assert recovered.identity_verified is False


def test_identity_mismatch_rejects_recovery():
    candidate = session()
    candidate.identity_hash = "0" * 64
    candidate.identity_verified = True
    with pytest.raises(ShiguangError) as caught:
        ShiguangAuth(transport=httpx.MockTransport(identity_handler("page-value"))).recover(
            candidate
        )
    assert caught.value.code == "auth_required"


def test_identity_match_preserves_verified_binding():
    candidate = session()
    candidate.identity_hash = identity_hash_from_html(
        '<input name="currId" value="page-value">'
    )
    candidate.identity_verified = True
    recovered = ShiguangAuth(
        transport=httpx.MockTransport(identity_handler("page-value"))
    ).recover(candidate)
    assert recovered.identity_verified is True
    assert recovered.identity_hash == candidate.identity_hash


def test_auth_cas_login_page_stops_without_session():
    def handler(request):
        if request.url.host == "dekt.upc.edu.cn":
            return httpx.Response(302, headers={"location": "https://cas.upc.edu.cn/cas/login?service=x"})
        return httpx.Response(200, text='<html><form id="loginForm"></form></html>')

    with pytest.raises(ShiguangError) as caught:
        ShiguangAuth(transport=httpx.MockTransport(handler)).recover(session())
    assert caught.value.code == "auth_required"


class FakeAuth:
    def __init__(self):
        self.calls = 0
        self.recovered = False

    def recover(self, current):
        self.calls += 1
        self.recovered = True
        return current.model_copy(
            update={
                "revision": "recovered-revision",
                "binding_id": current.effective_binding_id(),
                "state": "stored",
            }
        )


class FakeService(ShiguangService):
    def __init__(self, store, auth, execute):
        super().__init__(store, auth=auth)
        self.execute = execute

    def _execute(self, current, operation):
        return self.execute(current, operation)


def registration_state(status="open"):
    return {
        "status": status,
        "activity_id": "111",
        "kind": "activity",
        "normal_supported": True,
        "invite_code_supported": True,
        "reason_required": False,
        "can_register": status == "open",
        "enroll_start": "2026-01-01 00:00",
        "enroll_end": "2026-01-02 00:00",
        "activity_start": "2026-01-03 00:00",
        "activity_end": "2026-01-04 00:00",
        "already_registered": None,
    }


class FakeRegistrationClient:
    def __init__(
        self,
        preview_status="open",
        on_preview=None,
        on_register=None,
        result=None,
        preview_error=None,
        register_error=None,
        fail_preview_once=False,
    ):
        self.preview_status = preview_status
        self.on_preview = on_preview
        self.on_register = on_register
        self.result = result or {
            "status": "registered_pending_review",
            "submitted": True,
            "retry_recommended": False,
            "server_oper": "success",
            "message": "报名成功，等待活动管理员审核。",
        }
        self.preview_error = preview_error
        self.register_error = register_error
        self.fail_preview_once = fail_preview_once
        self.preview_calls = 0
        self.register_calls = 0

    def registration_preview(self, activity_id, kind):
        self.preview_calls += 1
        if self.on_preview:
            self.on_preview()
        if self.fail_preview_once and self.preview_calls == 1:
            raise ShiguangError("auth_required", "synthetic preview expiry")
        if self.preview_error:
            raise self.preview_error
        return {
            "activity_id": activity_id,
            "kind": kind,
            "title": "示例活动",
            "registration": registration_state(self.preview_status),
        }

    def activities(self, page=1, keyword=None, online_type=None, base_type=None):
        return {"status": "ok", "source_system": "shiguang", "items": [], "total_pages": 1}

    def register_activity(self, activity_id, kind, invite_code=None, reason=None):
        self.register_calls += 1
        if self.on_register:
            self.on_register()
        if self.register_error:
            raise self.register_error
        return self.result


class FakeRegistrationService(ShiguangService):
    def __init__(self, store, client, auth=None, timeout=0.2):
        super().__init__(
            store,
            auth=auth or FakeAuth(),
            registration_lock_timeout=timeout,
        )
        self.client = client

    def _execute(self, current, operation):
        return operation(self.client)


class SlowReadClient:
    def activities(self, page=1, keyword=None, online_type=None, base_type=None):
        return {"status": "ok", "source_system": "shiguang", "items": [], "total_pages": 1}

    def credits(self):
        time.sleep(0.5)
        return {"status": "ok", "source_system": "shiguang", "modules": []}


def test_service_registration_success_submits_once(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient()
    service = FakeRegistrationService(store, client)
    result = asyncio.run(service.register_activity("111", "activity"))
    assert result["status"] == "registered_pending_review"
    assert result["submitted"] is True
    assert result["retry"] is False
    assert client.preview_calls == 1
    assert client.register_calls == 1
    assert result["warnings"] == []


def test_service_recent_success_suppresses_repeat_without_post(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient()
    service = FakeRegistrationService(store, client)
    first = asyncio.run(service.register_activity("111", "activity"))
    second = asyncio.run(service.register_activity("111", "activity"))
    assert first["status"] == "registered_pending_review"
    assert second["status"] == "registration_recently_submitted"
    assert second["submitted"] is True
    assert client.register_calls == 1


def test_service_registration_closed_does_not_submit(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient(preview_status="closed")
    service = FakeRegistrationService(store, client)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "activity"))
    assert caught.value.code == "registration_closed"
    assert client.register_calls == 0


def test_service_social_registration_requires_reason_without_submit(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient(preview_status="open")
    service = FakeRegistrationService(store, client)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "social_practice"))
    assert caught.value.code == "requires_user_action"
    assert caught.value.details["action"] == "provide_reason"
    assert client.register_calls == 0


def test_service_clear_during_preview_does_not_submit(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient(on_preview=store.clear)
    service = FakeRegistrationService(store, client)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "activity"))
    assert caught.value.code == "auth_required"
    assert client.register_calls == 0
    assert store.load() is None


def test_service_clear_during_submit_keeps_remote_result(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient(on_register=store.clear)
    service = FakeRegistrationService(store, client)
    result = asyncio.run(service.register_activity("111", "activity"))
    assert result["status"] == "registered_pending_review"
    assert client.register_calls == 1
    assert result["warnings"]


def test_service_registration_lock_blocks_second_writer(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    started = threading.Event()
    release = threading.Event()

    def block_preview():
        started.set()
        release.wait(5)

    first = FakeRegistrationClient(on_preview=block_preview)
    second = FakeRegistrationClient()
    service_one = FakeRegistrationService(store, first)
    service_two = FakeRegistrationService(store, second)

    async def scenario():
        task = asyncio.create_task(service_one.register_activity("111", "activity"))
        assert await asyncio.to_thread(started.wait, 5)
        beats = 0

        async def heartbeat():
            nonlocal beats
            while not release.is_set():
                beats += 1
                await asyncio.sleep(0.02)

        beat_task = asyncio.create_task(heartbeat())
        with pytest.raises(ShiguangError) as caught:
            await service_two.register_activity("111", "activity")
        assert caught.value.code == "registration_in_progress"
        release.set()
        await beat_task
        assert beats >= 3
        return await task

    result = asyncio.run(scenario())
    assert result["status"] == "registered_pending_review"
    assert first.register_calls == 1
    assert second.register_calls == 0


def test_cancelled_registration_keeps_lock_until_worker_finishes(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def block_write():
        started.set()
        release.wait(5)
        finished.set()

    first = FakeRegistrationClient(on_register=block_write)
    second = FakeRegistrationClient()
    service_one = FakeRegistrationService(store, first)
    service_two = FakeRegistrationService(store, second)

    async def scenario():
        task = asyncio.create_task(service_one.register_activity("111", "activity"))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Cancelling the await does not stop the worker or release its file lock.
        with pytest.raises(ShiguangError) as caught:
            await service_two.register_activity("111", "activity")
        assert caught.value.code == "registration_in_progress"
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0.1)
        return first.register_calls, second.register_calls

    first_calls, second_calls = asyncio.run(scenario())
    assert first_calls == 1
    assert second_calls == 0


def test_slow_credits_does_not_block_activities(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    service = FakeRegistrationService(store, SlowReadClient())

    async def scenario():
        credits = asyncio.create_task(service.credits())
        await asyncio.sleep(0.05)
        activities = await asyncio.wait_for(service.activities(), timeout=0.2)
        assert await credits
        return activities

    result = asyncio.run(scenario())
    assert result["status"] == "ok"


def test_reads_are_not_blocked_by_inflight_registration(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    started = threading.Event()
    release = threading.Event()

    def block_preview():
        started.set()
        release.wait(5)

    client = FakeRegistrationClient(on_preview=block_preview)
    service = FakeRegistrationService(store, client)

    async def scenario():
        registration = asyncio.create_task(
            service.register_activity("111", "activity")
        )
        assert await asyncio.to_thread(started.wait, 5)
        # The write holds the registration lock, not the read lock.
        read = await asyncio.wait_for(service.activities(), timeout=1)
        release.set()
        return read, await registration

    read, registration = asyncio.run(scenario())
    assert read["status"] == "ok"
    assert registration["status"] == "registered_pending_review"


def test_service_preview_recovery_then_one_submit(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    auth = FakeAuth()
    client = FakeRegistrationClient(fail_preview_once=True)
    service = FakeRegistrationService(store, client, auth=auth)
    result = asyncio.run(service.register_activity("111", "activity"))
    assert result["status"] == "registered_pending_review"
    assert auth.calls == 1
    assert client.preview_calls == 2
    assert client.register_calls == 1


def test_service_submission_unknown_is_not_retried(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient(
        register_error=ShiguangError("submission_unknown", "synthetic timeout")
    )
    service = FakeRegistrationService(store, client)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "activity"))
    assert caught.value.code == "submission_unknown"
    assert caught.value.details["submitted"] is None
    assert client.register_calls == 1


def test_unknown_registration_result_does_not_block_reads(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient(
        register_error=ShiguangError("submission_unknown", "synthetic timeout")
    )
    service = FakeRegistrationService(store, client)
    with pytest.raises(ShiguangError):
        asyncio.run(service.register_activity("111", "activity"))
    read = asyncio.run(service.activities())
    assert read["status"] == "ok"
    assert client.register_calls == 1


def test_unknown_result_is_suppressed_across_services(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    first = FakeRegistrationClient(
        register_error=ShiguangError("submission_unknown", "synthetic timeout")
    )
    second = FakeRegistrationClient()
    service_one = FakeRegistrationService(store, first)
    service_two = FakeRegistrationService(store, second)
    with pytest.raises(ShiguangError):
        asyncio.run(service_one.register_activity("111", "activity"))
    result = asyncio.run(service_two.register_activity("111", "activity"))
    assert result["status"] == "submission_unknown"
    assert result["submitted"] is None
    assert result["verification_required"] is True
    assert first.register_calls == 1
    assert second.register_calls == 0


def test_unknown_result_payload_is_suppressed_across_services(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    first = FakeRegistrationClient(
        result={
            "status": "submission_unknown",
            "submitted": None,
            "retry_recommended": False,
            "server_oper": None,
            "verification_required": True,
            "message": "synthetic unknown business code",
        }
    )
    second = FakeRegistrationClient()
    service_one = FakeRegistrationService(store, first)
    service_two = FakeRegistrationService(store, second)
    first_result = asyncio.run(service_one.register_activity("111", "activity"))
    second_result = asyncio.run(service_two.register_activity("111", "activity"))
    assert first_result["status"] == "submission_unknown"
    assert first_result["status"] != "submission_rejected"
    assert first_result["verification_required"] is True
    assert second_result["status"] == "submission_unknown"
    assert second_result["verification_required"] is True
    assert first.register_calls == 1
    assert second.register_calls == 0


@pytest.mark.parametrize(
    "raw",
    [
        '{"version":1,"records":{',
        '{"version":2,"records":{}}',
        '{"version":1,"records":[]}',
        '{"version":1,"records":{"k":{"status":"weird","updated_at":1,'
        '"activity_id":"111","kind":"activity"}}}',
        '{"version":1,"records":{"k":{"status":"success","updated_at":"not-a-time",'
        '"activity_id":"111","kind":"activity"}}}',
        '{"version":1,"records":{"k":{"status":"success","updated_at":NaN,'
        '"activity_id":"111","kind":"activity"}}}',
        '{"version":1,"records":{"k":{"status":"success","updated_at":Infinity,'
        '"activity_id":"111","kind":"activity"}}}',
        '{"version":1,"records":{"k":{"status":"success","updated_at":1e99,'
        '"activity_id":"111","kind":"activity"}}}',
    ],
)
def test_corrupt_receipts_fail_closed_without_post(tmp_path, raw):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient()
    service = FakeRegistrationService(store, client)
    service.receipts_path.write_text(raw, encoding="utf-8")
    original = service.receipts_path.read_bytes()
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "activity"))
    assert caught.value.code == "local_error"
    assert caught.value.details["verification_required"] is True
    assert client.register_calls == 0
    assert client.preview_calls == 0
    assert service.receipts_path.read_bytes() == original
    assert asyncio.run(service.activities())["status"] == "ok"


def test_oversize_receipts_fail_closed_without_post(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    client = FakeRegistrationClient()
    service = FakeRegistrationService(store, client)
    service.receipts_path.write_bytes(b"{" + b"x" * MAX_RECEIPT_BYTES)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "activity"))
    assert caught.value.code == "local_error"
    assert client.register_calls == 0
    assert asyncio.run(service.activities())["status"] == "ok"


def test_service_write_auth_failure_does_not_trigger_sso(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    auth = FakeAuth()
    client = FakeRegistrationClient(
        register_error=ShiguangError("auth_required", "synthetic write auth failure")
    )
    service = FakeRegistrationService(store, client, auth=auth)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.register_activity("111", "activity"))
    assert caught.value.code == "auth_required"
    assert client.register_calls == 1
    assert client.preview_calls == 1
    assert auth.calls == 0


def test_service_attempts_one_recovery_and_one_retry(tmp_path):
    store = ShiguangStore(tmp_path)
    original = session()
    store.replace(original)
    auth = FakeAuth()
    calls = {"execute": 0}

    def execute(current, operation):
        calls["execute"] += 1
        if calls["execute"] == 1:
            raise ShiguangError("auth_required", "synthetic expiry")
        return {"status": "ok", "source_system": "shiguang", "items": [], "total_pages": 1}

    service = FakeService(store, auth, execute)
    result = asyncio.run(service.activities())
    assert result["status"] == "ok"
    assert auth.calls == 1
    assert calls["execute"] == 2


def test_service_http_500_does_not_trigger_recovery(tmp_path):
    store = ShiguangStore(tmp_path)
    original = session()
    store.replace(original)
    auth = FakeAuth()

    def execute(current, operation):
        raise ShiguangError("upstream_error", "synthetic 500")

    service = FakeService(store, auth, execute)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.activities())
    assert caught.value.code == "upstream_error"
    assert auth.calls == 0
    assert store.load().revision == original.revision


def test_service_403_does_not_trigger_recovery(tmp_path):
    store = ShiguangStore(tmp_path)
    original = session()
    store.replace(original)
    auth = FakeAuth()

    def execute(current, operation):
        raise ShiguangError("access_denied", "synthetic 403")

    service = FakeService(store, auth, execute)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.activities())
    assert caught.value.code == "access_denied"
    assert auth.calls == 0
    assert store.load().revision == original.revision


def test_two_service_instances_share_one_recovery(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    auth = FakeAuth()
    barrier = threading.Barrier(2)

    def execute(current, operation):
        if not auth.recovered:
            barrier.wait(timeout=5)
            raise ShiguangError("auth_required", "synthetic expiry")
        return {"status": "ok", "items": [], "total_pages": 1}

    service_one = FakeService(store, auth, execute)
    service_two = FakeService(store, auth, execute)

    async def scenario():
        return await asyncio.gather(
            service_one.activities(),
            service_two.activities(),
        )

    results = asyncio.run(scenario())
    assert all(result["status"] == "ok" for result in results)
    assert auth.calls == 1


def test_concurrent_recovery_failure_does_not_loop(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    barrier = threading.Barrier(2)

    class FailingAuth:
        def __init__(self):
            self.calls = 0

        def recover(self, current):
            self.calls += 1
            raise ShiguangError("auth_required", "synthetic no SSO")

    auth = FailingAuth()

    def execute(current, operation):
        barrier.wait(timeout=5)
        raise ShiguangError("auth_required", "synthetic expiry")

    service_one = FakeService(store, auth, execute)
    service_two = FakeService(store, auth, execute)

    async def scenario():
        return await asyncio.gather(
            service_one.activities(),
            service_two.activities(),
            return_exceptions=True,
        )

    results = asyncio.run(scenario())
    codes = sorted(
        result.code for result in results if isinstance(result, ShiguangError)
    )
    assert codes == ["auth_required", "recovery_backoff"]
    assert auth.calls == 1


def test_service_recovery_failure_keeps_previous_session(tmp_path):
    store = ShiguangStore(tmp_path)
    original = session()
    store.replace(original)

    class FailingAuth:
        def recover(self, current):
            raise ShiguangError("auth_required", "synthetic no SSO")

    def execute(current, operation):
        raise ShiguangError("auth_required", "synthetic expiry")

    service = FakeService(store, FailingAuth(), execute)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.activities())
    assert caught.value.code == "auth_required"
    assert store.load().revision == original.revision


def test_service_clear_during_request_discards_result(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())

    def execute(current, operation):
        store.clear()
        return {"status": "ok", "items": [], "total_pages": 1}

    service = FakeService(store, FakeAuth(), execute)
    with pytest.raises(ShiguangError) as caught:
        asyncio.run(service.activities())
    assert caught.value.code == "auth_required"
    assert store.load() is None


def test_service_concurrent_expiry_recovers_once(tmp_path):
    store = ShiguangStore(tmp_path)
    store.replace(session())
    auth = FakeAuth()

    def execute(current, operation):
        if not auth.recovered:
            raise ShiguangError("session_expired", "synthetic expiry")
        return {"status": "ok", "items": [], "total_pages": 1}

    service = FakeService(store, auth, execute)

    async def scenario():
        return await asyncio.gather(service.activities(), service.activities())

    results = asyncio.run(scenario())
    assert all(result["status"] == "ok" for result in results)
    assert auth.calls == 1


def test_adopt_candidate_verifies_before_writing(tmp_path):
    source = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-dekt",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
                "httpOnly": True,
                "session": True,
            }
        ]
    }
    source.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    store = ShiguangStore(tmp_path / "state")

    class AdoptService(ShiguangService):
        def _execute(self, current, operation):
            return {"status": "ok", "items": [], "total_pages": 1}

    result = asyncio.run(AdoptService(store).adopt_candidate(source))
    assert result["status"] == "ok"
    assert store.load() is not None
    public = json.dumps(store.load().public(), ensure_ascii=False)
    assert "synthetic-dekt" not in public


def test_adopt_failure_does_not_write(tmp_path):
    source = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-dekt",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
                "httpOnly": True,
                "session": True,
            }
        ]
    }
    source.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    store = ShiguangStore(tmp_path / "state")

    class FailingService(ShiguangService):
        def _execute(self, current, operation):
            raise ShiguangError("auth_required", "synthetic invalid session")

    with pytest.raises(ShiguangError):
        asyncio.run(FailingService(store).adopt_candidate(source))
    assert store.load() is None


def test_adopt_refuses_existing_binding(tmp_path):
    source = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-new",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
                "httpOnly": True,
                "session": True,
            }
        ]
    }
    source.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    store = ShiguangStore(tmp_path / "state")
    original = session()
    store.replace(original)

    with pytest.raises(ShiguangError) as caught:
        asyncio.run(ShiguangService(store).adopt_candidate(source))
    assert caught.value.code == "already_bound"
    assert store.load().revision == original.revision


def test_adopt_clear_during_verification_does_not_resurrect(tmp_path):
    source = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-dekt",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
                "httpOnly": True,
                "session": True,
            }
        ]
    }
    source.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    store = ShiguangStore(tmp_path / "state")

    class ClearingService(ShiguangService):
        def _execute(self, current, operation):
            store.clear()
            return {"status": "ok", "items": [], "total_pages": 1}

    with pytest.raises(ShiguangError) as caught:
        asyncio.run(ClearingService(store).adopt_candidate(source))
    assert caught.value.code == "auth_required"
    assert store.load() is None


def test_adopt_rebind_during_verification_does_not_overwrite(tmp_path):
    source = tmp_path / "candidate.dpapi"
    payload = {
        "cookies": [
            {
                "name": "SESSION",
                "value": "synthetic-dekt",
                "domain": "dekt.upc.edu.cn",
                "path": "/",
                "httpOnly": True,
                "session": True,
            }
        ]
    }
    source.write_bytes(protect(json.dumps(payload).encode("utf-8")))
    store = ShiguangStore(tmp_path / "state")
    other = session().model_copy(
        update={"revision": "other-revision", "binding_id": "other-binding"}
    )

    class RebindingService(ShiguangService):
        def _execute(self, current, operation):
            store.replace(other)
            return {"status": "ok", "items": [], "total_pages": 1}

    with pytest.raises(ShiguangError) as caught:
        asyncio.run(RebindingService(store).adopt_candidate(source))
    assert caught.value.code == "auth_required"
    current = store.load()
    assert current is not None
    assert current.revision == "other-revision"
    assert current.binding_id == "other-binding"


def test_recovery_lock_is_cross_process(tmp_path):
    marker = tmp_path / "child-locked"
    code = f"""
import time
from pathlib import Path
from upc_mcp.shiguang_session import RecoveryLock

with RecoveryLock(Path(r"{tmp_path}"), timeout=5.0).held():
    Path(r"{marker}").write_text("locked", encoding="utf-8")
    time.sleep(1.2)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"lock child exited early: {process.returncode}")
            time.sleep(0.05)
        assert marker.exists()
        with pytest.raises(ShiguangError) as caught:
            with RecoveryLock(tmp_path, timeout=0.2).held():
                pass
        assert caught.value.code == "recovery_in_progress"
    finally:
        process.terminate()
        process.wait(timeout=5)
