# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Async Python wrapper around the OpenCode CLI (`opencode run --format json`). Designed as a subprocess-based executor for multi-agent workflow orchestration. No runtime dependencies — only `pytest` and `pytest-asyncio` for dev.

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run all unit tests (no network/API calls)
pytest -q -m "not integration"

# Run a single test file
pytest -q tests/test_event_parser.py

# Run a single test by name
pytest -q -k "test_name_here"

# Integration tests (requires `opencode` on PATH + provider auth, slow)
pytest -m integration -q tests/test_integration_opencode.py

# Multi-agent weather integration (11 API calls, off by default)
OPENCODE_MULTI_AGENT_WEATHER=1 pytest -m integration -v tests/test_integration_multi_agent_weather.py
```

## Architecture

The wrapper lives in `opencode_wrapper/` with four modules:

- **`client.py`** — `AsyncOpenCodeClient` spawns `opencode run --format json` as a subprocess. Two main methods: `async_run()` (returns aggregated `RunResult`) and `async_stream()` (yields parsed event dicts). Helper functions `build_argv()` and `build_env()` construct the CLI invocation.
- **`config.py`** — `RunConfig` dataclass maps to CLI flags and `OPENCODE_CONFIG_CONTENT` env var (JSON). Config is injected per-call via deep-merge of `permission`, `mcp`, `tools`, and `config_overrides` fields.
- **`events.py`** — `parse_event_line()` handles JSON stdout lines; non-JSON lines become `diagnostic` events so the stream never breaks. `RunResult` aggregates events, extracted text, and tool call summaries. `run_result_fuzzy_text()` does best-effort text extraction across varying event shapes.
- **`errors.py`** — Exception hierarchy rooted at `OpenCodeError`. `OpenCodeProcessError` captures exit code, stderr, events, and raw stdout for debugging.

## Key Design Decisions

- **Zero runtime deps**: stdlib-only (`asyncio`, `json`, `shutil`, `dataclasses`). Test deps are optional.
- **Config via env var**: `RunConfig` serializes to `OPENCODE_CONFIG_CONTENT` JSON rather than writing temp config files.
- **Fault-tolerant parsing**: Non-JSON stdout lines become diagnostic events instead of raising errors, so partial or malformed output never breaks the event stream.
- **pytest-asyncio `auto` mode**: All async test functions are automatically treated as async tests (configured in `pyproject.toml`).

## Test Markers

- `integration` — requires real `opencode` CLI and configured provider (network/API; slow)
- `multi_agent_weather` — 11-call weather workflow; enable with `OPENCODE_MULTI_AGENT_WEATHER=1`

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENCODE_BINARY` | Path to `opencode` if not on `PATH` |
| `OPENCODE_INTEGRATION=0` | Skip integration tests |
| `OPENCODE_INTEGRATION_TIMEOUT_S` | Per-test timeout (default 300s) |
| `OPENCODE_MULTI_AGENT_WEATHER=1` | Enable multi-agent weather test |
