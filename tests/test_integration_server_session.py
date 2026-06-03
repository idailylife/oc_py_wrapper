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

# The `question` tool is gated on OPENCODE_CLIENT (default "cli" enables it);
# set this flag too so the tool is available regardless of how the server reads
# the client identity.
_QUESTION_ENV = {"OPENCODE_ENABLE_QUESTION_TOOL": "1"}


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
async def test_session_question_callback_answers(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """The model's ``question`` tool pauses; ``on_question`` answers; the run continues."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    asked: list[dict[str, Any]] = []

    async def answer(props: dict[str, Any]) -> list[list[str]]:
        asked.append(props)
        # Answer each asked question by picking its first option's label.
        out: list[list[str]] = []
        for q in props.get("questions", []):
            opts = q.get("options") or []
            out.append([opts[0]["label"]] if opts else ["yes"])
        return out

    cfg = RunConfig(model=_MODEL, extra_env=_QUESTION_ENV)
    async with OpenCodeSession(
        client, integration_workspace, run_cfg=cfg, on_question=answer, timeout_s=_timeout()
    ) as s:
        await s.send(
            "Use the `question` tool to ask me whether you should proceed, with options "
            "'Yes' and 'No'. Do not do anything else until I answer."
        )

    assert asked, "expected at least one question.asked event"
    assert asked[0].get("questions"), "question.asked should carry the questions payload"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_default_rejects_permission(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """With no callback, a ``permission.asked`` is auto-rejected — the turn still completes.

    The contract under test is "no hang": a bash-gated prompt with no
    ``on_permission`` must return (the default reject unblocks the turn) rather
    than block until timeout. ``send`` wraps the turn in ``asyncio.wait_for``, so
    returning at all proves the turn was not stuck waiting on the prompt. We do
    NOT string-match the command output here — the model tends to echo the
    prompt's literal command text back, which would false-positive.
    """
    client = AsyncOpenCodeClient(binary=opencode_path)
    cfg = RunConfig(model=_MODEL, permission={"bash": "ask"})
    async with OpenCodeSession(
        client, integration_workspace, run_cfg=cfg, timeout_s=_timeout()
    ) as s:
        r = await s.send("Run the shell command `echo CANARY_OUTPUT` and report its output.")
    # No bash tool call reached a completed state with real output (it was rejected).
    completed_bash = [
        t
        for t in r.tool_calls
        if t.get("tool") == "bash"
        and isinstance(t.get("state"), dict)
        and t["state"].get("status") == "completed"
    ]
    assert not completed_bash, f"bash should have been rejected, not completed: {completed_bash}"
