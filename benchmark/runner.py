"""Benchmark runner — orchestrates A/B eval runs via AsyncOpenCodeClient."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig, RunResult, run_result_fuzzy_text
from opencode_wrapper.errors import OpenCodeError

from benchmark.models import EvalCase, EvalSuite, RunOutput


async def _timed_run(
    client: AsyncOpenCodeClient,
    prompt: str,
    workspace: str,
    run_cfg: RunConfig,
    timeout_s: float | None,
) -> tuple[RunResult | None, float, str | None]:
    """Run once, return (result, duration_seconds, error_message)."""
    t0 = time.monotonic()
    try:
        result = await client.async_run(
            prompt, workspace, run_cfg=run_cfg, timeout_s=timeout_s,
        )
        return result, time.monotonic() - t0, None
    except OpenCodeError as exc:
        return None, time.monotonic() - t0, str(exc)


async def run_single_eval(
    client: AsyncOpenCodeClient,
    case: EvalCase,
    config_name: str,
    run_cfg: RunConfig,
    *,
    workspace: str = ".",
    timeout_s: float | None = 300.0,
) -> RunOutput:
    """Run a single eval case with a given config, return RunOutput."""
    ws = case.workspace if case.workspace != "." else workspace
    prompt = case.prompt

    result, duration, error = await _timed_run(
        client, prompt, ws, run_cfg, timeout_s,
    )

    if result is not None:
        return RunOutput(
            config_name=config_name,
            eval_id=case.id,
            final_text=run_result_fuzzy_text(result),
            tool_calls=result.tool_calls,
            events=result.events,
            exit_code=result.exit_code,
            stderr=result.stderr,
            duration_s=duration,
        )
    return RunOutput(
        config_name=config_name,
        eval_id=case.id,
        duration_s=duration,
        error=error,
    )


async def run_eval_pair(
    client: AsyncOpenCodeClient,
    case: EvalCase,
    configs: dict[str, RunConfig],
    *,
    workspace: str = ".",
    timeout_s: float | None = 300.0,
) -> dict[str, RunOutput]:
    """Run one eval case against all configs in parallel."""
    tasks = {
        name: run_single_eval(
            client, case, name, cfg,
            workspace=workspace, timeout_s=timeout_s,
        )
        for name, cfg in configs.items()
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    outputs: dict[str, RunOutput] = {}
    for name, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            outputs[name] = RunOutput(
                config_name=name, eval_id=case.id, error=str(res),
            )
        else:
            outputs[name] = res
    return outputs


async def run_benchmark(
    suite: EvalSuite,
    configs: dict[str, RunConfig],
    *,
    workspace: str = ".",
    timeout_s: float | None = 300.0,
    output_dir: str | Path = "benchmark_output",
    binary: str = "opencode",
    concurrency: int = 2,
) -> Path:
    """
    Run a full benchmark suite.

    For each eval case, runs all configs in parallel.
    Eval cases are run with bounded concurrency.
    Results are written to output_dir with the standard directory layout.

    Returns the output directory path.
    """
    client = AsyncOpenCodeClient(binary=binary)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Save suite metadata
    suite.save(out / "evals.json")

    semaphore = asyncio.Semaphore(concurrency)

    async def _run_case(case: EvalCase) -> dict[str, RunOutput]:
        async with semaphore:
            return await run_eval_pair(
                client, case, configs,
                workspace=workspace, timeout_s=timeout_s,
            )

    tasks = [_run_case(case) for case in suite.evals]
    all_results = await asyncio.gather(*tasks)

    # Write outputs to disk
    for case, pair_outputs in zip(suite.evals, all_results):
        case_dir = out / f"eval-{case.id}"
        for config_name, run_output in pair_outputs.items():
            cfg_dir = case_dir / config_name
            cfg_dir.mkdir(parents=True, exist_ok=True)
            run_output.save(cfg_dir / "output.json")

    return out
