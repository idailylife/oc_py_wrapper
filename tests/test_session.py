"""Unit tests for OpenCodeSession multi-turn behaviour (mocked server transport).

OpenCodeSession now drives an ``opencode serve`` process via :class:`_OpenCodeServer`.
These tests replace that class with a scriptable in-memory fake that feeds canned
SSE events onto the per-session queue and records HTTP posts, so the session's
event-loop contract can be exercised without spawning a real server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from opencode_wrapper import AsyncOpenCodeClient, OpenCodeSession, RunConfig
from opencode_wrapper.events import aggregate_run_result

SID = "ses_test"


def _text_turn(text: str, sid: str = SID) -> list[dict[str, Any]]:
    return [
        {
            "type": "message.part.updated",
            "properties": {"sessionID": sid, "part": {"type": "text", "id": "prt_1", "text": text}},
        },
        {"type": "session.idle", "properties": {"sessionID": sid}},
    ]


def _final_messages(text: str) -> list[dict[str, Any]]:
    return [{"info": {"role": "assistant", "id": "msg_1"}, "parts": [{"type": "text", "text": text}]}]


class _FakeServer:
    """Drop-in for ``_OpenCodeServer`` that scripts events per prompt turn."""

    def __init__(self, binary: str, run_cfg: RunConfig, workspace: str) -> None:
        self.binary = binary
        self.run_cfg = run_cfg
        self.workspace = workspace
        self.queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.posts: list[tuple[str, Any]] = []
        self.permission_answers: list[str] = []
        self.question_replies: list[Any] = []
        self.question_rejects: list[str] = []
        self.turn_events: list[list[dict[str, Any]]] = []
        self.final_messages_by_turn: list[list[dict[str, Any]]] = []
        self._turn_idx = 0
        self.closed = False
        self.deleted: list[str] = []

    @property
    def stderr_tail(self) -> str:
        return ""

    async def start(self) -> None:
        pass

    def subscribe(self, session_id: str) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.queues[session_id] = q
        return q

    def unsubscribe(self, session_id: str) -> None:
        self.queues.pop(session_id, None)

    async def post(self, path: str, body: Any = None) -> Any:
        self.posts.append((path, body))
        if path.startswith("/session?directory="):
            return {"id": SID}
        if "/prompt_async" in path:
            sid = path.split("/session/", 1)[1].split("/", 1)[0].split("?", 1)[0]
            evs = self.turn_events[self._turn_idx] if self._turn_idx < len(self.turn_events) else []
            self._turn_idx += 1
            q = self.queues[sid]
            for ev in evs:
                q.put_nowait(ev)
            return None
        if "/permissions/" in path:
            self.permission_answers.append(body.get("response"))
            return None
        if path.startswith("/question/") and "/reply" in path:
            qid = path.split("/question/", 1)[1].split("/", 1)[0]
            self.question_replies.append((qid, body.get("answers")))
            return True
        if path.startswith("/question/") and "/reject" in path:
            qid = path.split("/question/", 1)[1].split("/", 1)[0]
            self.question_rejects.append(qid)
            return True
        return None

    async def get(self, path: str) -> Any:
        idx = self._turn_idx - 1
        if 0 <= idx < len(self.final_messages_by_turn):
            return self.final_messages_by_turn[idx]
        return []

    async def delete(self, path: str) -> Any:
        self.deleted.append(path)
        return None

    async def aclose(self) -> None:
        self.closed = True


def _install_fake(monkeypatch) -> dict[str, _FakeServer]:
    """Patch session._OpenCodeServer with the fake; capture the instance created."""
    holder: dict[str, _FakeServer] = {}

    def factory(binary: str, run_cfg: RunConfig, workspace: str) -> _FakeServer:
        srv = _FakeServer(binary, run_cfg, workspace)
        holder["server"] = srv
        return srv

    monkeypatch.setattr("opencode_wrapper.session._OpenCodeServer", factory)
    return holder


def test_run_result_extracts_session_id() -> None:
    events = [
        {"type": "step_start", "sessionID": "ses_abc", "part": {"sessionID": "ses_abc"}},
        {"type": "text", "part": {"type": "text", "text": "hi"}},
    ]
    r = aggregate_run_result(events=events, raw_stdout_lines=[], exit_code=0, stderr="")
    assert r.session_id == "ses_abc"


@pytest.mark.asyncio
async def test_session_multi_turn_continuity(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig()) as s:
        srv = holder["server"]
        srv.turn_events = [_text_turn("hi Bob"), _text_turn("Bob")]
        srv.final_messages_by_turn = [_final_messages("hi Bob"), _final_messages("Bob")]

        assert s.session_id == SID
        r1 = await s.send("My name is Bob.")
        r2 = await s.send("What is my name?")

    assert r1.session_id == SID and r2.session_id == SID
    assert r1.final_text == "hi Bob"
    assert r2.final_text == "Bob"
    # One server, one session, re-prompted: exactly one session create.
    creates = [p for p, _ in srv.posts if p.startswith("/session?directory=")]
    prompts = [p for p, _ in srv.posts if "/prompt_async" in p]
    assert len(creates) == 1
    assert len(prompts) == 2
    # Server and session torn down on exit.
    assert srv.closed is True
    assert any(f"/session/{SID}" == d for d in srv.deleted)


@pytest.mark.asyncio
async def test_log_file_accumulates_events_across_turns(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    log_path = tmp_path / "sess.jsonl"
    turn1, turn2 = _text_turn("hi Bob"), _text_turn("Bob")

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig(), log_file=log_path) as s:
        srv = holder["server"]
        srv.turn_events = [turn1, turn2]
        srv.final_messages_by_turn = [_final_messages("hi Bob"), _final_messages("Bob")]
        r1 = await s.send("My name is Bob.")
        r2 = await s.send("What is my name?")

    lines = log_path.read_text().splitlines()
    logged = [json.loads(line) for line in lines]
    # Every event from both turns, in order, appended (not truncated per turn).
    assert logged == turn1 + turn2
    # Same dicts that land in each turn's result.events.
    assert logged == r1.events + r2.events


@pytest.mark.asyncio
async def test_log_exclude_types_drops_events_from_log_but_keeps_in_result(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    log_path = tmp_path / "sess.jsonl"
    turn = [
        {"type": "message.part.delta", "properties": {"delta": "hi"}},
        {"type": "message.part.delta", "properties": {"delta": " Bob"}},
        *_text_turn("hi Bob"),
    ]

    async with OpenCodeSession(
        client, tmp_path, run_cfg=RunConfig(), log_file=log_path,
        log_exclude_types={"message.part.delta"},
    ) as s:
        srv = holder["server"]
        srv.turn_events = [turn]
        srv.final_messages_by_turn = [_final_messages("hi Bob")]
        r = await s.send("hello")

    logged = [json.loads(line) for line in log_path.read_text().splitlines()]
    # Deltas dropped from the on-disk log...
    assert all(ev["type"] != "message.part.delta" for ev in logged)
    # ...but still present in the in-memory result.
    assert any(ev["type"] == "message.part.delta" for ev in r.events)


@pytest.mark.asyncio
async def test_aexit_swallows_aclose_failure_and_closes_log(monkeypatch, tmp_path) -> None:
    """Teardown errors (e.g. a subprocess-reap race) must not escape __aexit__.

    Regression for concurrent-session shutdown where aclose() raised
    ProcessLookupError and crashed the runner's asyncio.gather().
    """
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    log_path = tmp_path / "sess.jsonl"
    session = OpenCodeSession(client, tmp_path, run_cfg=RunConfig(), log_file=log_path)
    await session.__aenter__()
    srv = holder["server"]

    async def _boom() -> None:
        raise ProcessLookupError

    monkeypatch.setattr(srv, "aclose", _boom)

    # Must not raise despite aclose() blowing up mid-teardown.
    await session.__aexit__(None, None, None)

    assert session._log_fh is None  # finally still closed the log file
    assert session._server is None


@pytest.mark.asyncio
async def test_no_log_file_writes_nothing(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig()) as s:
        assert s._log_fh is None
        srv = holder["server"]
        srv.turn_events = [_text_turn("x")]
        await s.send("turn 1")

    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.asyncio
async def test_per_turn_model_override_in_prompt_body(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig(model="opencode/big-pickle")) as s:
        srv = holder["server"]
        srv.turn_events = [_text_turn("x"), _text_turn("y")]
        await s.send("turn 1")
        await s.send("turn 2", run_cfg=RunConfig(model="anthropic/claude"))

    prompt_bodies = [b for p, b in srv.posts if "/prompt_async" in p]
    assert prompt_bodies[0]["model"] == {"providerID": "opencode", "modelID": "big-pickle"}
    assert prompt_bodies[1]["model"] == {"providerID": "anthropic", "modelID": "claude"}


@pytest.mark.asyncio
async def test_permission_callback_invoked(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    seen: list[dict[str, Any]] = []

    async def approve(props: dict[str, Any]) -> str:
        seen.append(props)
        return "once"

    async with OpenCodeSession(
        client, tmp_path, run_cfg=RunConfig(), on_permission=approve
    ) as s:
        srv = holder["server"]
        srv.turn_events = [
            [
                {
                    "type": "permission.asked",
                    "properties": {"sessionID": SID, "id": "per_1", "permission": "bash"},
                },
                *_text_turn("done"),
            ]
        ]
        srv.final_messages_by_turn = [_final_messages("done")]
        r = await s.send("run echo")

    assert r.final_text == "done"
    assert len(seen) == 1 and seen[0]["id"] == "per_1"
    assert srv.permission_answers == ["once"]


@pytest.mark.asyncio
async def test_permission_default_reject(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig()) as s:
        srv = holder["server"]
        srv.turn_events = [
            [
                {
                    "type": "permission.asked",
                    "properties": {"sessionID": SID, "id": "per_9", "permission": "bash"},
                },
                *_text_turn("ok"),
            ]
        ]
        await s.send("run echo")

    assert srv.permission_answers == ["reject"]


@pytest.mark.asyncio
async def test_question_callback_replies(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    seen: list[dict[str, Any]] = []

    async def answer(props: dict[str, Any]) -> list[list[str]]:
        seen.append(props)
        return [["Postgres"]]

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig(), on_question=answer) as s:
        srv = holder["server"]
        srv.turn_events = [
            [
                {
                    "type": "question.asked",
                    "properties": {
                        "sessionID": SID,
                        "id": "que_1",
                        "questions": [
                            {
                                "question": "Which DB?",
                                "header": "DB",
                                "options": [{"label": "Postgres", "description": "pg"}],
                            }
                        ],
                    },
                },
                *_text_turn("done"),
            ]
        ]
        srv.final_messages_by_turn = [_final_messages("done")]
        r = await s.send("pick a db")

    assert r.final_text == "done"
    assert len(seen) == 1 and seen[0]["id"] == "que_1"
    assert srv.question_replies == [("que_1", [["Postgres"]])]
    assert srv.question_rejects == []


@pytest.mark.asyncio
async def test_question_default_rejects(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig()) as s:
        srv = holder["server"]
        srv.turn_events = [
            [
                {
                    "type": "question.asked",
                    "properties": {"sessionID": SID, "id": "que_9", "questions": []},
                },
                *_text_turn("ok"),
            ]
        ]
        await s.send("ask me")

    assert srv.question_rejects == ["que_9"]
    assert srv.question_replies == []


@pytest.mark.asyncio
async def test_question_callback_returning_none_rejects(monkeypatch, tmp_path) -> None:
    holder = _install_fake(monkeypatch)
    client = AsyncOpenCodeClient(binary="opencode")
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    async def dismiss(props: dict[str, Any]) -> None:
        return None

    async with OpenCodeSession(client, tmp_path, run_cfg=RunConfig(), on_question=dismiss) as s:
        srv = holder["server"]
        srv.turn_events = [
            [
                {
                    "type": "question.asked",
                    "properties": {"sessionID": SID, "id": "que_2", "questions": []},
                },
                *_text_turn("ok"),
            ]
        ]
        await s.send("ask me")

    assert srv.question_rejects == ["que_2"]
    assert srv.question_replies == []


@pytest.mark.asyncio
async def test_send_outside_context_raises(tmp_path) -> None:
    client = AsyncOpenCodeClient(binary="opencode")
    s = OpenCodeSession(client, tmp_path)
    with pytest.raises(RuntimeError):
        await s.send("nope")
