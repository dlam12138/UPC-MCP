from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from upc_mcp.service_portal_protocol import ServicePortalError
from upc_mcp.service_portal_store import (
    ServicePortalCookie,
    ServicePortalSession,
    ServicePortalStore,
)


def cookie(name="PHPSESSID", value="synthetic-cookie-value", domain="service.upc.edu.cn"):
    return ServicePortalCookie(
        name=name,
        value=SecretStr(value),
        domain=domain,
        path="/",
        secure=True,
        http_only=True,
        host_only=True,
    )


def session(cookies=None, **overrides):
    data = {
        "cookies": cookies or [cookie()],
        "state": "stored",
        "source": "auto_auth_sso",
        "verified_at": "2026-11-01T11:00:00+08:00",
        "credential_generation": 1,
        "account_hash": "a" * 64,
    }
    data.update(overrides)
    return ServicePortalSession(**data)


def test_cookie_domain_is_strict_and_scope_is_preserved():
    with pytest.raises(ValidationError):
        cookie(domain=".upc.edu.cn")
    with pytest.raises(ValidationError):
        cookie(domain="cas.upc.edu.cn")
    public = session().public()
    assert public["identity_verified"] is False
    assert public["identity_stable"] is False
    assert public["cookie_count"] == 1
    assert public["cookie_names"] == ["PHPSESSID"]
    serialized = json.dumps(public, ensure_ascii=False)
    assert "synthetic-cookie-value" not in serialized
    assert "a" * 64 not in serialized


def test_store_roundtrip_generation_clear_and_stale_replace(tmp_path):
    store = ServicePortalStore(tmp_path)
    first = session()
    store.replace(first, expected_generation=0)
    assert store.generation() == 1
    loaded = store.load()
    assert loaded is not None
    assert loaded.cookies[0].host_only is True
    assert loaded.cookies[0].secure is True
    assert loaded.cookies[0].http_only is True
    with pytest.raises(ServicePortalError):
        store.replace(session(), expected_generation=0)
    store.clear()
    assert store.generation() == 2
    assert store.load() is None
    with pytest.raises(ServicePortalError) as exc:
        store.replace(session(), expected_generation=1)
    assert exc.value.code == "auth_required"


def test_finish_and_mark_expired_are_revision_conditional(tmp_path):
    store = ServicePortalStore(tmp_path)
    first = session()
    store.replace(first)
    store.mark_expired(first.revision)
    assert store.load().state == "auth_expired"
    with pytest.raises(ServicePortalError):
        store.finish("wrong-revision")
    store.finish(first.revision)
    assert store.load().state == "stored"


def test_corrupt_file_is_not_silently_reused(tmp_path):
    store = ServicePortalStore(tmp_path)
    store.path.write_bytes(b"not-dpapi")
    with pytest.raises(ServicePortalError) as exc:
        store.load()
    assert exc.value.code == "auth_required"
    assert exc.value.action == "service_portal_clear"
