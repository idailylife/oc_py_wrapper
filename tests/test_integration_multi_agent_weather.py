"""
Multi-agent style integration: 10 parallel OpenCode runs (one “agent” per city weather),
then one summarizer run.

**Heavy**: 11 real ``opencode run`` invocations (API + optional web tools). Not run unless::

    OPENCODE_MULTI_AGENT_WEATHER=1

Also requires the same preconditions as other integration tests (``opencode`` on PATH,
configured provider). Run::

    OPENCODE_MULTI_AGENT_WEATHER=1 pytest -m integration tests/test_integration_multi_agent_weather.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from opencode_wrapper import AsyncOpenCodeClient, RunConfig, run_result_fuzzy_text

# 10 个国内主要城市（直辖市 / 省会或一线）
CITIES_CN = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "成都",
    "武汉",
    "西安",
    "南京",
    "重庆",
)


def _multi_agent_weather_enabled() -> bool:
    return os.environ.get("OPENCODE_MULTI_AGENT_WEATHER", "").lower() in (
        "1",
        "true",
        "yes",
    )


@pytest.fixture
def require_multi_agent_weather() -> None:
    if not _multi_agent_weather_enabled():
        pytest.skip(
            "Set OPENCODE_MULTI_AGENT_WEATHER=1 to run this 11-call weather workflow test."
        )


@pytest.mark.integration
@pytest.mark.multi_agent_weather
@pytest.mark.asyncio
async def test_integration_ten_cities_weather_then_summarize(
    require_multi_agent_weather: None,
    opencode_path: str,
    integration_workspace: Path,
) -> None:
    """
    10 次并行 ``async_run``：各查一城天气；第 11 次 ``plan`` 汇总中文结论。
    """
    client = AsyncOpenCodeClient(binary=opencode_path)
    base_timeout = float(os.environ.get("OPENCODE_INTEGRATION_TIMEOUT_S", "300"))
    per_city_timeout = float(
        os.environ.get("OPENCODE_WEATHER_PER_CITY_TIMEOUT_S", str(base_timeout))
    )
    summary_timeout = float(
        os.environ.get("OPENCODE_WEATHER_SUMMARY_TIMEOUT_S", str(base_timeout))
    )

    # 尝试开启 Exa 网页检索（OpenCode 文档：truthy OPENCODE_ENABLE_EXA）；无则仍可能仅靠模型知识
    extra_env: dict[str, str] = {
        "OPENCODE_ENABLE_EXA": os.environ.get("OPENCODE_ENABLE_EXA", "1"),
    }

    worker_cfg = RunConfig(
        agent="general",
        disable_autoupdate=True,
        permission={
            "websearch": "allow",
            "webfetch": "allow",
            "bash": "deny",
            "edit": "deny",
        },
        extra_env=extra_env,
    )
    summary_cfg = RunConfig(
        agent="plan",
        disable_autoupdate=True,
        permission={
            "websearch": "deny",
            "webfetch": "deny",
            "bash": "deny",
            "edit": "deny",
        },
    )

    async def weather_for_city(city: str):
        prompt = (
            f"请用中文查询或检索「{city}」今天的主要天气情况"
            f"（气温范围、晴雨、如有风力可简述）。控制在 4 句以内，只输出与天气相关的内容。"
        )
        return city, await client.async_run(
            prompt,
            integration_workspace,
            run_cfg=worker_cfg,
            timeout_s=per_city_timeout,
        )

    if os.environ.get("OPENCODE_WEATHER_SEQUENTIAL", "").lower() in ("1", "true", "yes"):
        pairs = []
        for c in CITIES_CN:
            pairs.append(await weather_for_city(c))
    else:
        pairs = list(
            await asyncio.gather(*[weather_for_city(c) for c in CITIES_CN])
        )

    city_results = dict(pairs)
    assert len(city_results) == len(CITIES_CN)

    for city in CITIES_CN:
        r = city_results[city]
        assert r.exit_code == 0, f"{city}: exit {r.exit_code} stderr={r.stderr!r}"
        text = run_result_fuzzy_text(r)
        assert len(text) >= 8, (
            f"{city}: expected non-trivial answer, got fuzzy={text!r} "
            f"final_text={r.final_text!r} events={len(r.events)}"
        )

    bundle_lines = [
        f"【{c}】{run_result_fuzzy_text(city_results[c])}" for c in CITIES_CN
    ]
    bundle = "\n\n".join(bundle_lines)

    summary_prompt = (
        "你是一位气象摘要助手。下面给出了 10 个中国主要城市各自的天气要点（每城一段）。\n"
        "请用中文输出：\n"
        "1）一段总体概述（5 句以内）；\n"
        "2）然后按城市列出一句结论（必须包含该城市名称）。\n\n"
        f"{bundle}"
    )

    summary = await client.async_run(
        summary_prompt,
        integration_workspace,
        run_cfg=summary_cfg,
        timeout_s=summary_timeout,
    )
    assert summary.exit_code == 0
    summary_text = run_result_fuzzy_text(summary)
    assert len(summary_text) >= 80, f"summary too short: {summary_text!r}"

    # 摘要里应体现多数城市名（允许模型用简称，至少命中若干）
    hits = sum(1 for c in CITIES_CN if c in summary_text)
    assert hits >= 5, (
        f"expected summary to mention several city names; got {hits}/10, text={summary_text[:500]!r}..."
    )
