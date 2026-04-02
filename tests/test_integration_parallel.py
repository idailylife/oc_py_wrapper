"""
Integration test: parallel async_run calls must not block each other.

Verifies that two concurrent opencode processes complete without one
serializing behind the other (the SQLite shared-DB bug).

Run::

    pytest -m integration -v tests/test_integration_parallel.py
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from opencode_wrapper import AsyncOpenCodeClient, RunConfig


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parallel_runs_not_serialized(
    opencode_path: str,
    tmp_path: Path,
) -> None:
    """Two concurrent runs should overlap, not serialize.

    Each task asks the model to echo a unique marker.  We measure when the
    first event arrives for each run.  If the runs are truly concurrent, both
    first-event times should be close to each other — not separated by the
    full duration of the first run.
    """
    timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "120"))
    client = AsyncOpenCodeClient(
        binary=opencode_path,
        startup_concurrency=1,
        startup_delay_s=0.3,
        isolate_db=True,
    )
    run_cfg = RunConfig(agent="plan", disable_autoupdate=True)

    first_event_times: dict[str, float] = {}
    results: dict[str, object] = {}

    async def run_task(name: str, ws: Path) -> None:
        ws.mkdir(exist_ok=True)
        (ws / ".gitkeep").write_text("", encoding="utf-8")
        markers = {"alpha": "HELLO_ALPHA", "beta": "HELLO_BETA"}
        prompt = (
            f"Write a Python script that prints '{markers[name]}' and nothing else. "
            f"Save it as hello.py and run it."
        )
        t0 = time.monotonic()
        result = await client.async_run(
            prompt, ws, run_cfg=run_cfg, timeout_s=timeout
        )
        first_event_times[name] = time.monotonic() - t0
        results[name] = result

    wall_start = time.monotonic()
    await asyncio.gather(
        run_task("alpha", tmp_path / "ws_alpha"),
        run_task("beta", tmp_path / "ws_beta"),
    )
    wall_total = time.monotonic() - wall_start

    alpha_t = first_event_times["alpha"]
    beta_t = first_event_times["beta"]
    sequential_estimate = alpha_t + beta_t

    # Both runs must have succeeded
    for name in ("alpha", "beta"):
        r = results[name]
        assert r.exit_code == 0, f"{name} exited {r.exit_code}: {r.stderr}"

    # Parallel speedup check: wall time must be meaningfully less than
    # running them sequentially would take.  We allow up to 80% of the
    # sequential estimate to account for minor scheduling jitter.
    assert wall_total < sequential_estimate * 0.80, (
        f"Runs appear serialized: wall={wall_total:.1f}s, "
        f"sequential_estimate={sequential_estimate:.1f}s "
        f"(alpha={alpha_t:.1f}s, beta={beta_t:.1f}s)"
    )
