"""
示例：3 路并行 ``opencode run`` 查各城天气，再 ``plan`` 汇总（共 4 次调用）。

需要本机已安装 ``opencode``、配置好模型。在项目根目录::

    PYTHONPATH=. python examples/multi_agent_weather.py
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig, run_result_fuzzy_text

CITIES_CN = (
    "北京",
    "上海",
    "广州",
)

TIMEOUT_PER_CITY_S = 300
TIMEOUT_SUMMARY_S = 300


def append_section(output_file: Path, title: str, body: str) -> None:
    with output_file.open("a", encoding="utf-8") as f:
        f.write(f"===== {title} =====\n")
        f.write(body.rstrip())
        f.write("\n\n")


def run_raw_output_text(result) -> str:
    raw_stdout = "".join(result.raw_stdout_lines).strip()
    stderr = (result.stderr or "").strip()
    if stderr:
        return f"{raw_stdout}\n\n--- STDERR ---\n{stderr}".strip()
    return raw_stdout


async def main_async(
    workspace: Path,
    *,
    binary: str,
    sequential: bool,
    output_file: Path,
) -> int:
    client = AsyncOpenCodeClient(binary=binary)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        (
            f"run_at={datetime.now().isoformat(timespec='seconds')}\n"
            f"workspace={workspace}\n"
            f"cities={','.join(CITIES_CN)}\n\n"
        ),
        encoding="utf-8",
    )

    worker_cfg = RunConfig(
        agent="general",
        disable_autoupdate=True,
        permission={
            "*": "allow",
        },
    )
    summary_cfg = RunConfig(
        agent="plan",
        disable_autoupdate=True,
        permission={
            "*": "deny",
        },
    )

    print_lock = asyncio.Lock()

    async def weather_for_city(city: str):
        prompt = (
            f"请用中文查询或检索「{city}」今天的主要天气情况"
            f"（气温范围、晴雨、如有风力可简述）。控制在 4 句以内，只输出与天气相关的内容。"
        )
        r = await client.async_run(
            prompt,
            workspace,
            run_cfg=worker_cfg,
            timeout_s=TIMEOUT_PER_CITY_S,
        )
        async with print_lock:
            if r.exit_code == 0:
                text = run_result_fuzzy_text(r)
                print(f"【{city}】\n{text}\n", flush=True)
                append_section(
                    output_file,
                    f"general/{city} (exit={r.exit_code})",
                    run_raw_output_text(r),
                )
            else:
                print(f"【{city}】exit {r.exit_code}", file=sys.stderr, flush=True)
                append_section(
                    output_file,
                    f"general/{city} (exit={r.exit_code})",
                    run_raw_output_text(r),
                )
        return city, r

    print("=== 各城（general）===\n", flush=True)
    if sequential:
        pairs = [await weather_for_city(c) for c in CITIES_CN]
    else:
        pairs = await asyncio.gather(*[weather_for_city(c) for c in CITIES_CN])

    city_results = dict(pairs)
    if any(r.exit_code != 0 for r in city_results.values()):
        return 1

    bundle = "\n\n".join(
        f"【{c}】{run_result_fuzzy_text(city_results[c])}" for c in CITIES_CN
    )
    summary_prompt = (
        "你是一位气象摘要助手。下面给出了 3 个中国主要城市各自的天气要点（每城一段）。\n"
        "请用中文输出：\n"
        "1）一段总体概述（5 句以内）；\n"
        "2）然后按城市列出一句结论（必须包含该城市名称）。\n\n"
        f"{bundle}"
    )

    summary = await client.async_run(
        summary_prompt,
        workspace,
        run_cfg=summary_cfg,
        timeout_s=TIMEOUT_SUMMARY_S,
    )
    if summary.exit_code != 0:
        print(f"汇总失败: exit {summary.exit_code}", file=sys.stderr)
        append_section(
            output_file,
            f"plan/summary (exit={summary.exit_code})",
            run_raw_output_text(summary),
        )
        return 1

    print("=== 汇总（plan）===\n")
    summary_text = run_result_fuzzy_text(summary)
    print(summary_text)
    append_section(
        output_file,
        f"plan/summary (exit={summary.exit_code})",
        run_raw_output_text(summary),
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="3 城天气并行查询 + 中文汇总")
    p.add_argument("--binary", default=None, help="opencode 可执行文件（默认从 PATH 查找）")
    p.add_argument("--workspace", type=Path, default=None, help="工作目录（默认临时目录）")
    p.add_argument("--sequential", action="store_true", help="顺序执行 3 城")
    p.add_argument(
        "--output-file",
        type=Path,
        default=Path("multi_agent_weather_output.txt"),
        help="将每个 agent 的输出保存到该文件",
    )
    args = p.parse_args()

    binary = args.binary or shutil.which("opencode")
    if not binary:
        print("未找到 opencode，请安装或传入 --binary", file=sys.stderr)
        raise SystemExit(1)

    if args.workspace is not None:
        ws = args.workspace.expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        raise SystemExit(
            asyncio.run(
                main_async(
                    ws,
                    binary=binary,
                    sequential=args.sequential,
                    output_file=args.output_file.expanduser().resolve(),
                )
            )
        )

    with tempfile.TemporaryDirectory(prefix="oc_weather_") as td:
        ws = Path(td)
        (ws / ".gitkeep").write_text("", encoding="utf-8")
        raise SystemExit(
            asyncio.run(
                main_async(
                    ws,
                    binary=binary,
                    sequential=args.sequential,
                    output_file=args.output_file.expanduser().resolve(),
                )
            )
        )


if __name__ == "__main__":
    main()
