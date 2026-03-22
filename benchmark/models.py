"""Data models for the benchmark framework."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EvalCase:
    """A single evaluation test case."""

    id: str
    prompt: str
    assertions: list[str] = field(default_factory=list)
    expected_output: str = ""
    workspace: str = "."
    files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalSuite:
    """A collection of eval cases with metadata."""

    name: str
    description: str
    evals: list[EvalCase] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_file(cls, path: str | Path) -> EvalSuite:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        evals = [EvalCase(**e) for e in data.get("evals", [])]
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            evals=evals,
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


@dataclass
class RunOutput:
    """Captured output from a single opencode run."""

    config_name: str
    eval_id: str
    final_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    stderr: str = ""
    duration_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def from_file(cls, path: str | Path) -> RunOutput:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class GradingResult:
    """Grading output for one (eval_case, config) pair."""

    eval_id: str
    config_name: str
    expectations: list[dict[str, Any]] = field(default_factory=list)
    # Each entry: {"text": str, "passed": bool, "evidence": str}
    pass_rate: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def from_file(cls, path: str | Path) -> GradingResult:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Feedback:
    """Human review feedback for one eval case."""

    eval_id: str
    preferred: str = ""  # config name or "tie"
    scores: dict[str, int] = field(default_factory=dict)  # config_name -> 1-5
    comment: str = ""
    reviewer: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConfigStats:
    """Aggregated statistics for one config across all eval cases."""

    config_name: str
    n: int = 0
    pass_rate_mean: float = 0.0
    pass_rate_stddev: float = 0.0
    pass_rate_min: float = 0.0
    pass_rate_max: float = 0.0
    duration_mean_s: float = 0.0
    duration_stddev_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BenchmarkRunSummary:
    """Per-config stats plus deltas."""

    configs: list[ConfigStats] = field(default_factory=list)
    delta_pass_rate: float = 0.0  # config_b - config_a
    delta_duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BenchmarkReport:
    """Full benchmark report combining all signals."""

    suite_name: str
    configs: list[str] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    summary: BenchmarkRunSummary = field(default_factory=BenchmarkRunSummary)
    feedback: list[Feedback] = field(default_factory=list)
    analysis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
