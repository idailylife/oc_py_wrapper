"""
Integration tests against a real ``opencode`` CLI (requires configured provider).

Skip when:
- ``opencode`` is not on ``PATH`` (override with ``OPENCODE_BINARY``), or
- ``OPENCODE_INTEGRATION=0`` (or ``false`` / ``no``).

Run::

    pytest -m integration -q

Or only this file::

    pytest tests/test_integration_opencode.py -q
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from opencode_wrapper import AsyncOpenCodeClient, RunConfig


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_async_run_minimal_prompt(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """End-to-end ``async_run``: JSON events, exit 0, some model output."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    prompt = (
        "You must reply with exactly this single line and nothing else: "
        "INTEGRATION_PONG"
    )
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))
    result = await client.async_run(
        prompt,
        integration_workspace,
        run_cfg=RunConfig(
            agent="plan",
            disable_autoupdate=True,
        ),
        timeout_s=timeout,
    )
    assert result.exit_code == 0
    assert result.events, "expected at least one JSON event on stdout"
    assert result.raw_stdout_lines, "expected raw stdout lines"
    blob = (result.final_text or "") + "\n".join(str(e) for e in result.events)
    assert "INTEGRATION_PONG" in blob.upper(), (
        f"expected marker in output; final_text={result.final_text!r} stderr={result.stderr!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_async_stream_yields_events(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """``async_stream`` yields parsed dicts before successful completion."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))
    seen: list[dict] = []

    async def collect() -> None:
        async for ev in client.async_stream(
            "Reply with exactly: STREAM_OK",
            integration_workspace,
            run_cfg=RunConfig(agent="plan", disable_autoupdate=True),
        ):
            assert isinstance(ev, dict)
            seen.append(ev)

    await asyncio.wait_for(collect(), timeout=timeout)

    assert seen, "expected at least one streamed event"
    assert any(
        not (e.get("type") == "diagnostic" and e.get("kind") == "empty_line")
        for e in seen
    ), f"only empty-line diagnostics: {seen[:5]}"
