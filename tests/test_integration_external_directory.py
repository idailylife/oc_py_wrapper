"""
Integration: ``permission.external_directory`` allows reading paths outside the workspace cwd.

Requires real ``opencode`` and provider auth (same as other ``integration`` tests).

See https://opencode.ai/docs/permissions/#external-directories
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opencode_wrapper import AsyncOpenCodeClient, RunConfig


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_read_file_outside_workspace_with_external_directory_allow(
    opencode_path: str,
    integration_workspace: Path,
    tmp_path: Path,
) -> None:
    """Workspace is ``oc_ws``; secret file lives in a sibling dir — allowed via ``external_directory``."""
    outside = (tmp_path / "outside_repo").resolve()
    outside.mkdir(parents=True, exist_ok=True)
    marker = "EXTERNAL_DIR_READ_OK_9e4c2a1f"
    secret_file = (outside / "payload.txt").resolve()
    secret_file.write_text(marker, encoding="utf-8")

    # Pattern must cover files under ``outside`` (OpenCode uses wildcard rules).
    outside_pattern = f"{outside.as_posix()}/**"

    client = AsyncOpenCodeClient(binary=opencode_path)
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))
    prompt = (
        "Use the read tool (or equivalent file read) on this absolute path, then reply with "
        "ONLY the file's exact text content and nothing else — no quotes, no markdown, no explanation:\n"
        f"{secret_file.as_posix()}"
    )
    result = await client.async_run(
        prompt,
        integration_workspace,
        run_cfg=RunConfig(
            agent="plan",
            disable_autoupdate=True,
            permission={
                "external_directory": {
                    outside_pattern: "allow",
                },
            },
        ),
        timeout_s=timeout,
    )
    assert result.exit_code == 0, (
        f"expected success; stderr={result.stderr!r} final_text={result.final_text!r}"
    )
    blob = (result.final_text or "") + "\n".join(str(e) for e in result.events)
    assert marker in blob, (
        f"expected file marker in output; final_text={result.final_text!r} stderr={result.stderr!r}"
    )
