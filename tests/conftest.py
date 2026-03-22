"""Shared pytest fixtures for integration tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


def integration_binary() -> str | None:
    """Resolved path to ``opencode`` when integration is allowed."""
    if os.environ.get("OPENCODE_INTEGRATION", "").lower() in ("0", "false", "no"):
        return None
    explicit = os.environ.get("OPENCODE_BINARY", "").strip()
    if explicit:
        p = os.path.expanduser(explicit)
        return p if os.path.isfile(p) else None
    return shutil.which("opencode")


@pytest.fixture
def opencode_path() -> str:
    path = integration_binary()
    if not path:
        pytest.skip(
            "Real opencode not available: install opencode and ensure it is on PATH, "
            "or set OPENCODE_BINARY. Set OPENCODE_INTEGRATION=0 to skip integration tests."
        )
    return path


@pytest.fixture
def integration_workspace(tmp_path: Path) -> Path:
    """Empty workspace; opencode resolves project config from cwd upward."""
    ws = tmp_path / "oc_ws"
    ws.mkdir()
    (ws / ".gitkeep").write_text("", encoding="utf-8")
    return ws
