"""Aggregate grading results into benchmark statistics."""

from __future__ import annotations

import math

from benchmark.models import (
    BenchmarkReport,
    BenchmarkRunSummary,
    ConfigStats,
    Feedback,
    GradingResult,
    RunOutput,
)


def _stats(values: list[float]) -> tuple[float, float, float, float]:
    """Return (mean, sample_stddev, min, max) for a list of floats."""
    if not values:
        return 0.0, 0.0, 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0, min(values), max(values)
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance), min(values), max(values)


def compute_config_stats(
    config_name: str,
    gradings: list[GradingResult],
    outputs: list[RunOutput],
) -> ConfigStats:
    """Compute aggregate stats for one config."""
    pass_rates = [g.pass_rate for g in gradings]
    durations = [o.duration_s for o in outputs]

    pr_mean, pr_std, pr_min, pr_max = _stats(pass_rates)
    dur_mean, dur_std, _, _ = _stats(durations)

    return ConfigStats(
        config_name=config_name,
        n=len(gradings),
        pass_rate_mean=pr_mean,
        pass_rate_stddev=pr_std,
        pass_rate_min=pr_min,
        pass_rate_max=pr_max,
        duration_mean_s=dur_mean,
        duration_stddev_s=dur_std,
    )


def aggregate(
    suite_name: str,
    config_names: list[str],
    all_gradings: dict[str, list[GradingResult]],
    all_outputs: dict[str, list[RunOutput]],
    feedback: list[Feedback] | None = None,
) -> BenchmarkReport:
    """
    Build a BenchmarkReport from grading results and run outputs.

    all_gradings: {config_name: [GradingResult, ...]}
    all_outputs:  {config_name: [RunOutput, ...]}
    """
    stats_list: list[ConfigStats] = []
    for name in config_names:
        gradings = all_gradings.get(name, [])
        outputs = all_outputs.get(name, [])
        stats_list.append(compute_config_stats(name, gradings, outputs))

    # Delta: second config minus first (if exactly two)
    delta_pr = 0.0
    delta_dur = 0.0
    if len(stats_list) == 2:
        delta_pr = stats_list[1].pass_rate_mean - stats_list[0].pass_rate_mean
        delta_dur = stats_list[1].duration_mean_s - stats_list[0].duration_mean_s

    summary = BenchmarkRunSummary(
        configs=stats_list,
        delta_pass_rate=delta_pr,
        delta_duration_s=delta_dur,
    )

    # Flatten per-eval runs for the report
    runs: list[dict] = []
    for name in config_names:
        for g in all_gradings.get(name, []):
            runs.append(g.to_dict())

    return BenchmarkReport(
        suite_name=suite_name,
        configs=config_names,
        runs=runs,
        summary=summary,
        feedback=feedback or [],
    )


def format_markdown(report: BenchmarkReport) -> str:
    """Render a human-readable markdown summary."""
    lines = [f"# Benchmark: {report.suite_name}\n"]

    for cs in report.summary.configs:
        lines.append(f"## {cs.config_name}")
        lines.append(f"- Cases: {cs.n}")
        lines.append(f"- Pass rate: {cs.pass_rate_mean:.1%} "
                      f"(stddev {cs.pass_rate_stddev:.1%}, "
                      f"range {cs.pass_rate_min:.1%}–{cs.pass_rate_max:.1%})")
        lines.append(f"- Duration: {cs.duration_mean_s:.1f}s "
                      f"(stddev {cs.duration_stddev_s:.1f}s)")
        lines.append("")

    if len(report.summary.configs) == 2:
        lines.append("## Delta (B − A)")
        lines.append(f"- Pass rate: {report.summary.delta_pass_rate:+.1%}")
        lines.append(f"- Duration: {report.summary.delta_duration_s:+.1f}s")
        lines.append("")

    if report.feedback:
        lines.append("## Human Feedback")
        pref_counts: dict[str, int] = {}
        for fb in report.feedback:
            pref_counts[fb.preferred] = pref_counts.get(fb.preferred, 0) + 1
        for pref, count in sorted(pref_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- Preferred **{pref}**: {count}")
        lines.append("")

    return "\n".join(lines)
