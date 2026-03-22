"""CLI entry point: python -m benchmark <command> [options]."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def cmd_run(args: argparse.Namespace) -> None:
    """Run a benchmark suite against two configs."""
    from benchmark.aggregator import aggregate, format_markdown
    from benchmark.grader import grade_output
    from benchmark.models import EvalSuite, GradingResult, RunOutput
    from benchmark.runner import run_benchmark
    from opencode_wrapper import AsyncOpenCodeClient, RunConfig

    suite = EvalSuite.from_file(args.evals)

    # Load configs from JSON files
    configs: dict[str, RunConfig] = {}
    for spec in args.config:
        name, path = spec.split("=", 1)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        configs[name] = RunConfig(**{
            k: v for k, v in data.items() if k in RunConfig.__dataclass_fields__
        })

    if len(configs) < 2:
        print("Error: provide at least 2 configs via --config name=path.json", file=sys.stderr)
        sys.exit(1)

    async def _main() -> None:
        # Phase 1: Run all evals
        print(f"Running {len(suite.evals)} evals × {len(configs)} configs...")
        out_dir = await run_benchmark(
            suite, configs,
            workspace=args.workspace,
            timeout_s=args.timeout,
            output_dir=args.output,
            binary=args.binary,
            concurrency=args.concurrency,
        )

        # Phase 2: Auto-grade
        if args.grade:
            print("Grading outputs...")
            client = AsyncOpenCodeClient(binary=args.binary)
            grader_cfg = None
            if args.grader_config:
                gdata = json.loads(Path(args.grader_config).read_text(encoding="utf-8"))
                grader_cfg = RunConfig(**{
                    k: v for k, v in gdata.items() if k in RunConfig.__dataclass_fields__
                })

            for case in suite.evals:
                case_dir = out_dir / f"eval-{case.id}"
                for config_name in configs:
                    cfg_dir = case_dir / config_name
                    out_path = cfg_dir / "output.json"
                    if not out_path.exists():
                        continue
                    output = RunOutput.from_file(out_path)
                    grading = await grade_output(
                        client, case, output,
                        grader_workspace=args.workspace,
                        grader_cfg=grader_cfg,
                        timeout_s=args.timeout,
                    )
                    grading.save(cfg_dir / "grading.json")
                    print(f"  {case.id}/{config_name}: {grading.pass_rate:.0%}")

        # Phase 3: Aggregate
        print("Aggregating results...")
        all_gradings: dict[str, list[GradingResult]] = {n: [] for n in configs}
        all_outputs: dict[str, list[RunOutput]] = {n: [] for n in configs}

        for case in suite.evals:
            case_dir = out_dir / f"eval-{case.id}"
            for config_name in configs:
                cfg_dir = case_dir / config_name
                out_path = cfg_dir / "output.json"
                if out_path.exists():
                    all_outputs[config_name].append(RunOutput.from_file(out_path))
                grading_path = cfg_dir / "grading.json"
                if grading_path.exists():
                    all_gradings[config_name].append(GradingResult.from_file(grading_path))

        report = aggregate(
            suite.name, list(configs.keys()), all_gradings, all_outputs,
        )
        report.save(out_dir / "benchmark.json")
        md = format_markdown(report)
        (out_dir / "benchmark.md").write_text(md, encoding="utf-8")
        print(md)
        print(f"\nResults saved to {out_dir}/")

    asyncio.run(_main())


def cmd_review(args: argparse.Namespace) -> None:
    """Launch the human review UI."""
    from benchmark.reviewer import serve
    serve(args.output, port=args.port)


def cmd_analyze(args: argparse.Namespace) -> None:
    """Run the analyzer agent on benchmark results."""
    from benchmark.analyzer import analyze
    from benchmark.models import BenchmarkReport
    from benchmark.reviewer import load_feedback
    from opencode_wrapper import AsyncOpenCodeClient, RunConfig

    out_dir = Path(args.output)
    report_data = json.loads((out_dir / "benchmark.json").read_text(encoding="utf-8"))

    # Reconstruct report (shallow — enough for analysis)
    from benchmark.models import BenchmarkRunSummary, ConfigStats
    summary_data = report_data.get("summary", {})
    config_stats = [ConfigStats(**cs) for cs in summary_data.get("configs", [])]
    summary = BenchmarkRunSummary(
        configs=config_stats,
        delta_pass_rate=summary_data.get("delta_pass_rate", 0),
        delta_duration_s=summary_data.get("delta_duration_s", 0),
    )
    feedback = load_feedback(out_dir)
    report = BenchmarkReport(
        suite_name=report_data.get("suite_name", ""),
        configs=report_data.get("configs", []),
        runs=report_data.get("runs", []),
        summary=summary,
        feedback=feedback,
    )

    async def _main() -> None:
        client = AsyncOpenCodeClient(binary=args.binary)
        cfg = None
        if args.analyzer_config:
            adata = json.loads(Path(args.analyzer_config).read_text(encoding="utf-8"))
            cfg = RunConfig(**{
                k: v for k, v in adata.items() if k in RunConfig.__dataclass_fields__
            })
        result = await analyze(
            client, report,
            analyzer_workspace=args.workspace,
            analyzer_cfg=cfg,
            timeout_s=args.timeout,
        )
        output_path = out_dir / "analysis.json"
        output_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"\nAnalysis saved to {output_path}")

    asyncio.run(_main())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="A/B benchmark framework for OpenCode configurations",
    )
    parser.add_argument("--binary", default="opencode", help="OpenCode binary path")
    parser.add_argument("--workspace", default=".", help="Default workspace directory")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = sub.add_parser("run", help="Run a benchmark suite")
    p_run.add_argument("--evals", required=True, help="Path to evals.json")
    p_run.add_argument("--config", action="append", required=True,
                       help="Config spec: name=path.json (provide at least 2)")
    p_run.add_argument("--output", default="benchmark_output", help="Output directory")
    p_run.add_argument("--timeout", type=float, default=300.0, help="Per-run timeout (seconds)")
    p_run.add_argument("--concurrency", type=int, default=2, help="Max parallel eval cases")
    p_run.add_argument("--grade", action="store_true", default=True, help="Auto-grade outputs")
    p_run.add_argument("--no-grade", dest="grade", action="store_false")
    p_run.add_argument("--grader-config", help="RunConfig JSON for the grader agent")
    p_run.set_defaults(func=cmd_run)

    # --- review ---
    p_review = sub.add_parser("review", help="Launch human review UI")
    p_review.add_argument("--output", default="benchmark_output", help="Output directory")
    p_review.add_argument("--port", type=int, default=8384, help="Server port")
    p_review.set_defaults(func=cmd_review)

    # --- analyze ---
    p_analyze = sub.add_parser("analyze", help="Run post-benchmark analysis")
    p_analyze.add_argument("--output", default="benchmark_output", help="Output directory")
    p_analyze.add_argument("--timeout", type=float, default=120.0)
    p_analyze.add_argument("--analyzer-config", help="RunConfig JSON for the analyzer agent")
    p_analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
