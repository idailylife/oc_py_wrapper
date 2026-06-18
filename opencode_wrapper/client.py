"""Async client: spawn ``opencode run --format json`` and stream parsed events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping

from opencode_wrapper.config import RunConfig, validate_config_for_run
from opencode_wrapper.config import loads_jsonc, sanitize_user_config_json
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
    """Build ``opencode run`` argument list.

    ``model`` / ``agent`` are structured fields shared with server mode and map
    to ``-m`` / ``--agent``.  Every other ``opencode run`` flag is passed through
    ``run_cfg.cli_kwargs``: each entry expands to ``--flag`` (bool ``True``),
    ``--flag=value`` (multi-char key) / ``-f value`` (single-char key), or a
    repetition per element (list/tuple value).  ``False`` / ``None`` are skipped.
    The argv list is handed to ``create_subprocess_exec`` (no shell), so values
    are not subject to shell injection.
    """
    cmd: list[str] = [binary_resolved, "run", "--format", "json"]
    if run_cfg.model:
        cmd.extend(["-m", run_cfg.model])
    if run_cfg.agent:
        cmd.extend(["--agent", run_cfg.agent])

    for key, val in (run_cfg.cli_kwargs or {}).items():
        flag = f"-{key}" if len(key) == 1 else f"--{key}"
        vals = val if isinstance(val, (list, tuple)) else [val]
        for v in vals:
            if v is True:
                cmd.append(flag)
            elif v is False or v is None:
                continue
            elif len(key) == 1:
                cmd.extend([flag, str(v)])
            else:
                cmd.append(f"{flag}={v}")

    if prompt:
        cmd.append(prompt)
    return cmd


def build_env(
    run_cfg: RunConfig,
    base: Mapping[str, str] | None = None,
    *,
    cwd: str | Path | None = None,
) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    if run_cfg.extra_env:
        env.update(dict(run_cfg.extra_env))
    content = run_cfg.opencode_config_content_json()
    if content is not None:
        env["OPENCODE_CONFIG_CONTENT"] = content
    if run_cfg.disable_autoupdate:
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    # opencode's `run` cmd resolves the project root as
    # `process.env.PWD ?? process.cwd()` (run.ts:276), and the bash builtin
    # `pwd` reads $PWD too.  asyncio.create_subprocess_exec(cwd=...) only
    # chdirs the child; it leaves PWD inherited from the parent shell, which
    # makes opencode operate against the wrong directory.  Pin PWD to the
    # resolved workspace so the child sees a consistent cwd.
    if cwd is not None:
        env["PWD"] = str(cwd)
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


_LOG = logging.getLogger(__name__)

# Filenames opencode reads from its global config dir + ~/.opencode.
# Legacy TOML ("config" without extension) is intentionally skipped — it's rare
# and would require a TOML parser dep; the wrapper aims for stdlib-only.
_GLOBAL_CONFIG_FILENAMES: tuple[str, ...] = (
    "config.json",
    "opencode.json",
    "opencode.jsonc",
)


def _resolve_real_xdg_config_opencode_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path(env.get("HOME", str(Path.home()))).expanduser() / ".config"
    return base / "opencode"


def _resolve_real_home_opencode_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser() / ".opencode"


def _sanitize_and_copy(src_dir: Path, dst_dir: Path) -> None:
    """Read each known opencode config file in *src_dir*, sanitize, write to *dst_dir*.

    Strict JSON files parse on the fast path; JSONC (comments, trailing commas)
    falls back to ``loads_jsonc``.  Files that aren't valid in either form are
    skipped with a warning — for benchmark reproducibility the wrapper would
    rather hide a file it can't safely strip than risk leaking capability keys.
    Missing source files are silently skipped.  ``dst_dir`` is always created
    so opencode finds the directory (even if empty).
    """
    from opencode_wrapper.config import sanitize_user_config_json

    dst_dir.mkdir(parents=True, exist_ok=True)
    if not src_dir.is_dir():
        return
    for fname in _GLOBAL_CONFIG_FILENAMES:
        src = src_dir / fname
        if not src.is_file():
            continue
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as exc:
            _LOG.warning("user-config isolation: cannot read %s: %s", src, exc)
            continue
        try:
            raw = loads_jsonc(text)
        except json.JSONDecodeError as exc:
            _LOG.warning(
                "user-config isolation: skipping %s (not parseable as JSON or JSONC): %s",
                src, exc,
            )
            continue
        if not isinstance(raw, dict):
            _LOG.warning(
                "user-config isolation: skipping %s (root is not a JSON object)", src
            )
            continue
        sanitized = sanitize_user_config_json(raw)
        (dst_dir / fname).write_text(
            json.dumps(sanitized, ensure_ascii=False), encoding="utf-8"
        )


def _isolate_user_config(env: dict[str, str], tmp_root: Path) -> dict[str, str]:
    """Mutate *env* so opencode sees a sanitized copy of the user's global config.

    Reads the real ``$XDG_CONFIG_HOME/opencode`` and ``$HOME/.opencode``,
    filters each file through ``sanitize_user_config_json`` (keeping only
    ``provider`` / ``disabled_providers`` / ``enabled_providers`` / ``$schema``),
    writes the results under *tmp_root*, and points ``XDG_CONFIG_HOME`` /
    ``OPENCODE_TEST_HOME`` at those tmpdir locations.  Strips
    ``OPENCODE_CONFIG`` / ``OPENCODE_CONFIG_DIR`` so the parent shell can't
    re-introduce extras.  Project-level config (cwd walk + ``.opencode/``)
    is untouched.
    """
    iso_xdg = tmp_root / "xdg"
    iso_home = tmp_root / "home"

    _sanitize_and_copy(_resolve_real_xdg_config_opencode_dir(env), iso_xdg / "opencode")
    _sanitize_and_copy(_resolve_real_home_opencode_dir(env), iso_home / ".opencode")

    env["XDG_CONFIG_HOME"] = str(iso_xdg)
    env["OPENCODE_TEST_HOME"] = str(iso_home)
    env.pop("OPENCODE_CONFIG", None)
    env.pop("OPENCODE_CONFIG_DIR", None)
    return env


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
        run_cfg: RunConfig,
        data_home: str | None = None,
    ) -> AsyncIterator[tuple[asyncio.subprocess.Process, list[str]]]:
        stderr_lines: list[str] = []
        cleanup_tmpdirs: list[str] = []
        # Give each process its own XDG_DATA_HOME so opencode.db is isolated.
        # Without this, all concurrent processes share ~/.local/share/opencode/opencode.db
        # and SQLite write locks during tool execution serialize the runs (37–46s delays).
        # When *data_home* is provided the caller owns a persistent dir (e.g. an
        # OpenCodeSession reusing one DB across turns), so it is NOT added to
        # cleanup_tmpdirs — the caller deletes it when done.
        managed = data_home is not None
        if self._isolate_db or managed:
            if managed:
                xdg_tmpdir = data_home  # type: ignore[assignment]
            else:
                xdg_tmpdir = tempfile.mkdtemp(prefix="oc_xdg_")
                cleanup_tmpdirs.append(xdg_tmpdir)
            # Symlink auth.json so provider API keys (stored by `opencode auth`)
            # are visible in the isolated data dir.  Without this, providers
            # that rely on auth.json (rather than env-var keys) fail with
            # "Model not found" because the provider never activates.
            real_xdg = env.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
            real_auth = Path(real_xdg) / "opencode" / "auth.json"
            if real_auth.is_file():
                iso_oc_dir = Path(xdg_tmpdir) / "opencode"
                iso_oc_dir.mkdir(parents=True, exist_ok=True)
                link = iso_oc_dir / "auth.json"
                if not link.exists():  # reused across turns — guard re-symlink
                    link.symlink_to(real_auth)
            env = {**env, "XDG_DATA_HOME": xdg_tmpdir}
        # When the caller has not opted into host-config inheritance (the
        # default), redirect XDG_CONFIG_HOME / OPENCODE_TEST_HOME at a sanitized
        # tmpdir copy of the user's global config — only provider settings are
        # carried over, all capability keys (mcp / agent / command / tools /
        # plugin / skills / instructions / permission / model / ...) are stripped.
        # Project-level config (cwd walk + .opencode/) is untouched.
        if not run_cfg.inherit_user_config:
            cfg_tmpdir = tempfile.mkdtemp(prefix="oc_cfg_")
            cleanup_tmpdirs.append(cfg_tmpdir)
            env = _isolate_user_config(dict(env), Path(cfg_tmpdir))
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
            for path in cleanup_tmpdirs:
                shutil.rmtree(path, ignore_errors=True)

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
        cwd = str(Path(workspace).expanduser().resolve())
        env = build_env(run_cfg, cwd=cwd)

        events_acc: list[dict[str, Any]] = []
        raw_acc: list[str] = []

        async with self._managed_process(argv, cwd, env, run_cfg) as (proc, stderr_lines):
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
        log_exclude_types: Iterable[str] | None = None,
        max_retries: int = 2,
        retry_delay_s: float = 1.0,
        data_home: str | None = None,
    ) -> RunResult:
        """
        Run to completion and return a :class:`RunResult`.

        If ``log_file`` is given, each event dict is appended as a JSON line
        during execution (flushed immediately), so partial progress survives
        crashes.

        Raises :class:`OpenCodeTimeoutError` if ``timeout_s`` elapses.

        Parameters
        ----------
        log_exclude_types:
            Optional collection of event ``type`` values to omit from
            ``log_file`` (e.g. ``{"message.part.delta"}`` to keep streaming
            chunks off disk).  Excluded events are still returned in
            ``RunResult.events``.  ``None`` (the default) logs every event.
        max_retries:
            Number of additional attempts when opencode crashes during SQLite
            startup (WAL-pragma race).  Set to ``0`` to disable retry.
        retry_delay_s:
            Seconds to wait between retry attempts.
        """
        run_cfg = run_cfg or RunConfig()
        exclude_types = frozenset(log_exclude_types or ())

        async def _inner() -> RunResult:
            validate_config_for_run(run_cfg)
            bin_path = self.resolved_binary()
            argv = build_argv(bin_path, prompt, run_cfg)
            cwd = str(Path(workspace).expanduser().resolve())
            env = build_env(run_cfg, cwd=cwd)

            events_acc: list[dict[str, Any]] = []
            raw_acc: list[str] = []

            log_fh = open(log_file, "w") if log_file is not None else None
            try:
                async with self._managed_process(argv, cwd, env, run_cfg, data_home=data_home) as (proc, stderr_lines):
                    async for line, ev in _stdout_line_event_iter(proc):
                        raw_acc.append(line)
                        events_acc.append(ev)
                        if log_fh is not None and ev.get("type") not in exclude_types:
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
