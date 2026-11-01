"""Local hidden-input lifecycle and encrypted research migration, no network."""

import json
from types import SimpleNamespace

import pytest

from upc_mcp.auto_auth.coordinator import create_auto_auth
from upc_mcp.auto_auth.credentials import _protect, account_hash
from upc_mcp.auto_auth.errors import AutoAuthError
from upc_mcp.cli import execute_credential, parser


def test_configure_uses_hidden_local_input_and_clear_invalidates_binding(tmp_path, monkeypatch):
    inputs = iter(["synthetic-user", "synthetic-password", "synthetic-password"])
    monkeypatch.setattr("upc_mcp.cli.getpass.getpass", lambda _prompt: next(inputs))
    args = parser().parse_args(["credential", "configure"])
    result = execute_credential(args, tmp_path)
    assert result["configured"]
    auto = create_auto_auth(tmp_path)
    old = auto.binding()
    assert auto.credentials.load().username == "synthetic-user"
    encrypted = auto.credentials.path.read_bytes()
    assert b"synthetic-password" not in encrypted
    clear = execute_credential(parser().parse_args(["credential", "clear"]), tmp_path)
    assert clear["generation"] > old.generation
    assert not auto.binding().configured
    assert not auto.cas_store.path.exists()


def test_existing_credential_requires_explicit_local_rebind(tmp_path, monkeypatch):
    auto = create_auto_auth(tmp_path)
    auto.credentials.store("synthetic-user", "synthetic-password")

    def no_prompt(_prompt):
        raise AssertionError("Must reject before prompting for secrets")

    monkeypatch.setattr("upc_mcp.cli.getpass.getpass", no_prompt)
    with pytest.raises(AutoAuthError) as error:
        execute_credential(parser().parse_args(["credential", "configure"]), tmp_path)
    assert error.value.code == "already_configured"


def test_late_configure_cannot_overwrite_clear(tmp_path):
    auto = create_auto_auth(tmp_path)
    before_input = auto.binding()
    auto.clear_credential()
    with pytest.raises(AutoAuthError) as error:
        auto.configure("synthetic-user", "synthetic-password", expected=before_input)
    assert error.value.code == "credential_changed"
    assert not auto.binding().configured


def test_research_migration_preserves_source_and_refuses_overwrite(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    source = research / "credential.dpapi"
    source.write_bytes(_protect(json.dumps({"version": 1, "username": "synthetic-user",
                                           "password": "synthetic-password"}).encode()))
    original = source.read_bytes()
    checkpoint = research / "cas-session.dpapi"
    checkpoint.write_bytes(_protect(json.dumps({
        "version": 1, "account_hash": account_hash("synthetic-user"),
        "cookies": [{"name": "CASTGC", "value": "synthetic-cookie",
                     "domain": "cas.upc.edu.cn", "path": "/cas/", "secure": True}],
    }).encode()))
    args = parser().parse_args(["credential", "migrate", "--source", str(source),
                               "--cas-source", str(checkpoint)])
    output = execute_credential(args, tmp_path / "state")
    assert output["credential_adopted"] and output["cas_checkpoint_adopted"]
    assert source.read_bytes() == original
    with pytest.raises(AutoAuthError) as error:
        execute_credential(args, tmp_path / "state")
    assert error.value.code == "already_configured"


def test_checkpoint_adopt_after_clear_does_not_recreate_old_identity(tmp_path):
    auto = create_auto_auth(tmp_path)
    binding = auto.credentials.store("synthetic-user", "synthetic-password")
    auto.clear_credential()
    with pytest.raises(AutoAuthError) as error:
        auto.adopt_checkpoint(tmp_path / "absent.dpapi", binding)
    assert error.value.code == "credential_changed"
    assert not auto.cas_store.path.exists()


@pytest.mark.parametrize("action", ["configure", "adopt", "status"])
def test_no_cli_password_argument_or_environment_contract(action):
    with pytest.raises(SystemExit):
        parser().parse_args(["credential", action, "--password", "synthetic-secret"])


def test_cli_check_dispatch_injects_auth_without_requiring_three_browser_logins(tmp_path,
                                                                            monkeypatch):
    import asyncio

    from upc_mcp.cli import execute_library, execute_shiguang
    from upc_mcp.library_service import LibraryService
    from upc_mcp.shiguang_service import ShiguangService
    from upc_mcp.shiguang_session import ShiguangStore
    from upc_mcp.timetable_cli import execute_timetable
    from upc_mcp.timetable_service import TimetableService

    create_auto_auth(tmp_path).credentials.store("synthetic-user", "synthetic-password")

    async def check(service):
        assert service.auto_auth is not None
        assert service.auto_auth.is_configured()
        return {"status": "ok"}

    monkeypatch.setattr(ShiguangService, "check", check)
    monkeypatch.setattr(LibraryService, "check", check)
    monkeypatch.setattr(TimetableService, "check", check)

    async def scenario():
        assert await execute_library(SimpleNamespace(library_action="check"), tmp_path)
        assert await execute_shiguang(SimpleNamespace(shiguang_action="check"),
                                      ShiguangStore(tmp_path))
        assert await execute_timetable(SimpleNamespace(timetable_action="check"), tmp_path)

    asyncio.run(scenario())
