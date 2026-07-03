"""
示例：``async_stream`` 实时消费解析后的 JSON 事件。

``async_stream`` 逐条 yield 已解析的事件字典（``opencode run --format json`` 的每行
输出），可在模型运行过程中实时观察，而不必等到 ``async_run`` 聚合完成。

需要本机已安装 ``opencode``、配置好模型。在项目根目录::

    PYTHONPATH=. python examples/stream_events.py
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig


def _brief(ev: dict) -> str:
    """从事件里挑一点可读内容，便于观察流的推进。"""
    part = ev.get("part") or {}
    if isinstance(part, dict):
        text = part.get("text")
        if text:
            snippet = text.replace("\n", " ").strip()
            return snippet[:80]
    return ""


async def main_async(workspace: Path, *, binary: str, model: str) -> int:
    client = AsyncOpenCodeClient(binary=binary)
    cfg = RunConfig(model=model, permission={"*": "allow"})

    prompt = "请用中文分 3 步简述如何冲一杯手冲咖啡，每步一句。"

    n = 0
    async for ev in client.async_stream(prompt, workspace, run_cfg=cfg):
        n += 1
        etype = ev.get("type", "?")
        brief = _brief(ev)
        line = f"[{n:03d}] {etype}"
        if brief:
            line += f"  {brief}"
        print(line, flush=True)

    print(f"\n共收到 {n} 个事件。", flush=True)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="async_stream 实时事件流示例")
    p.add_argument("--binary", default=None, help="opencode 可执行文件（默认从 PATH 查找）")
    p.add_argument(
        "--model", default="opencode/big-pickle", help="模型（provider/model，默认 opencode/big-pickle）"
    )
    p.add_argument("--workspace", type=Path, default=None, help="工作目录（默认临时目录）")
    args = p.parse_args()

    binary = args.binary or shutil.which("opencode")
    if not binary:
        print("未找到 opencode，请安装或传入 --binary", file=sys.stderr)
        raise SystemExit(1)

    if args.workspace is not None:
        ws = args.workspace.expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        raise SystemExit(asyncio.run(main_async(ws, binary=binary, model=args.model)))

    with tempfile.TemporaryDirectory(prefix="oc_stream_") as td:
        ws = Path(td)
        (ws / ".gitkeep").write_text("", encoding="utf-8")
        raise SystemExit(asyncio.run(main_async(ws, binary=binary, model=args.model)))


if __name__ == "__main__":
    main()
