"""Async client: spawn ``opencode run --format json`` and stream parsed events."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from opencode_wrapper.config import RunConfig, validate_config_for_run
from opencode_wrapper.errors import (
    OpenCodeBinaryNotFoundError,
    OpenCodeProcessError,
    OpenCodeTimeoutError,
)
from opencode_wrapper.events import RunResult, aggregate_run_result, parse_event_line


def resolve_binary(binary: str) -> str:
    """Resolve ``binary`` to an executable path."""
    expanded = Path(binary).expanduser()
    if expanded.is_file():
        return str(expanded)
    found = shutil.which(binary)
    if found:
        return found
    raise OpenCodeBinaryNotFoundError(f"OpenCode binary not found: {binary!r}")


def build_argv(
    binary_resolved: str,
    prompt: str,
    run_cfg: RunConfig,
) -> list[str]:
    """Build ``opencode run`` argument list."""
    cmd: list[str] = [binary_resolved, "run", "--format", "json"]

    if run_cfg.print_logs:
        cmd.append("--print-logs")
    if run_cfg.log_level:
        cmd.extend(["--log-level", run_cfg.log_level])
    if run_cfg.command:
        cmd.extend(["--command", run_cfg.command])
    if run_cfg.continue_session:
        cmd.append("--continue")
    if run_cfg.session_id:
        cmd.extend(["--session", run_cfg.session_id])
    if run_cfg.fork:
        cmd.append("--fork")
    if run_cfg.share is True:
        cmd.append("--share")
    if run_cfg.model:
        cmd.extend(["-m", run_cfg.model])
    if run_cfg.agent:
        cmd.extend(["--agent", run_cfg.agent])
    for f in run_cfg.files:
        cmd.extend(["-f", str(f)])
    if run_cfg.title:
        cmd.extend(["--title", run_cfg.title])
    if run_cfg.attach:
        cmd.extend(["--attach", run_cfg.attach])
    if run_cfg.password:
        cmd.extend(["-p", run_cfg.password])
    if run_cfg.remote_dir:
        cmd.extend(["--dir", run_cfg.remote_dir])
    if run_cfg.port is not None:
        cmd.extend(["--port", str(run_cfg.port)])
    if run_cfg.variant:
        cmd.extend(["--variant", run_cfg.variant])
    if run_cfg.thinking is True:
        cmd.append("--thinking")

    if prompt:
        cmd.append(prompt)
    return cmd


def build_env(run_cfg: RunConfig, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    if run_cfg.extra_env:
        env.update(dict(run_cfg.extra_env))
    content = run_cfg.opencode_config_content_json()
    if content is not None:
        env["OPENCODE_CONFIG_CONTENT"] = content
    if run_cfg.disable_autoupdate:
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    return env


async def _readline_unlimited(reader: asyncio.StreamReader) -> bytes:
    """readline with no size limit, works around asyncio's default 64 KiB cap.

    Uses ``readuntil()`` directly instead of ``readline()``: unlike ``readline()``,
    ``readuntil()`` raises ``LimitOverrunError`` *without* clearing the buffer, so
    we can drain the oversized chunk with ``readexactly()`` and keep looping.
    """
    chunks: list[bytes] = []
    while True:
        try:
            chunk = await reader.readuntil(b"\n")
            if chunks:
                chunks.append(chunk)
                return b"".join(chunks)
            return chunk
        except asyncio.IncompleteReadError as exc:
            # EOF reached before newline — return whatever partial data we have
            if chunks:
                chunks.append(exc.partial)
                return b"".join(chunks)
            return exc.partial
        except asyncio.LimitOverrunError as exc:
            # Buffer limit hit but data is still intact; drain consumed bytes and loop
            chunks.append(bytes(await reader.readexactly(exc.consumed)))


async def _drain_stderr(proc: asyncio.subprocess.Process, out: list[str]) -> None:
    if proc.stderr is None:
        return
    while True:
        chunk = await _readline_unlimited(proc.stderr)
        if not chunk:
            break
        out.append(chunk.decode(errors="replace"))


async def _stdout_line_event_iter(
    proc: asyncio.subprocess.Process,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    if proc.stdout is None:
        return
    while True:
        line_b = await _readline_unlimited(proc.stdout)
        if not line_b:
            break
        line = line_b.decode(errors="replace")
        yield line, parse_event_line(line)


# Substrings that indicate opencode crashed during SQLite WAL initialisation.
# This happens when multiple instances race to set journal_mode=WAL before
# busy_timeout is configured (opencode bug: busy_timeout set after WAL pragma).
_SQLITE_STARTUP_PATTERNS: tuple[str, ...] = (
    "database is locked",
    "sqlite_busy",
    "sqliteerror",
    "journal_mode",
    "disk i/o error",
)


def _is_sqlite_startup_error(stderr: str) -> bool:
    """Return True when *stderr* looks like an opencode SQLite initialisation crash."""
    lower = stderr.lower()
    return any(pat in lower for pat in _SQLITE_STARTUP_PATTERNS)


class AsyncOpenCodeClient:
    """
    One-shot async wrapper around the OpenCode CLI.

    Uses ``opencode run --format json`` with optional ``OPENCODE_CONFIG_CONTENT``.

    Parameters
    ----------
    startup_concurrency:
        Maximum number of opencode processes that may enter their SQLite
        initialisation window simultaneously.  Defaults to ``1`` (serialised
        startup) to avoid the WAL-pragma race that crashes concurrent instances.
    startup_delay_s:
        Seconds to hold the startup semaphore *after* the process is spawned,
        giving SQLite time to finish ``PRAGMA journal_mode = WAL`` before the
        next instance starts.  Defaults to ``0.3``.
    isolate_db:
        If ``True`` (default), each run gets a private ``XDG_DATA_HOME`` temp
        directory so opencode stores its SQLite database in isolation.  Without
        this, concurrent processes share ``~/.local/share/opencode/opencode.db``
        and SQLite write locks during tool execution serialize otherwise-parallel
        runs (observed 37–46 s delays).  Set to ``False`` only if you need runs
        to share session history.
    """

    def __init__(
        self,
        binary: str = "opencode",
        startup_concurrency: int = 1,
        startup_delay_s: float = 0.3,
        isolate_db: bool = True,
    ) -> None:
        self.binary = binary
        self._resolved_binary: str | None = None
        self._startup_sem = asyncio.Semaphore(startup_concurrency)
        self._startup_delay_s = startup_delay_s
        self._isolate_db = isolate_db

    def resolved_binary(self) -> str:
        if self._resolved_binary is None:
            self._resolved_binary = resolve_binary(self.binary)
        return self._resolved_binary

    @asynccontextmanager
    async def _managed_process(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
    ) -> AsyncIterator[tuple[asyncio.subprocess.Process, list[str]]]:
        stderr_lines: list[str] = []
        # Give each process its own XDG_DATA_HOME so opencode.db is isolated.
        # Without this, all concurrent processes share ~/.local/share/opencode/opencode.db
        # and SQLite write locks during tool execution serialize the runs (37–46s delays).
        if self._isolate_db:
            xdg_tmpdir = tempfile.mkdtemp(prefix="oc_xdg_")
            env = {**env, "XDG_DATA_HOME": xdg_tmpdir}
        else:
            xdg_tmpdir = None
        # Serialise process startup to avoid the SQLite WAL-pragma race.
        # The semaphore is released as soon as the startup window has elapsed,
        # so all processes run concurrently after their individual delay.
        async with self._startup_sem:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            if self._startup_delay_s > 0:
                await asyncio.sleep(self._startup_delay_s)
        stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_lines))
        try:
            yield proc, stderr_lines
        except asyncio.CancelledError:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise
        finally:
            # Natural completion: child usually still has returncode=None until wait().
            await proc.wait()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            if xdg_tmpdir is not None:
                shutil.rmtree(xdg_tmpdir, ignore_errors=True)

    async def async_stream(
        self,
        prompt: str,
        workspace: str | Path,
        *,
        run_cfg: RunConfig | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Yield parsed JSON event dicts from stdout.

        After the stream completes successfully, returns normally.
        On non-zero exit, raises :class:`OpenCodeProcessError` (after all lines are yielded).
        """
        run_cfg = run_cfg or RunConfig()
        validate_config_for_run(run_cfg)
        bin_path = self.resolved_binary()
        argv = build_argv(bin_path, prompt, run_cfg)
        env = build_env(run_cfg)
        cwd = str(Path(workspace).expanduser().resolve())

        events_acc: list[dict[str, Any]] = []
        raw_acc: list[str] = []

        async with self._managed_process(argv, cwd, env) as (proc, stderr_lines):
            async for line, ev in _stdout_line_event_iter(proc):
                raw_acc.append(line)
                events_acc.append(ev)
                yield ev

        code = proc.returncode if proc.returncode is not None else -1
        stderr = "".join(stderr_lines)
        if code != 0:
            raise OpenCodeProcessError(
                exit_code=code,
                stderr=stderr,
                events=events_acc,
                raw_stdout_lines=raw_acc,
            )

    async def async_run(
        self,
        prompt: str,
        workspace: str | Path,
        *,
        run_cfg: RunConfig | None = None,
        timeout_s: float | None = None,
        log_file: str | Path | None = None,
        max_retries: int = 2,
        retry_delay_s: float = 1.0,
    ) -> RunResult:
        """
        Run to completion and return a :class:`RunResult`.

        If ``log_file`` is given, each event dict is appended as a JSON line
        during execution (flushed immediately), so partial progress survives
        crashes.

        Raises :class:`OpenCodeTimeoutError` if ``timeout_s`` elapses.

        Parameters
        ----------
        max_retries:
            Number of additional attempts when opencode crashes during SQLite
            startup (WAL-pragma race).  Set to ``0`` to disable retry.
        retry_delay_s:
            Seconds to wait between retry attempts.
        """
        run_cfg = run_cfg or RunConfig()

        async def _inner() -> RunResult:
            validate_config_for_run(run_cfg)
            bin_path = self.resolved_binary()
            argv = build_argv(bin_path, prompt, run_cfg)
            env = build_env(run_cfg)
            cwd = str(Path(workspace).expanduser().resolve())

            events_acc: list[dict[str, Any]] = []
            raw_acc: list[str] = []

            log_fh = open(log_file, "w") if log_file is not None else None
            try:
                async with self._managed_process(argv, cwd, env) as (proc, stderr_lines):
                    async for line, ev in _stdout_line_event_iter(proc):
                        raw_acc.append(line)
                        events_acc.append(ev)
                        if log_fh is not None:
                            log_fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                            log_fh.flush()
            finally:
                if log_fh is not None:
                    log_fh.close()

            code = proc.returncode if proc.returncode is not None else -1
            stderr = "".join(stderr_lines)
            if code != 0:
                raise OpenCodeProcessError(
                    exit_code=code,
                    stderr=stderr,
                    events=events_acc,
                    raw_stdout_lines=raw_acc,
                )
            return aggregate_run_result(
                events=events_acc,
                raw_stdout_lines=raw_acc,
                exit_code=code,
                stderr=stderr,
            )

        async def _run_with_retries() -> RunResult:
            last_exc: OpenCodeProcessError | None = None
            for attempt in range(1 + max_retries):
                if attempt > 0:
                    await asyncio.sleep(retry_delay_s)
                try:
                    return await _inner()
                except OpenCodeProcessError as exc:
                    if attempt < max_retries and _is_sqlite_startup_error(exc.stderr):
                        last_exc = exc
                        continue
                    raise
            raise last_exc  # type: ignore[misc]  # unreachable; satisfies type checker

        if timeout_s is not None:
            try:
                return await asyncio.wait_for(_run_with_retries(), timeout=timeout_s)
            except asyncio.TimeoutError as e:
                raise OpenCodeTimeoutError(
                    f"OpenCode run exceeded timeout_s={timeout_s!r}"
                ) from e
        return await _run_with_retries()
