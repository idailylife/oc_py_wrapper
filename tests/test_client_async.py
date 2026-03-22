"""Async client tests with mocked subprocess."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_wrapper.client import AsyncOpenCodeClient, build_argv, build_env, resolve_binary
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


def test_build_argv_with_agent_model_files() -> None:
    cfg = RunConfig(agent="plan", model="anthropic/claude-3-5-haiku-20241022", files=(Path("a.txt"),))
    argv = build_argv("/x/opencode", "p", cfg)
    assert "--agent" in argv
    assert "plan" in argv
    assert "-m" in argv
    assert "-f" in argv


def test_build_env_config_content_and_autoupdate() -> None:
    cfg = RunConfig(permission={"bash": "deny"}, disable_autoupdate=True)
    env = build_env(cfg, base={"HOME": "/tmp"})
    assert "OPENCODE_CONFIG_CONTENT" in env
    assert "bash" in env["OPENCODE_CONFIG_CONTENT"]
    assert env.get("OPENCODE_DISABLE_AUTOUPDATE") == "1"


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._q = list(lines)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if not self._q:
            return b""
        return self._q.pop(0)


class _FakeStderr:
    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return b""


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
