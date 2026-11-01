"""Audit tracked/public release files without reading any local credentials.

Reports rule names and locations only, never matched secret values. Independent
secret scanning is also used before publication; this is a content boundary,
not a claim that a regex can prove absence of every possible secret.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

BLOCKED_PARTS = {
    ".git", ".venv", ".local", ".superdesign", ".playwright-mcp", ".uv-cache",
    ".pytest_cache", ".ruff_cache", "__pycache__", "state", "private", "browser-profile",
}
BLOCKED_SUFFIXES = {".dpapi", ".har", ".saz", ".pyc", ".log"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".lock", ".txt", ".yml", ".yaml",
                 ".ps1", ".cmd", ".svg"}
RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,})"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{20,}"),
    "personal_windows_path": re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+[^\s<>/\\]+", re.I),
    "local_private_path": re.compile(
        r"[A-Za-z]:[/\\]+(?:UPC-MCP-private|ReverseProjects|ReverseTools|UPCpocket)(?:[/\\]|\b)",
        re.I,
    ),
    "session_reference": re.compile(r"(?:lody://session/|session://)[a-f0-9-]{30,}", re.I),
    "hardcoded_password": re.compile(
        r"(?:[\"']password[\"']|\bpassword)\s*[:=]\s*[\"']([^\"'\r\n]{12,})[\"']", re.I
    ),
}


def inspect_file(name: str, data: bytes) -> list[dict]:
    path = PurePosixPath(name.replace("\\", "/"))
    lower = path.as_posix().lower()
    issues = []
    if (path.is_absolute() or ".." in path.parts or any(p.lower() in BLOCKED_PARTS
            or p.lower().startswith(".pytest-tmp") for p in path.parts)
            or path.suffix.lower() in BLOCKED_SUFFIXES
            or lower.startswith(("docs/research/", "docs/implementation/", "docs/decisions/",
                                 "docs/protocols/"))
            or lower == "examples/mcp-config.json"
            or path.name.startswith(".env")):
        issues.append({"file": name, "rule": "excluded_local_content"})
    if len(data) > 10 * 1024 * 1024:
        issues.append({"file": name, "rule": "unexpected_large_file"})
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
        "LICENSE", ".gitignore", ".gitattributes",
    }:
        return issues
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [*issues, {"file": name, "rule": "non_utf8_text"}]
    for rule, pattern in RULES.items():
        for match in pattern.finditer(text):
            if rule == "hardcoded_password":
                value = match.group(1).lower()
                if any(word in value for word in ("synthetic", "demo", "test", "example")):
                    continue
            issues.append({
                "file": name, "rule": rule, "line": text.count("\n", 0, match.start()) + 1,
            })
    return issues


def archive_entries(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if any(item.file_size > 10 * 1024 * 1024 for item in archive.infolist()):
                raise ValueError("archive_entry_too_large")
            entries = [(item.filename, archive.read(item)) for item in archive.infolist()
                       if not item.is_dir()]
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            entries = []
            for item in archive.getmembers():
                if item.issym() or item.islnk():
                    raise ValueError("archive_symlink_not_allowed")
                if item.isfile():
                    if item.size > 10 * 1024 * 1024:
                        raise ValueError("archive_entry_too_large")
                    source = archive.extractfile(item)
                    assert source is not None
                    entries.append((item.name, source.read()))
    else:
        raise ValueError("unsupported_archive")
    # Source archives have one containing directory; wheels do not.
    if path.suffix != ".whl" and entries:
        parts = [PurePosixPath(name).parts for name, _data in entries]
        if all(len(item) > 1 and item[0] == parts[0][0] and ".." not in item
               and not PurePosixPath(*item).is_absolute() for item in parts):
            entries = [(PurePosixPath(*PurePosixPath(name).parts[1:]).as_posix(), data)
                       for name, data in entries]
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", action="append", type=Path, default=[])
    args = parser.parse_args()
    problems = []
    checked = 0
    if args.archive:
        for path in args.archive:
            for name, data in archive_entries(path):
                checked += 1
                problems.extend(inspect_file(name, data))
    else:
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                                check=True)
        names = [x for x in result.stdout.decode("utf-8").split("\0") if x]
        if not names:
            raise ValueError("no_tracked_files")
        for name in names:
            checked += 1
            problems.extend(inspect_file(name, (root / name).read_bytes()))
    print(json.dumps({"checked_files": checked, "issues": problems, "ok": not problems},
                     ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
