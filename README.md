# py-opencode-wrapper

Python **async** wrapper around the [OpenCode](https://opencode.ai/docs/) CLI (`opencode run --format json`). Intended as a subprocess-based executor for **multi-agent workflow** orchestration.

## Requirements

- Python 3.8+
- `opencode` on `PATH` (or pass an absolute path to the binary)

## Install

From PyPI (most users):

```bash
pip install py-opencode-wrapper
```

The distribution name on PyPI is `py-opencode-wrapper`; import it as `opencode_wrapper`:

```python
from opencode_wrapper import AsyncOpenCodeClient, RunConfig
```

For local development (editable install with test deps):

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
        model="opencode/big-pickle",
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

Set `RunConfig(cli_kwargs={"thinking": True})` when you want OpenCode
reasoning/thinking parts included in `result.events` and `log_file` JSON lines in
**run mode**. This maps to OpenCode's display/output flag `--thinking`; it does
not change model reasoning effort. In **server/session mode** there is no
`--thinking` equivalent — reasoning parts are produced per the model's reasoning
config and streamed onto the SSE bus unconditionally, so they already land in
`result.events` / `log_file` with no opt-in.

Pass `async_run(..., log_exclude_types={"message.part.delta"})` to keep selected
event types out of `log_file` (e.g. streaming delta chunks). Excluded events are
still returned in `result.events` — this only trims the on-disk log. The default
(`None`) logs every event.

### Multi-turn conversation (`OpenCodeSession`)

For a stateful, multi-turn chat, use `OpenCodeSession` as an async context
manager. Unlike the one-shot `async_run`/`async_stream` (which spawn
`opencode run` per call), a session owns a headless `opencode serve` process for
the duration of the `async with` block and re-prompts one server-side session, so
the model retains context **natively** across turns:

```python
import asyncio
from opencode_wrapper import AsyncOpenCodeClient, OpenCodeSession, RunConfig

async def chat():
    client = AsyncOpenCodeClient()
    async with OpenCodeSession(client, ".", run_cfg=RunConfig(model="opencode/big-pickle")) as s:
        r1 = await s.send("My name is Bob.")
        r2 = await s.send("What is my name?")   # continues natively → "Bob"
        print(s.session_id, r2.final_text)

asyncio.run(chat())
```

On enter, the session spawns `opencode serve` (with the same hermetic isolation
run mode uses) and creates one session pinned to the workspace; on exit the
session is deleted and the server torn down. `send()` accepts per-turn `run_cfg`
and `timeout_s` overrides, but only **prompt-body knobs** (`model` / `agent` /
`tools`) vary per turn — `permission` / `mcp` / `instructions` are fixed at enter
(they are server-global).

#### Human-in-the-loop permissions

Because the server can pause on a permission request, sessions support an
`on_permission` async callback that run mode cannot. Set `permission={"bash":
"ask"}` and answer each prompt with `"once"` / `"always"` / `"reject"`:

```python
async def approve(props):    # props: {"id", "sessionID", "permission", ...}
    return "once"

async with OpenCodeSession(client, ".", run_cfg=RunConfig(permission={"bash": "ask"}),
                           on_permission=approve) as s:
    r = await s.send("Run `echo hi` and tell me the output.")
```

When `on_permission` is `None` (the default), any `permission.asked` is
auto-rejected so a turn never blocks. File attachments are run-mode only — pass
`RunConfig(cli_kwargs={"f": ["a.txt", "b.png"]})` to `async_run`. Server-mode
sessions ignore `cli_kwargs`, so embed file content in the prompt instead.

#### Answering the model's questions

opencode's built-in `question` tool lets the model ask the user multiple-choice
questions mid-run (gather preferences, clarify, offer choices). Pass an
`on_question` async callback to answer it. The callback receives the question
props (`{"id", "sessionID", "questions": [{"question", "header", "options":
[{"label", "description"}], "multiple"?, "custom"?}], ...}`) and returns a list
with one entry per question — each a list of selected option labels. Returning
`None` rejects (dismisses) the question.

```python
async def answer(props):
    out = []
    for q in props["questions"]:
        out.append([q["options"][0]["label"]])   # pick the first option
    return out

async with OpenCodeSession(client, ".", run_cfg=RunConfig(model="opencode/big-pickle"),
                           on_question=answer) as s:
    r = await s.send("Ask me which database to use, then scaffold it.")
```

When `on_question` is `None` (the default), any `question.asked` is auto-rejected
so a turn never blocks. The `question` tool is enabled by default under
`opencode serve`; set `RunConfig(extra_env={"OPENCODE_ENABLE_QUESTION_TOOL": "1"})`
to force-enable it regardless of the server's client identity.

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
    results = await asyncio.gather(*[
        client.async_run(
            f"Explain services/{svc}.",
            ws / "services" / svc,
            run_cfg=RunConfig(agent="explore"),
            timeout_s=600,
        )
        for svc in ["api", "worker", "gateway"]
    ])
    return results
```

Safe defaults for parallel runs (startup serialisation, private SQLite DB per run, and automatic retry on SQLite-startup crashes) are enabled out of the box — most users don't need to tune them. See *Concurrency notes* below if you want to.

## Configuration injection

Per-call JSON is merged and passed as `OPENCODE_CONFIG_CONTENT` (see [OpenCode config](https://opencode.ai/docs/config/)). Use `RunConfig` fields:

| Field | Purpose |
|--------|---------|
| `permission` | `permission` map (`allow` / `deny`, patterns) |
| `mcp` | MCP server definitions |
| `tools` | Enable/disable tools (including MCP globs) |
| `instructions` | Instruction file paths / glob patterns to inject |
| `config_overrides` | Any extra top-level config keys to deep-merge |

Optional env tuning: `disable_autoupdate=True` sets `OPENCODE_DISABLE_AUTOUPDATE=1`.
Note: `ask` is intentionally rejected in subprocess mode (no interactive terminal); use `allow` or `deny`.

### User config isolation

By default, `RunConfig.inherit_user_config=False` makes each child `opencode`
process see a sanitized copy of the host's global OpenCode config. The wrapper
keeps only provider-selection keys (`$schema`, `provider`,
`disabled_providers`, `enabled_providers`) and drops capability/configuration
keys such as `mcp`, `agent`, `command`, `tools`, `plugin`, `skills`,
`instructions`, `permission`, and `model`.

This keeps benchmark and orchestration runs reproducible while still allowing
provider configuration and `opencode auth` credentials to work. Project-level
config discovered from the workspace is not suppressed.

Set `inherit_user_config=True` to restore the legacy behavior of inheriting the
host OpenCode config as-is. For reproducible runs, pass `model`, `permission`,
`mcp`, `tools`, and `instructions` explicitly through `RunConfig`.

## CLI arguments

In run mode, `model` and `agent` map to `-m` and `--agent`. Every other
`opencode run` flag is passed through `RunConfig.cli_kwargs`, a raw dict expanded
by `build_argv`:

- bool `True` → `--flag` (e.g. `{"fork": True}` → `--fork`)
- a value → `--flag=value` (e.g. `{"title": "demo"}` → `--title=demo`)
- a single-char key → `-k value` (e.g. `{"f": "a.txt"}` → `-f a.txt`)
- a list/tuple → repeated (e.g. `{"f": ["a.txt", "b.txt"]}` → `-f a.txt -f b.txt`)
- `False` / `None` → skipped

```python
RunConfig(model="anthropic/claude", cli_kwargs={"fork": True, "title": "demo", "f": ["a.txt"]})
# -> opencode run --format json -m anthropic/claude --fork --title=demo -f a.txt <prompt>
```

Prompt text is appended as the final `opencode run` message argument.
`cli_kwargs` is ignored by `OpenCodeSession` (server mode has no CLI surface).

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

## Concurrency notes

The defaults already handle the common pitfalls when running many `async_run` calls in parallel — you usually don't need to touch any of these.

- **Startup serialisation** (`startup_concurrency=1`, `startup_delay_s=0.3`) — spaces out SQLite WAL initialisation across processes to avoid a startup race in `opencode`.
- **DB isolation** (`isolate_db=True`) — each run gets its own `XDG_DATA_HOME`, so concurrent runs don't share `opencode.db` and serialise on SQLite write locks during tool execution.
- **Automatic retry** (`async_run(max_retries=2, retry_delay_s=1.0)`) — retries known SQLite-startup crashes with short backoff. Non-SQLite failures still fail fast.

To opt out: pass `startup_delay_s=0` (and a large `startup_concurrency`) to drop the startup pacing, `isolate_db=False` to share session history across runs, and `max_retries=0` to disable retries.

> These notes apply to `async_run` / `async_stream` (run mode). For **multi-turn conversations** use [`OpenCodeSession`](#multi-turn-conversation-opencodesession) instead — it runs `opencode serve` and re-prompts one server-side session, so context is preserved natively with no shared-DB contention.

## Notes

- Event shapes from `--format json` may change between OpenCode versions; unknown fields are preserved in each parsed dict.
- For fully non-interactive automation, prefer explicit `permission` (`allow`/`deny`) over relying on interactive `ask` prompts.
