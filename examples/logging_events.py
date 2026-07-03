"""
示例：``async_run`` 的 JSONL 事件日志（``log_file``）与类型过滤（``log_exclude_types``）。

``log_file`` 把本次运行的每个事件按 JSON 行追加落盘；``log_exclude_types`` 可将指定
``type`` 的事件排除**在磁盘日志之外**，但它们仍保留在 ``result.events`` 里。本示例排除
run 模式的记账事件 ``step_start`` / ``step_finish``，运行后对比“日志行数 < 事件总数”，
并确认被排除的类型不在日志中、却仍在 ``result.events`` 中。

需要本机已安装 ``opencode``、配置好模型。在项目根目录::

    PYTHONPATH=. python examples/logging_events.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig, run_result_fuzzy_text

TIMEOUT_S = 300
# run 模式的记账事件；排除后不落盘，但仍在 result.events 中。
# 注：server/session 模式的流式增量类型是 "message.part.delta"。
EXCLUDE = {"step_start", "step_finish"}


async def main_async(workspace: Path, *, binary: str, model: str, log_file: Path) -> int:
    client = AsyncOpenCodeClient(binary=binary)
    cfg = RunConfig(model=model, permission={"*": "allow"})

    result = await client.async_run(
        "请用中文一句话介绍你自己。",
        workspace,
        run_cfg=cfg,
        timeout_s=TIMEOUT_S,
        log_file=log_file,
        log_exclude_types=EXCLUDE,
    )

    if result.exit_code != 0:
        print(f"运行失败: exit {result.exit_code}", file=sys.stderr)
        return 1

    print(f"=== 模型输出 ===\n{run_result_fuzzy_text(result)}\n", flush=True)

    # 读回磁盘日志，按 type 统计。
    logged = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    logged_types = Counter(ev.get("type") for ev in logged)
    all_types = Counter(ev.get("type") for ev in result.events)

    print(f"result.events 事件总数：{len(result.events)}")
    print(f"日志文件行数：{len(logged)}  ({log_file})")
    print(f"被排除类型：{sorted(EXCLUDE)}\n")

    print("按类型对比（result.events / 日志）：")
    for t in sorted(all_types):
        print(f"  {t:<16} {all_types[t]:>3} / {logged_types.get(t, 0):>3}")

    # 断言：被排除类型出现在 events、但不在日志。
    excluded_in_events = any(all_types.get(t, 0) > 0 for t in EXCLUDE)
    excluded_in_log = any(logged_types.get(t, 0) > 0 for t in EXCLUDE)
    if excluded_in_events and not excluded_in_log:
        print("\n✓ 被排除类型仍在 result.events 中，但未写入日志文件")
    else:
        print(
            f"\n⚠ 未观察到预期效果（events 命中={excluded_in_events}, 日志命中={excluded_in_log}）",
            file=sys.stderr,
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="async_run 事件日志与类型过滤示例")
    p.add_argument("--binary", default=None, help="opencode 可执行文件（默认从 PATH 查找）")
    p.add_argument(
        "--model", default="opencode/big-pickle", help="模型（provider/model，默认 opencode/big-pickle）"
    )
    p.add_argument("--workspace", type=Path, default=None, help="工作目录（默认临时目录）")
    p.add_argument("--log-file", type=Path, default=None, help="事件日志路径（默认临时文件）")
    args = p.parse_args()

    binary = args.binary or shutil.which("opencode")
    if not binary:
        print("未找到 opencode，请安装或传入 --binary", file=sys.stderr)
        raise SystemExit(1)

    def _run(ws: Path, log_file: Path) -> int:
        return asyncio.run(main_async(ws, binary=binary, model=args.model, log_file=log_file))

    if args.workspace is not None:
        ws = args.workspace.expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        log_file = (args.log_file or Path("logging_events_output.jsonl")).expanduser().resolve()
        raise SystemExit(_run(ws, log_file))

    with tempfile.TemporaryDirectory(prefix="oc_logging_") as td:
        ws = Path(td)
        (ws / ".gitkeep").write_text("", encoding="utf-8")
        log_file = (
            args.log_file.expanduser().resolve()
            if args.log_file is not None
            else ws / "events.jsonl"
        )
        raise SystemExit(_run(ws, log_file))


if __name__ == "__main__":
    main()
