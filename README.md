# oc-py-harness

Python **async** wrapper around the [OpenCode](https://opencode.ai/docs/) CLI (`opencode run --format json`). Intended as a subprocess-based executor for **multi-agent workflow** orchestration.

## Requirements

- Python 3.10+
- `opencode` on `PATH` (or pass an absolute path to the binary)

## Install (local tree)

```bash
pip install -e ".[dev]"
```

## Usage

### One-shot run with aggregated result

```python
import asyncio
from pathlib import Path

from opencode_wrapper import AsyncOpenCodeClient, RunConfig

async def main():
    client = AsyncOpenCodeClient("opencode")
    cfg = RunConfig(
        model="anthropic/claude-sonnet-4-5",
        agent="plan",
        permission={"bash": "deny", "edit": "deny"},
        mcp={
            "demo": {
                "type": "local",
                "command": ["npx", "-y", "@modelcontextprotocol/server-everything"],
                "enabled": True,
            }
        },
    )
    result = await client.async_run(
        "Summarize the README in one sentence.",
        Path("/path/to/repo"),
        run_cfg=cfg,
        timeout_s=600,
    )
    print(result.exit_code, result.final_text)

asyncio.run(main())
```

### Stream structured JSON events

```python
async def stream_example():
    client = AsyncOpenCodeClient()
    cfg = RunConfig(permission={"*": "allow"})
    async for event in client.async_stream("List top-level files.", workspace=".", run_cfg=cfg):
        print(event)
```

### Parallel agents (`asyncio.gather`)

```python
async def multi():
    client = AsyncOpenCodeClient()
    ws = Path("/path/to/monorepo")
    r1, r2 = await asyncio.gather(
        client.async_run("Explain services/api.", ws / "services" / "api", run_cfg=RunConfig(agent="explore")),
        client.async_run("Explain packages/ui.", ws / "packages" / "ui", run_cfg=RunConfig(agent="explore")),
    )
    return r1, r2
```

## Configuration injection

Per-call JSON is merged and passed as `OPENCODE_CONFIG_CONTENT` (see [OpenCode config](https://opencode.ai/docs/config/)). Use `RunConfig` fields:

| Field | Purpose |
|--------|---------|
| `permission` | `permission` map (`allow` / `ask` / `deny`, patterns) |
| `mcp` | MCP server definitions |
| `tools` | Enable/disable tools (including MCP globs) |
| `config_overrides` | Any extra top-level config keys to deep-merge |

Optional env tuning: `disable_autoupdate=True` sets `OPENCODE_DISABLE_AUTOUPDATE=1`.

## CLI arguments

`RunConfig` maps to flags such as `--agent`, `-m`, `-f`, `--attach`, `--title`, etc. Prompt text is appended as the final `opencode run` message argument.

## Tests

Unit tests (no real OpenCode / no API calls):

```bash
pytest -q -m "not integration"
```

Integration tests (real `opencode run`, needs working provider auth — **slow**, may incur API usage):

```bash
pytest -m integration -q tests/test_integration_opencode.py
```

**Multi-agent weather workflow** (10 parallel city lookups + 1 summary — **11 API calls**, not run by default):

```bash
OPENCODE_MULTI_AGENT_WEATHER=1 pytest -m integration -v tests/test_integration_multi_agent_weather.py
```

Optional: `OPENCODE_WEATHER_SEQUENTIAL=1` runs the 10 city calls one-by-one (easier on rate limits).  
Per-stage timeouts: `OPENCODE_WEATHER_PER_CITY_TIMEOUT_S`, `OPENCODE_WEATHER_SUMMARY_TIMEOUT_S` (default: same as `OPENCODE_INTEGRATION_TIMEOUT_S`).

| Env | Meaning |
|-----|--------|
| `OPENCODE_BINARY` | Absolute path to `opencode` if not on `PATH` |
| `OPENCODE_INTEGRATION=0` | Skip integration tests |
| `OPENCODE_INTEGRATION_TIMEOUT_S` | Per-test timeout seconds (default `300`) |
| `OPENCODE_MULTI_AGENT_WEATHER=1` | Enable 11-call weather integration test |
| `OPENCODE_ENABLE_EXA` | Passed through / defaulted to `1` in that test for web search tools |

Default `pytest -q` runs **all** tests; use `-m "not integration"` in CI without OpenCode.

## Notes

- Event shapes from `--format json` may change between OpenCode versions; unknown fields are preserved in each parsed dict.
- For fully non-interactive automation, prefer explicit `permission` (`allow`/`deny`) over relying on interactive `ask` prompts.
