"""Post-benchmark analyzer — synthesizes auto grading + human feedback via LLM."""

from __future__ import annotations

import json
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig, run_result_fuzzy_text
from opencode_wrapper.errors import OpenCodeError

from benchmark.models import BenchmarkReport

_ANALYZER_PROMPT_DIR = Path(__file__).parent / "agents"


def _build_analysis_prompt(report: BenchmarkReport) -> str:
    """Build the prompt for the analyzer agent."""
    report_json = json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
    return (
        f"Analyze this benchmark report and provide structured observations.\n\n"
        f"## Benchmark Report\n```json\n{report_json}\n```\n\n"
        f"Respond with the JSON analysis object only."
    )


async def analyze(
    client: AsyncOpenCodeClient,
    report: BenchmarkReport,
    *,
    analyzer_workspace: str = ".",
    analyzer_cfg: RunConfig | None = None,
    timeout_s: float | None = 120.0,
) -> dict:
    """
    Run the analyzer agent on a benchmark report.

    Returns the parsed analysis dict with observations, recommendations, verdict.
    """
    cfg = analyzer_cfg or RunConfig()
    analyzer_md = _ANALYZER_PROMPT_DIR / "analyzer.md"
    if analyzer_md.exists():
        cfg = RunConfig(
            **{
                **cfg.__dict__,
                "files": (*cfg.files, str(analyzer_md)),
            }
        )

    prompt = _build_analysis_prompt(report)

    try:
        result = await client.async_run(
            prompt, analyzer_workspace, run_cfg=cfg, timeout_s=timeout_s,
        )
        text = run_result_fuzzy_text(result)
    except OpenCodeError as exc:
        return {"error": str(exc)}

    try:
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            clean = "\n".join(lines)
        return json.loads(clean)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse analyzer JSON", "raw": text[:500]}
