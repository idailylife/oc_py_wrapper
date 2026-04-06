"""Integration test: verify ``RunConfig.instructions`` is actually loaded by opencode.

Two complementary strategies:

Strategy A — "probe keyword":
  The instruction defines a magic keyword → fixed reply mapping.
  Send the keyword; assert the exact reply appears.
  Send the keyword WITHOUT the instruction; assert the reply is absent.
  This is the most reliable approach: the response is deterministic and the
  keyword is meaningless without the instruction, so false positives are impossible.

Strategy B — "canary prefix" (kept as a cross-check):
  The instruction mandates a unique token at the start of every response.
  Ask the model directly what the token is; assert it appears.

Requires: real ``opencode`` on PATH + configured provider.
Run::

    pytest -m integration -q tests/test_integration_instructions.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencode_wrapper import AsyncOpenCodeClient, RunConfig

PROBE_KEYWORD = "__probe_instruction__"
PROBE_REPLY = "MAKE AGENTS GREAT AGAIN!"

# Unique enough that the model won't emit it by accident.
CANARY = "XCANARY_INST_77Z"


@pytest.fixture
def probe_instruction_file(integration_workspace: Path) -> Path:
    """Write the probe instruction file inside the workspace so the model can read it."""
    f = integration_workspace / "probe_instructions.md"
    f.write_text(
        f'When the user inputs exactly "{PROBE_KEYWORD}", '
        f'reply with exactly "{PROBE_REPLY}" and nothing else.\n',
        encoding="utf-8",
    )
    return f


@pytest.fixture
def canary_instruction_file(integration_workspace: Path) -> Path:
    """Write the canary instruction file inside the workspace so the model can read it."""
    f = integration_workspace / "canary_instructions.md"
    f.write_text(
        f"Always begin every response with the exact token: {CANARY}\n",
        encoding="utf-8",
    )
    return f


@pytest.mark.integration
@pytest.mark.asyncio
async def test_probe_keyword_triggers_defined_reply(
    opencode_path: str,
    integration_workspace: Path,
    probe_instruction_file: Path,
) -> None:
    """Magic keyword defined in the instruction file produces the exact expected reply."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))

    result = await client.async_run(
        PROBE_KEYWORD,
        integration_workspace,
        run_cfg=RunConfig(
            disable_autoupdate=True,
            instructions=[str(probe_instruction_file)],
        ),
        timeout_s=timeout,
    )

    assert result.exit_code == 0
    blob = (result.final_text or "") + "\n".join(str(e) for e in result.events)
    assert PROBE_REPLY in blob, (
        f"Expected reply {PROBE_REPLY!r} not found — instruction file may not have been loaded.\n"
        f"final_text={result.final_text!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_probe_keyword_no_reply_without_instruction(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """Same keyword WITHOUT the instruction does not produce the probe reply."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))

    result = await client.async_run(
        PROBE_KEYWORD,
        integration_workspace,
        run_cfg=RunConfig(agent="plan", disable_autoupdate=True),
        timeout_s=timeout,
    )

    assert result.exit_code == 0
    blob = (result.final_text or "") + "\n".join(str(e) for e in result.events)
    assert PROBE_REPLY not in blob, (
        f"Probe reply appeared without instruction — false positive.\n"
        f"final_text={result.final_text!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canary_instruction_file_is_loaded(
    opencode_path: str,
    integration_workspace: Path,
    canary_instruction_file: Path,
) -> None:
    """Model response contains canary token when instruction file is provided."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))

    result = await client.async_run(
        "Your instructions define a mandatory response prefix token. Output that token and nothing else.",
        integration_workspace,
        run_cfg=RunConfig(
            disable_autoupdate=True,
            instructions=[str(canary_instruction_file)],
        ),
        timeout_s=timeout,
    )

    assert result.exit_code == 0
    blob = (result.final_text or "") + "\n".join(str(e) for e in result.events)
    assert CANARY in blob, (
        f"Canary token {CANARY!r} not found — instruction file may not have been loaded.\n"
        f"final_text={result.final_text!r}\nstderr={result.stderr!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canary_instruction_absent_without_field(
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """Same prompt without instruction file does NOT produce the canary token."""
    client = AsyncOpenCodeClient(binary=opencode_path)
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))

    result = await client.async_run(
        "Your instructions define a mandatory response prefix token. Output that token and nothing else.",
        integration_workspace,
        run_cfg=RunConfig(agent="plan", disable_autoupdate=True),
        timeout_s=timeout,
    )

    assert result.exit_code == 0
    blob = (result.final_text or "") + "\n".join(str(e) for e in result.events)
    assert CANARY not in blob, (
        f"Canary token appeared without instruction — something is wrong.\n"
        f"final_text={result.final_text!r}"
    )
