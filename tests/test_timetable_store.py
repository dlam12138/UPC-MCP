from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr

from upc_mcp.timetable_protocol import TimetableCookie, TimetableError
from upc_mcp.timetable_store import (
    MAX_CANDIDATE_BYTES,
    MAX_SESSION_BYTES,
    TimetableSession,
    TimetableStore,
)


def cookie(name: str = "eai-sess", value: str = "synthetic-session") -> TimetableCookie:
    return TimetableCookie(
        name=name,
        value=SecretStr(value),
        domain="app.upc.edu.cn",
        path="/",
        secure=True,
        http_only=True,
        same_site="Lax",
    )


def session() -> TimetableSession:
    return TimetableSession(
        cookies=[cookie()],
        state="stored",
        source="adopted_candidate",
    )


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_store_paths_cookie_scope_and_public_summary(tmp_path):
    store = TimetableStore(tmp_path)
    original = session()
    store.replace(original)
    loaded = store.load()
    assert store.path.name == "timetable-self.dpapi"
    assert (tmp_path / "timetable-session.lock").exists()
    assert (tmp_path / "timetable-generation").exists()
    assert loaded is not None
    assert loaded.cookies[0].domain == "app.upc.edu.cn"
    assert loaded.cookies[0].path == "/"
    assert loaded.cookies[0].secure is True
    assert loaded.cookies[0].http_only is True
    public = loaded.public()
    assert public["session_ref"] == "self"
    assert public["cookie_count"] == 1
    assert "synthetic-session" not in json.dumps(public)


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_clear_and_rebind_reject_late_writer(tmp_path):
    store = TimetableStore(tmp_path)
    original = session()
    store.replace(original)
    old_generation = store.generation()
    store.clear()
    with pytest.raises(TimetableError) as caught:
        store.replace(original, expected_generation=old_generation)
    assert caught.value.code == "auth_required"
    with pytest.raises(TimetableError) as caught:
        store.finish(original.revision, old_generation, "stored")
    assert caught.value.code == "auth_required"


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_finish_and_mark_expired_only_for_current_generation(tmp_path):
    store = TimetableStore(tmp_path)
    original = session()
    store.replace(original)
    generation = store.generation()
    store.mark_expired(original.revision, generation)
    assert store.load().state == "auth_expired"
    store.finish(original.revision, generation, "stored")
    assert store.load().state == "stored"
    store.clear()
    store.mark_expired(original.revision, generation)
    assert store.load() is None


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_candidate_roundtrip_and_validation(tmp_path):
    path = tmp_path / "candidate.dpapi"
    TimetableStore.write_candidate(path, [cookie()])
    loaded = TimetableStore.candidate(path)
    assert loaded[0].name == "eai-sess"
    with pytest.raises(TimetableError) as caught:
        TimetableStore.candidate(tmp_path / "missing.dpapi")
    assert caught.value.code == "invalid_input"
    with pytest.raises(TimetableError):
        TimetableStore.candidate(path.with_suffix(".json"))


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_oversized_session_and_generation_fail_closed(tmp_path):
    store = TimetableStore(tmp_path)
    store.path.write_bytes(b"x" * (MAX_SESSION_BYTES + 1))
    with pytest.raises(TimetableError) as caught:
        store.load()
    assert caught.value.code == "local_error"
    store.path.unlink()
    store.generation_path.write_text("1" * 100, encoding="ascii")
    with pytest.raises(TimetableError) as caught:
        store.snapshot()
    assert caught.value.code == "local_error"


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_oversized_candidate_is_rejected_before_decrypt(tmp_path):
    path = tmp_path / "large.dpapi"
    path.write_bytes(b"x" * (MAX_CANDIDATE_BYTES + 1))
    with pytest.raises(TimetableError) as caught:
        TimetableStore.candidate(path)
    assert caught.value.code == "invalid_input"


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_store_does_not_touch_other_system_files(tmp_path):
    other = {
        "library-self.dpapi": b"library",
        "jhzf-self.dpapi": b"jhzf",
        "shiguang-self.dpapi": b"shiguang",
    }
    for name, value in other.items():
        (tmp_path / name).write_bytes(value)
    store = TimetableStore(tmp_path)
    store.replace(session())
    store.clear()
    for name, value in other.items():
        assert (tmp_path / name).read_bytes() == value


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI is Windows-only")
def test_store_lock_contention_is_bounded(tmp_path):
    first = TimetableStore(tmp_path, lock_timeout=1.0)
    second = TimetableStore(tmp_path, lock_timeout=0.05)
    with first.locked():
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(second.load)
            with pytest.raises(TimetableError) as caught:
                future.result(timeout=3)
    assert caught.value.code == "local_error"


def test_session_rejects_duplicate_cookie_names():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TimetableSession(cookies=[cookie(), cookie()], state="stored")


def test_cookie_domain_is_limited_to_upc():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TimetableCookie(
            name="eai-sess",
            value=SecretStr("x"),
            domain="example.com",
        )
