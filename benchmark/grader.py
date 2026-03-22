"""Auto-grader — uses an opencode LLM call to grade run outputs against assertions."""

from __future__ import annotations

import json
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig, run_result_fuzzy_text
from opencode_wrapper.errors import OpenCodeError

from benchmark.models import EvalCase, GradingResult, RunOutput

_GRADER_PROMPT_DIR = Path(__file__).parent / "agents"


def _build_grading_prompt(case: EvalCase, output: RunOutput) -> str:
    """Construct the prompt sent to the grader LLM."""
    assertions_block = "\n".join(f"- {a}" for a in case.assertions)
    return (
        f"## Prompt\n{case.prompt}\n\n"
        f"## Expected Output\n{case.expected_output}\n\n"
        f"## Assertions\n{assertions_block}\n\n"
        f"## Assistant Output\n{output.final_text}\n\n"
        f"## Tool Calls\n{json.dumps(output.tool_calls, indent=2, ensure_ascii=False)}\n\n"
        f"Grade this output against the assertions above. "
        f"Respond with the JSON grading object only."
    )


async def grade_output(
    client: AsyncOpenCodeClient,
    case: EvalCase,
    output: RunOutput,
    *,
    grader_workspace: str = ".",
    grader_cfg: RunConfig | None = None,
    timeout_s: float | None = 120.0,
) -> GradingResult:
    """
    Grade a single run output using an LLM judge.

    Sends the eval prompt, output, and assertions to the grader agent,
    then parses the structured JSON response into a GradingResult.
    """
    if not case.assertions:
        return GradingResult(
            eval_id=case.id,
            config_name=output.config_name,
            pass_rate=1.0,
            summary="No assertions to evaluate.",
        )

    cfg = grader_cfg or RunConfig()
    # Inject the grader agent prompt as a file
    grader_md = _GRADER_PROMPT_DIR / "grader.md"
    if grader_md.exists():
        cfg = RunConfig(
            **{
                **cfg.__dict__,
                "files": (*cfg.files, str(grader_md)),
            }
        )

    prompt = _build_grading_prompt(case, output)

    try:
        result = await client.async_run(
            prompt, grader_workspace, run_cfg=cfg, timeout_s=timeout_s,
        )
        text = run_result_fuzzy_text(result)
    except OpenCodeError as exc:
        return GradingResult(
            eval_id=case.id,
            config_name=output.config_name,
            summary=f"Grader error: {exc}",
        )

    # Parse JSON from grader response
    try:
        # Strip markdown fences if present
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            clean = "\n".join(lines)
        data = json.loads(clean)
    except json.JSONDecodeError:
        return GradingResult(
            eval_id=case.id,
            config_name=output.config_name,
            summary=f"Failed to parse grader JSON: {text[:200]}",
        )

    return GradingResult(
        eval_id=case.id,
        config_name=output.config_name,
        expectations=data.get("expectations", []),
        pass_rate=data.get("pass_rate", 0.0),
        summary=data.get("summary", ""),
    )


async def grade_eval_pair(
    client: AsyncOpenCodeClient,
    case: EvalCase,
    outputs: dict[str, RunOutput],
    *,
    grader_workspace: str = ".",
    grader_cfg: RunConfig | None = None,
    timeout_s: float | None = 120.0,
) -> dict[str, GradingResult]:
    """Grade all config outputs for one eval case."""
    import asyncio

    tasks = {
        name: grade_output(
            client, case, out,
            grader_workspace=grader_workspace,
            grader_cfg=grader_cfg,
            timeout_s=timeout_s,
        )
        for name, out in outputs.items()
    }
    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results))
