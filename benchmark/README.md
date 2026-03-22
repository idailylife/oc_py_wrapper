# benchmark

A/B benchmark framework for comparing OpenCode configurations. Runs eval cases against multiple configs, auto-grades with LLM-as-judge, supports blind human review, and synthesizes all signals into a final analysis.

## Quick Start

```bash
# 1. Run benchmark
python -m benchmark run \
  --evals benchmark/examples/evals.json \
  --config baseline=benchmark/examples/config_a.json \
  --config thinking=benchmark/examples/config_b.json \
  --output my_benchmark

# 2. Human review (blind A/B comparison in browser)
python -m benchmark review --output my_benchmark

# 3. Post-benchmark analysis
python -m benchmark analyze --output my_benchmark
```

## Pipeline

```
run ──→ grade ──→ review (human) ──→ analyze
 2 instances   1 instance   browser        1 instance
```

- **run**: Executes each eval case against all configs in parallel via `AsyncOpenCodeClient`.
- **grade**: LLM-as-judge evaluates each output against assertions (PASS/FAIL).
- **review**: Local HTTP server serves a blind A/B comparison UI. Reviewer scores outputs 1-5 and picks a preferred config. Feedback is saved to `feedback.json`.
- **analyze**: Synthesizes auto grading + human feedback, surfaces patterns, and outputs a verdict with confidence level.

## File Layout

```
benchmark/
├── models.py        # Data models (EvalCase, RunOutput, GradingResult, Feedback, ...)
├── runner.py        # Orchestration engine
├── grader.py        # LLM auto-grader
├── aggregator.py    # Statistics (mean, stddev, delta)
├── analyzer.py      # Post-benchmark LLM analysis
├── reviewer.py      # Human review HTTP server
├── viewer.html      # Blind A/B review UI
├── agents/
│   ├── grader.md    # Grader agent prompt
│   └── analyzer.md  # Analyzer agent prompt
└── examples/
    ├── evals.json   # Sample eval suite
    ├── config_a.json
    └── config_b.json
```

## Output Directory Structure

After a run, the output directory looks like:

```
my_benchmark/
├── evals.json           # Copy of the eval suite
├── eval-fizzbuzz/
│   ├── baseline/
│   │   ├── output.json  # RunOutput (text, tool_calls, duration, ...)
│   │   └── grading.json # GradingResult (per-assertion PASS/FAIL)
│   └── thinking/
│       ├── output.json
│       └── grading.json
├── eval-bug-find/
│   └── ...
├── benchmark.json       # Aggregated report
├── benchmark.md         # Human-readable summary
├── feedback.json        # Human review entries
└── analysis.json        # Analyzer output (verdict, observations)
```

## Eval Suite Format

```json
{
  "name": "my-benchmark",
  "description": "What this benchmark tests",
  "evals": [
    {
      "id": "unique-id",
      "prompt": "The task prompt sent to opencode",
      "assertions": [
        "Output should contain X",
        "Function handles edge case Y"
      ],
      "expected_output": "Description of ideal output",
      "workspace": ".",
      "files": [],
      "tags": ["category"]
    }
  ]
}
```

## Config Format

Each config file is a JSON object matching `RunConfig` fields:

```json
{
  "model": "some-model",
  "thinking": true,
  "disable_autoupdate": true,
  "permission": { "...": "allow" },
  "config_overrides": { "...": "..." }
}
```

## CLI Reference

```
python -m benchmark run     --evals PATH --config name=path.json [--config ...] [--output DIR]
                            [--timeout SEC] [--concurrency N] [--no-grade] [--grader-config PATH]

python -m benchmark review  [--output DIR] [--port PORT]

python -m benchmark analyze [--output DIR] [--timeout SEC] [--analyzer-config PATH]
```

Global options: `--binary PATH`, `--workspace DIR`.
