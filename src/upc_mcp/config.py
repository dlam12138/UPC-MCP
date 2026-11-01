"""Shared local paths without importing any campus-system implementation."""

import os
from pathlib import Path


def default_state_dir() -> Path:
    configured = os.environ.get("UPC_MCP_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UPC-MCP"
