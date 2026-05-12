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

## Releasing

GitHub remote: `idailylife/oc_py_wrapper`. Releases are tagged + built locally and published with `gh`; PyPI publish is automated via GitHub Actions on the `release: published` event using PyPI Trusted Publishers (OIDC — no API token). `dist/` is gitignored.

### One-time PyPI setup

Configure a trusted publisher on PyPI:

1. https://pypi.org/manage/account/publishing/ → add a **pending** publisher (before the package exists on PyPI) or open the existing project's *Publishing* tab.
2. Fields:
   - PyPI project name: `py-opencode-wrapper`
   - Owner: `idailylife`
   - Repository: `oc_py_wrapper`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
3. On GitHub, create an environment named `pypi` (repo → Settings → Environments). No secrets needed.

### Cutting a release

```bash
# 1. Bump version in pyproject.toml (X.Y.Z) and commit
# 2. Build wheel + sdist locally (sanity check; CI rebuilds these)
python -m build

# 3. Tag and push
git tag vX.Y.Z
git push origin main vX.Y.Z

# 4. Create the GitHub release with the dist artifacts attached
gh release create vX.Y.Z \
  dist/py_opencode_wrapper-X.Y.Z-py3-none-any.whl \
  dist/py_opencode_wrapper-X.Y.Z.tar.gz \
  --title "vX.Y.Z — <short summary>" \
  --notes "<release notes>"
```

Publishing the release fires `.github/workflows/release.yml`, which rebuilds the artifacts in CI and uploads them to PyPI via OIDC. `gh` must already be authenticated (`gh auth status`).

## Architecture

The wrapper lives in `opencode_wrapper/` with four modules:

- **`client.py`** — `AsyncOpenCodeClient` spawns `opencode run --format json` as a subprocess. Two main methods: `async_run()` (returns aggregated `RunResult`) and `async_stream()` (yields parsed event dicts). Helper functions `build_argv()` and `build_env()` construct the CLI invocation.
- **`config.py`** — `RunConfig` dataclass maps to CLI flags and `OPENCODE_CONFIG_CONTENT` env var (JSON). Config is injected per-call via deep-merge of `permission`, `mcp`, `tools`, and `config_overrides` fields.
- **`events.py`** — `parse_event_line()` handles JSON stdout lines; non-JSON lines become `diagnostic` events so the stream never breaks. `RunResult` aggregates events, extracted text, and tool call summaries. `run_result_fuzzy_text()` does best-effort text extraction across varying event shapes.
- **`errors.py`** — Exception hierarchy rooted at `OpenCodeError`. `OpenCodeProcessError` captures exit code, stderr, events, and raw stdout for debugging.

## Key Design Decisions

- **Zero runtime deps**: stdlib-only (`asyncio`, `json`, `shutil`, `dataclasses`). Test deps are optional.
- **Config via env var**: `RunConfig` serializes to `OPENCODE_CONFIG_CONTENT` JSON rather than writing temp config files.
- **Hermetic by default**: `RunConfig.inherit_user_config` defaults to `False`. The wrapper redirects `XDG_CONFIG_HOME` and `OPENCODE_TEST_HOME` at a sanitized tmpdir copy of the host's global opencode config, keeping only `provider` / `disabled_providers` / `enabled_providers` / `$schema` and dropping everything else (`mcp`, `agent`, `command`, `tools`, `plugin`, `skills`, `instructions`, `permission`, `model`, ...). Project-level config (cwd walk + `.opencode/`) and `auth.json` are untouched. Set `inherit_user_config=True` to restore the legacy "inherit everything" behavior. Benchmark callers must pin `model` explicitly.
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

Env vars set by the wrapper on the child process (informational): `XDG_DATA_HOME` (when `isolate_db=True`, the default), `XDG_CONFIG_HOME` + `OPENCODE_TEST_HOME` (when `inherit_user_config=False`, the default), `OPENCODE_CONFIG_CONTENT` (whenever `RunConfig` has any of `permission` / `mcp` / `tools` / `instructions` / `config_overrides`), `OPENCODE_DISABLE_AUTOUPDATE=1` (when `disable_autoupdate=True`). Under hermetic mode the wrapper also strips inherited `OPENCODE_CONFIG` and `OPENCODE_CONFIG_DIR`. macOS MDM `.mobileconfig` and remote org config (`/.well-known/opencode`) are NOT suppressed.
