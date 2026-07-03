"""
示例：``OpenCodeSession`` 多轮会话，验证上下文跨轮原生保留。

与一次性的 ``async_run`` 不同，``OpenCodeSession`` 在 ``async with`` 期间持有一个
``opencode serve`` 进程并复用同一个服务端 session，模型自身即记住上下文。

需要本机已安装 ``opencode``、配置好模型。在项目根目录::

    PYTHONPATH=. python examples/session_multi_turn.py
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from opencode_wrapper import (
    AsyncOpenCodeClient,
    OpenCodeSession,
    RunConfig,
    run_result_fuzzy_text,
)

TIMEOUT_S = 300


async def main_async(workspace: Path, *, binary: str, model: str) -> int:
    client = AsyncOpenCodeClient(binary=binary)
    cfg = RunConfig(model=model, permission={"*": "deny"})

    async with OpenCodeSession(client, workspace, run_cfg=cfg, timeout_s=TIMEOUT_S) as s:
        print(f"session_id={s.session_id}\n", flush=True)

        r1 = await s.send("我叫小明，请记住。")
        print(f"=== 第 1 轮 ===\n{run_result_fuzzy_text(r1)}\n", flush=True)

        # 第 2 轮不重复名字：若上下文保留，模型应答出“小明”。
        r2 = await s.send("我刚才说我叫什么名字？")
        text2 = run_result_fuzzy_text(r2)
        print(f"=== 第 2 轮 ===\n{text2}\n", flush=True)

        if r2.exit_code != 0:
            print(f"第 2 轮失败: exit {r2.exit_code}", file=sys.stderr)
            return 1
        if "小明" in text2:
            print("✓ 上下文跨轮保留（答案含“小明”）")
        else:
            print("⚠ 答案未包含“小明”，请检查模型输出", file=sys.stderr)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="OpenCodeSession 多轮上下文示例")
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

    with tempfile.TemporaryDirectory(prefix="oc_session_") as td:
        ws = Path(td)
        (ws / ".gitkeep").write_text("", encoding="utf-8")
        raise SystemExit(asyncio.run(main_async(ws, binary=binary, model=args.model)))


if __name__ == "__main__":
    main()
