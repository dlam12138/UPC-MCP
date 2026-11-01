"""Public release boundaries and password-free client configuration."""

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_audit", Path(__file__).resolve().parents[1] / "scripts/audit_release.py"
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


@pytest.mark.parametrize("name", [
    ".local/credential.dpapi", "docs/research/raw.json", ".superdesign/resume.json",
    "state/session.json", "examples/mcp-config.json", "nested/__pycache__/x.pyc",
])
def test_private_release_paths_are_rejected(name):
    assert any(issue["rule"] == "excluded_local_content"
               for issue in AUDIT.inspect_file(name, b"{}"))


def test_audit_redacts_findings_instead_of_printing_secret():
    token = "gh" + "p_" + "A" * 30
    result = AUDIT.inspect_file("src/example.py", token.encode())
    assert result[0]["rule"] == "github_token"
    assert token not in json.dumps(result)
    assert AUDIT.inspect_file("tests/synthetic.py", b'password = "synthetic-password"') == []


def test_source_archive_prefix_is_stripped_but_traversal_is_not(tmp_path):
    normal = tmp_path / "source.zip"
    with zipfile.ZipFile(normal, "w") as archive:
        archive.writestr("UPC-MCP-v0.1.0/README.md", "public")
    assert AUDIT.archive_entries(normal) == [("README.md", b"public")]
    traversal = tmp_path / "bad.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../private.json", "{}")
    name, data = AUDIT.archive_entries(traversal)[0]
    assert AUDIT.inspect_file(name, data)


def test_mcp_config_cli_is_portable_local_only_and_does_not_create_credentials(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "upc_mcp", "--state-dir", str(tmp_path), "mcp-config"],
        capture_output=True, encoding="utf-8", check=True,
    )
    config = json.loads(result.stdout)["mcpServers"]["upc"]
    assert Path(config["command"]).is_absolute()
    assert config["args"][-3:] == ["--state-dir", str(tmp_path), "serve"]
    assert "password" not in result.stdout.lower()
    assert "env" not in config
    assert list(tmp_path.iterdir()) == []
