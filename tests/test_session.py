"""Unit tests for OpenCodeSession multi-turn behaviour (mocked subprocess)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_wrapper import AsyncOpenCodeClient, OpenCodeSession, RunConfig
from opencode_wrapper.events import aggregate_run_result


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._q = list(lines)

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        await asyncio.sleep(0)
        if not self._q:
            return b""
        return self._q.pop(0)

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("readexactly should not be called for small test lines")


class _FakeStderr:
    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        await asyncio.sleep(0)
        return b""

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("readexactly should not be called for small test lines")


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr()
        self.returncode: int | None = 0

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = 137


def _line(sid: str) -> bytes:
    return (
        '{"type":"text","sessionID":"%s","part":{"type":"text","text":"ok"}}\n' % sid
    ).encode()


def test_run_result_extracts_session_id() -> None:
    events = [
        {"type": "step_start", "sessionID": "ses_abc", "part": {"sessionID": "ses_abc"}},
        {"type": "text", "part": {"type": "text", "text": "hi"}},
    ]
    r = aggregate_run_result(events=events, raw_stdout_lines=[], exit_code=0, stderr="")
    assert r.session_id == "ses_abc"


@pytest.mark.asyncio
async def test_session_injects_session_id_across_turns(monkeypatch, tmp_path) -> None:
    procs = [_FakeProc([_line("ses_x")]), _FakeProc([_line("ses_x")])]
    captured_argv: list[list[str]] = []
    captured_data_home: list[str | None] = []

    async def fake_exec(*args, **kwargs):
        captured_argv.append(list(args))
        captured_data_home.append(kwargs.get("env", {}).get("XDG_DATA_HOME"))
        return procs.pop(0)

    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig()) as s:
            data_home = s._data_home
            assert data_home is not None and Path(data_home).is_dir()
            r1 = await s.send("turn 1")
            assert s.session_id == "ses_x"
            r2 = await s.send("turn 2")

    assert r1.session_id == "ses_x"
    assert r2.session_id == "ses_x"
    # Turn 1: no --session; turn 2: continues ses_x.
    assert "--session" not in captured_argv[0]
    assert "--session" in captured_argv[1]
    assert "ses_x" in captured_argv[1]
    # Same private data dir reused across turns; removed after context exit.
    assert captured_data_home[0] == data_home
    assert captured_data_home[1] == data_home
    assert not Path(data_home).exists()


@pytest.mark.asyncio
async def test_send_outside_context_raises(tmp_path) -> None:
    client = AsyncOpenCodeClient(binary="opencode")
    s = OpenCodeSession(client, tmp_path)
    with pytest.raises(RuntimeError):
        await s.send("nope")
