"""Async client tests with mocked subprocess."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from opencode_wrapper.client import (
    AsyncOpenCodeClient,
    _is_sqlite_startup_error,
    _readline_unlimited,
    build_argv,
    build_env,
    resolve_binary,
)
from opencode_wrapper.config import RunConfig
from opencode_wrapper.errors import OpenCodeBinaryNotFoundError, OpenCodeProcessError, OpenCodeTimeoutError


def test_resolve_binary_uses_which_for_bare_name(tmp_path, monkeypatch) -> None:
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_binary("opencode") == str(fake)


def test_resolve_binary_missing_raises() -> None:
    with pytest.raises(OpenCodeBinaryNotFoundError):
        resolve_binary("definitely-no-such-opencode-binary-xyz")


def test_build_argv_minimal() -> None:
    cfg = RunConfig()
    argv = build_argv("/bin/opencode", "hello", cfg)
    assert argv[:4] == ["/bin/opencode", "run", "--format", "json"]
    assert argv[-1] == "hello"


def test_build_argv_with_agent_model_and_file_cli_kwarg() -> None:
    cfg = RunConfig(
        agent="plan",
        model="anthropic/claude-3-5-haiku-20241022",
        cli_kwargs={"f": ("a.txt",)},
    )
    argv = build_argv("/x/opencode", "p", cfg)
    assert "--agent" in argv
    assert "plan" in argv
    assert "-m" in argv
    # single-char key -> "-f a.txt" (separate argv entries)
    assert argv[argv.index("-f") + 1] == "a.txt"


def test_build_argv_thinking_cli_kwarg_adds_display_flag() -> None:
    cfg = RunConfig(cli_kwargs={"thinking": True})
    argv = build_argv("/x/opencode", "p", cfg)
    assert "--thinking" in argv


def test_build_argv_cli_kwargs_bool_true_emits_bare_flag() -> None:
    argv = build_argv("/x/opencode", "p", RunConfig(cli_kwargs={"fork": True}))
    assert "--fork" in argv


def test_build_argv_cli_kwargs_value_emits_long_flag_eq() -> None:
    argv = build_argv("/x/opencode", "p", RunConfig(cli_kwargs={"title": "demo"}))
    assert "--title=demo" in argv


def test_build_argv_cli_kwargs_single_char_value_uses_separate_args() -> None:
    argv = build_argv("/x/opencode", "p", RunConfig(cli_kwargs={"p": "secret"}))
    assert argv[argv.index("-p") + 1] == "secret"


def test_build_argv_cli_kwargs_list_repeats_flag() -> None:
    argv = build_argv("/x/opencode", "p", RunConfig(cli_kwargs={"f": ["a.txt", "b.txt"]}))
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-f"] == ["a.txt", "b.txt"]


def test_build_argv_cli_kwargs_false_and_none_skipped() -> None:
    argv = build_argv(
        "/x/opencode", "p", RunConfig(cli_kwargs={"fork": False, "title": None})
    )
    assert "--fork" not in argv
    assert not any(a.startswith("--title") for a in argv)


def test_build_argv_cli_kwargs_coexist_with_model_agent_order() -> None:
    cfg = RunConfig(
        model="anthropic/claude",
        cli_kwargs={"fork": True, "title": "demo", "session": "ses_1", "f": ["a.txt"]},
    )
    argv = build_argv("opencode", "hi", cfg)
    assert argv == [
        "opencode", "run", "--format", "json",
        "-m", "anthropic/claude",
        "--fork", "--title=demo", "--session=ses_1", "-f", "a.txt",
        "hi",
    ]


def test_build_env_config_content_and_autoupdate() -> None:
    cfg = RunConfig(permission={"bash": "deny"}, disable_autoupdate=True)
    env = build_env(cfg, base={"HOME": "/tmp"})
    assert "OPENCODE_CONFIG_CONTENT" in env
    assert "bash" in env["OPENCODE_CONFIG_CONTENT"]
    assert env.get("OPENCODE_DISABLE_AUTOUPDATE") == "1"


def test_build_env_sets_pwd_to_cwd() -> None:
    """opencode's run.ts:276 reads `process.env.PWD ?? process.cwd()` to
    resolve the project root, and the bash builtin pwd reads $PWD.
    asyncio.create_subprocess_exec(cwd=...) only chdirs the child; PWD stays
    inherited from the parent shell.  build_env must pin PWD to the workspace
    so opencode and bash both see a consistent cwd."""
    cfg = RunConfig()
    env = build_env(cfg, base={"HOME": "/tmp", "PWD": "/some/other/dir"}, cwd="/ws/x")
    assert env["PWD"] == "/ws/x"


def test_build_env_without_cwd_leaves_pwd_untouched() -> None:
    cfg = RunConfig()
    env = build_env(cfg, base={"HOME": "/tmp", "PWD": "/parent"})
    assert env["PWD"] == "/parent"


@pytest.mark.asyncio
async def test_readline_unlimited_normal_line() -> None:
    """Lines within the default 64 KiB limit are returned as-is."""
    reader = asyncio.StreamReader(limit=2**16)
    reader.feed_data(b'{"type":"text","content":"hello"}\n')
    reader.feed_eof()
    assert await _readline_unlimited(reader) == b'{"type":"text","content":"hello"}\n'


@pytest.mark.asyncio
async def test_readline_unlimited_line_exceeds_default_limit() -> None:
    """A line just over the default 64 KiB limit must not raise LimitOverrunError."""
    reader = asyncio.StreamReader(limit=2**16)
    payload = b"x" * (2**16 + 1)  # 65537 bytes — one byte over the default cap
    reader.feed_data(payload + b"\n")
    reader.feed_eof()
    result = await _readline_unlimited(reader)
    assert result == payload + b"\n"


@pytest.mark.asyncio
async def test_readline_unlimited_multi_chunk_large_line() -> None:
    """A line that spans many multiples of the limit is fully reassembled."""
    reader = asyncio.StreamReader(limit=2**16)
    payload = b"y" * (2**16 * 5)  # 320 KiB — requires several iterations
    reader.feed_data(payload + b"\n")
    reader.feed_eof()
    result = await _readline_unlimited(reader)
    assert result == payload + b"\n"


@pytest.mark.asyncio
async def test_readline_unlimited_multiple_lines() -> None:
    """Multiple lines are returned one at a time in order, even when one is oversized."""
    reader = asyncio.StreamReader(limit=2**16)
    small = b"small line\n"
    big = b"z" * (2**16 + 100) + b"\n"
    reader.feed_data(small + big + small)
    reader.feed_eof()
    assert await _readline_unlimited(reader) == small
    assert await _readline_unlimited(reader) == big
    assert await _readline_unlimited(reader) == small


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._q = list(lines)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if not self._q:
            return b""
        return self._q.pop(0)

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        # Test lines are small — readuntil is equivalent to readline here
        return await self.readline()

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("readexactly should not be called for small test lines")


class _FakeStderr:
    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return b""

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        await asyncio.sleep(0)
        return b""

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("readexactly should not be called for small test lines")


class _FakeStderrLines:
    """Fake stderr that returns a fixed list of lines then EOF."""

    def __init__(self, lines: list[bytes]) -> None:
        self._q = list(lines)

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        await asyncio.sleep(0)
        if not self._q:
            return b""
        return self._q.pop(0)

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("readexactly should not be called for small test lines")


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr()
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = 137


@pytest.mark.asyncio
async def test_async_run_success(monkeypatch, tmp_path) -> None:
    proc = _FakeProc([b'{"type":"text","content":"ok"}\n'])
    proc.returncode = 0

    async def fake_exec(*args, **kwargs):
        return proc

    client = AsyncOpenCodeClient(binary=str(tmp_path / "noop"))
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        r = await client.async_run("hi", tmp_path, run_cfg=RunConfig())

    assert r.exit_code == 0
    assert r.final_text == "ok"
    assert len(r.events) == 1


@pytest.mark.asyncio
async def test_async_run_log_exclude_types_drops_events_from_log(monkeypatch, tmp_path) -> None:
    proc = _FakeProc([
        b'{"type":"message.part.delta","properties":{"delta":"o"}}\n',
        b'{"type":"text","content":"ok"}\n',
    ])
    proc.returncode = 0

    async def fake_exec(*args, **kwargs):
        return proc

    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    log_path = tmp_path / "run.jsonl"
    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        r = await client.async_run(
            "hi", tmp_path, run_cfg=RunConfig(), log_file=log_path,
            log_exclude_types={"message.part.delta"},
        )

    logged = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert all(ev["type"] != "message.part.delta" for ev in logged)
    # Delta still retained in the in-memory result.
    assert any(ev["type"] == "message.part.delta" for ev in r.events)


@pytest.mark.asyncio
async def test_async_run_process_error(monkeypatch, tmp_path) -> None:
    proc = _FakeProc([b'{"type":"text","content":"x"}\n'])

    async def wait_fail():
        proc.returncode = 2
        return 2

    proc.wait = wait_fail  # type: ignore[method-assign]

    async def fake_exec(*args, **kwargs):
        return proc

    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with pytest.raises(OpenCodeProcessError) as ei:
            await client.async_run("hi", tmp_path)

    assert ei.value.exit_code == 2
    assert len(ei.value.events) == 1


@pytest.mark.asyncio
async def test_async_run_timeout(monkeypatch, tmp_path) -> None:
    # Process that never finishes stdout (no newline EOF hang)
    class HangStdout:
        async def readline(self) -> bytes:
            await asyncio.sleep(10)
            return b""

        async def readuntil(self, sep: bytes = b"\n") -> bytes:
            await asyncio.sleep(10)
            return b""

    class HangProc:
        def __init__(self) -> None:
            self.stdout = HangStdout()
            self.stderr = _FakeStderr()
            self.returncode: int | None = None

        def kill(self) -> None:
            self.returncode = 137

        async def wait(self) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    proc = HangProc()

    async def fake_exec(*args, **kwargs):
        return proc

    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with pytest.raises(OpenCodeTimeoutError):
            await client.async_run("hi", tmp_path, timeout_s=0.05)


@pytest.mark.asyncio
async def test_async_stream_yields_then_raises_on_bad_exit(monkeypatch, tmp_path) -> None:
    proc = _FakeProc([b'{"type":"text","content":"a"}\n'])

    async def wait_bad():
        proc.returncode = 1
        return 1

    proc.wait = wait_bad  # type: ignore[method-assign]

    async def fake_exec(*args, **kwargs):
        return proc

    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        gen = client.async_stream("hi", tmp_path)
        first = await gen.__anext__()
        assert first["type"] == "text"
        with pytest.raises(OpenCodeProcessError):
            await gen.__anext__()


# ---------------------------------------------------------------------------
# _is_sqlite_startup_error
# ---------------------------------------------------------------------------


def test_is_sqlite_startup_error_detects_locked() -> None:
    assert _is_sqlite_startup_error("SqliteError: database is locked")


def test_is_sqlite_startup_error_detects_busy() -> None:
    assert _is_sqlite_startup_error("error code SQLITE_BUSY returned")


def test_is_sqlite_startup_error_detects_journal_mode() -> None:
    assert _is_sqlite_startup_error("failed to set PRAGMA journal_mode = WAL")


def test_is_sqlite_startup_error_detects_disk_io() -> None:
    assert _is_sqlite_startup_error("SqliteError: disk I/O error")


def test_is_sqlite_startup_error_ignores_unrelated() -> None:
    assert not _is_sqlite_startup_error("Error: file not found")
    assert not _is_sqlite_startup_error("")


# ---------------------------------------------------------------------------
# startup semaphore serialises process creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_semaphore_serialises_launches(monkeypatch, tmp_path) -> None:
    """Concurrent async_run calls must not enter _managed_process simultaneously."""
    launch_times: list[float] = []

    async def fake_exec(*args, **kwargs):
        launch_times.append(asyncio.get_event_loop().time())
        proc = _FakeProc([b'{"type":"text","content":"ok"}\n'])
        proc.returncode = 0
        return proc

    client = AsyncOpenCodeClient(
        binary="opencode",
        startup_concurrency=1,
        startup_delay_s=0.05,
    )
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        await asyncio.gather(
            client.async_run("a", tmp_path, run_cfg=RunConfig()),
            client.async_run("b", tmp_path, run_cfg=RunConfig()),
            client.async_run("c", tmp_path, run_cfg=RunConfig()),
        )

    assert len(launch_times) == 3
    # Each launch must be at least startup_delay_s after the previous one.
    for i in range(1, len(launch_times)):
        gap = launch_times[i] - launch_times[i - 1]
        assert gap >= 0.04, f"launch gap {gap:.3f}s too small — semaphore not working"


# ---------------------------------------------------------------------------
# retry on SQLite startup crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_run_retries_on_sqlite_error(monkeypatch, tmp_path) -> None:
    """async_run retries when opencode crashes with a SQLite startup error."""
    call_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First attempt: simulate SQLite startup crash (exits immediately, no stdout)
            proc = _FakeProc([])

            async def wait_locked():
                proc.returncode = 1
                return 1

            proc.wait = wait_locked  # type: ignore[method-assign]
            proc.stderr = _FakeStderrLines([b"SqliteError: database is locked\n"])
        else:
            # Second attempt: success
            proc = _FakeProc([b'{"type":"text","content":"ok"}\n'])
            proc.returncode = 0
        return proc

    client = AsyncOpenCodeClient(binary="opencode", startup_delay_s=0)
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        result = await client.async_run("hi", tmp_path, run_cfg=RunConfig(), retry_delay_s=0)

    assert call_count == 2
    assert result.final_text == "ok"


@pytest.mark.asyncio
async def test_async_run_does_not_retry_non_sqlite_error(monkeypatch, tmp_path) -> None:
    """Non-SQLite process errors are raised immediately without retrying."""
    call_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        proc = _FakeProc([])

        async def wait_fail():
            proc.returncode = 1
            return 1

        proc.wait = wait_fail  # type: ignore[method-assign]
        proc.stderr = _FakeStderrLines([b"Error: something else went wrong\n"])
        return proc

    client = AsyncOpenCodeClient(binary="opencode", startup_delay_s=0)
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with pytest.raises(OpenCodeProcessError):
            await client.async_run("hi", tmp_path, run_cfg=RunConfig(), retry_delay_s=0)

    assert call_count == 1


@pytest.mark.asyncio
async def test_async_run_raises_after_max_retries_exhausted(monkeypatch, tmp_path) -> None:
    """After max_retries SQLite failures the final error is re-raised."""
    call_count = 0

    async def fake_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        proc = _FakeProc([])

        async def wait_locked():
            proc.returncode = 1
            return 1

        proc.wait = wait_locked  # type: ignore[method-assign]
        proc.stderr = _FakeStderrLines([b"SqliteError: database is locked\n"])
        return proc

    client = AsyncOpenCodeClient(binary="opencode", startup_delay_s=0)
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with pytest.raises(OpenCodeProcessError) as ei:
            await client.async_run(
                "hi", tmp_path, run_cfg=RunConfig(), max_retries=2, retry_delay_s=0
            )

    assert call_count == 3  # 1 initial + 2 retries
    assert "database is locked" in ei.value.stderr
