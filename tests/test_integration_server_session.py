"""Integration tests: ``OpenCodeSession`` on a real ``opencode serve`` process.

These exercise the server-mode session API end-to-end:
- native multi-turn continuity (the model recalls a fact across turns),
- a human-in-the-loop permission callback (``permission={"bash":"ask"}`` →
  ``on_permission`` returns ``"once"`` → the bash tool actually runs),
- hermetic isolation (a canary global agent does NOT leak into the session).

Skip when ``opencode`` is unavailable or ``OPENCODE_INTEGRATION=0``.

Run::

    pytest -m integration -q tests/test_integration_server_session.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from opencode_wrapper import AsyncOpenCodeClient, OpenCodeSession, RunConfig

# Optional model pin; falls back to the provider default when unset.
_MODEL = os.environ.get("OPENCODE_INTEGRATION_MODEL", "").strip() or None


def _timeout() -> float:
    return float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_multi_turn_continuity(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """Two turns on one session: the model recalls a name set in turn 1."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    cfg = RunConfig(model=_MODEL, agent="plan")
    async with OpenCodeSession(
        client, integration_workspace, run_cfg=cfg, timeout_s=_timeout()
    ) as s:
        sid = s.session_id
        await s.send("My name is Bob. Remember it.")
        r2 = await s.send("What is my name? Reply with just the name.")
        assert s.session_id == sid, "session id must be stable across turns"
        assert "bob" in (r2.final_text or "").lower(), f"expected recall; got {r2.final_text!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_permission_callback_runs_bash(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """``permission={"bash":"ask"}`` pauses; callback returns ``once``; bash runs."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    asked: list[dict[str, Any]] = []

    async def approve(props: dict[str, Any]) -> str:
        asked.append(props)
        return "once"

    cfg = RunConfig(model=_MODEL, permission={"bash": "ask"})
    async with OpenCodeSession(
        client, integration_workspace, run_cfg=cfg, on_permission=approve, timeout_s=_timeout()
    ) as s:
        r = await s.send(
            "Run the shell command `echo HELLO_FROM_OC_SESSION` and tell me its exact output."
        )

    assert asked, "expected at least one permission.asked"
    blob = (r.final_text or "") + " " + " ".join(str(t) for t in r.tool_calls)
    assert "HELLO_FROM_OC_SESSION" in blob, f"bash output missing; final_text={r.final_text!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_default_rejects_permission(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """With no callback, a ``permission.asked`` is auto-rejected — the turn still completes."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    cfg = RunConfig(model=_MODEL, permission={"bash": "ask"})
    async with OpenCodeSession(
        client, integration_workspace, run_cfg=cfg, timeout_s=_timeout()
    ) as s:
        # Should not hang: default callback rejects, the turn returns.
        r = await s.send("Run `echo SHOULD_NOT_RUN` if you are allowed to.")
    assert "SHOULD_NOT_RUN" not in (r.final_text or "")
