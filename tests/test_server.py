"""Unit tests for server-mode helpers (no real ``opencode serve`` spawned)."""

from __future__ import annotations

import pytest

from opencode_wrapper.config import RunConfig, split_model, validate_permission_actions
from opencode_wrapper.events import aggregate_server_result
from opencode_wrapper.server import _OpenCodeServer, _event_session_id, _free_port


# -- split_model ------------------------------------------------------------
def test_split_model_provider_and_model() -> None:
    assert split_model("opencode/big-pickle") == {"providerID": "opencode", "modelID": "big-pickle"}


def test_split_model_only_first_slash_splits() -> None:
    # model ids may themselves contain a slash; only the first separates.
    assert split_model("openrouter/meta/llama") == {
        "providerID": "openrouter",
        "modelID": "meta/llama",
    }


def test_split_model_no_slash_is_bare_model() -> None:
    assert split_model("big-pickle") == {"providerID": "", "modelID": "big-pickle"}


# -- validate_permission_actions allow_ask ----------------------------------
def test_validate_permission_allow_ask_accepts_ask() -> None:
    validate_permission_actions({"bash": "ask"}, allow_ask=True)
    validate_permission_actions({"edit": {"*.py": "ask"}}, allow_ask=True)


def test_validate_permission_default_rejects_ask() -> None:
    with pytest.raises(ValueError, match="'ask' is not supported"):
        validate_permission_actions({"bash": "ask"})


def test_validate_permission_rejects_unknown_action() -> None:
    with pytest.raises(ValueError, match="Invalid permission action"):
        validate_permission_actions({"bash": "maybe"}, allow_ask=True)


# -- _event_session_id ------------------------------------------------------
def test_event_session_id_top_level() -> None:
    assert _event_session_id({"properties": {"sessionID": "ses_1"}}) == "ses_1"


def test_event_session_id_nested_part() -> None:
    ev = {"properties": {"part": {"sessionID": "ses_2", "type": "text"}}}
    assert _event_session_id(ev) == "ses_2"


def test_event_session_id_nested_info() -> None:
    ev = {"properties": {"info": {"sessionID": "ses_3", "role": "assistant"}}}
    assert _event_session_id(ev) == "ses_3"


def test_event_session_id_absent() -> None:
    assert _event_session_id({"type": "server.connected"}) is None
    assert _event_session_id({"properties": {}}) is None


# -- _free_port -------------------------------------------------------------
def test_free_port_is_usable_int() -> None:
    p = _free_port()
    assert isinstance(p, int) and 1024 <= p <= 65535


# -- _OpenCodeServer dispatch routing (no subprocess) -----------------------
@pytest.mark.asyncio
async def test_dispatch_routes_event_to_session_queue() -> None:
    srv = _OpenCodeServer("/fake/opencode", RunConfig(), "/tmp/ws")
    q1 = srv.subscribe("ses_a")
    q2 = srv.subscribe("ses_b")

    srv._dispatch({"type": "message.part.updated", "properties": {"sessionID": "ses_a"}})
    assert q1.qsize() == 1 and q2.qsize() == 0
    ev = q1.get_nowait()
    assert ev["properties"]["sessionID"] == "ses_a"


@pytest.mark.asyncio
async def test_dispatch_drops_event_for_unknown_session() -> None:
    srv = _OpenCodeServer("/fake/opencode", RunConfig(), "/tmp/ws")
    q = srv.subscribe("ses_a")
    srv._dispatch({"type": "message.part.updated", "properties": {"sessionID": "ses_other"}})
    assert q.qsize() == 0


@pytest.mark.asyncio
async def test_dispatch_sse_error_fans_out_to_all() -> None:
    srv = _OpenCodeServer("/fake/opencode", RunConfig(), "/tmp/ws")
    q1 = srv.subscribe("ses_a")
    q2 = srv.subscribe("ses_b")
    srv._dispatch({"type": "_sse_error", "error": "boom"})
    assert q1.get_nowait()["type"] == "_sse_error"
    assert q2.get_nowait()["type"] == "_sse_error"


@pytest.mark.asyncio
async def test_unsubscribe_removes_queue() -> None:
    srv = _OpenCodeServer("/fake/opencode", RunConfig(), "/tmp/ws")
    srv.subscribe("ses_a")
    srv.unsubscribe("ses_a")
    q = srv.subscribe("ses_b")
    srv._dispatch({"type": "x", "properties": {"sessionID": "ses_a"}})
    assert q.qsize() == 0  # ses_a event dropped, ses_b untouched


# -- aggregate_server_result ------------------------------------------------
def _part_updated(sid: str, ptype: str, **part) -> dict:
    return {"type": "message.part.updated", "properties": {"sessionID": sid, "part": {"type": ptype, **part}}}


def test_aggregate_uses_final_messages_as_authoritative_text() -> None:
    events = [_part_updated("ses_1", "text", id="prt_1", text="streamed")]
    final = [{"info": {"role": "assistant", "id": "m1"}, "parts": [{"type": "text", "text": "final answer"}]}]
    r = aggregate_server_result(events=events, session_id="ses_1", final_messages=final)
    assert r.final_text == "final answer"
    assert r.session_id == "ses_1"


def test_aggregate_falls_back_to_streamed_text() -> None:
    events = [
        _part_updated("ses_1", "text", id="prt_1", text="hello "),
        _part_updated("ses_1", "text", id="prt_1", text="hello world"),  # replaced snapshot
    ]
    r = aggregate_server_result(events=events, session_id="ses_1", final_messages=None)
    assert r.final_text == "hello world"


def test_aggregate_collects_tool_calls() -> None:
    events = [
        _part_updated("ses_1", "tool", id="prt_t", tool="bash", callID="call_1", state={"status": "completed"}),
    ]
    r = aggregate_server_result(events=events, session_id="ses_1")
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0]["tool"] == "bash"
    assert r.tool_calls[0]["callID"] == "call_1"


def test_aggregate_accumulates_tokens_cost_and_turns() -> None:
    events = [
        {
            "type": "message.updated",
            "properties": {
                "info": {
                    "role": "assistant",
                    "id": "m1",
                    "cost": 0.5,
                    "tokens": {"input": 10, "output": 5, "total": 15, "cache": {"read": 2, "write": 1}},
                }
            },
        },
    ]
    r = aggregate_server_result(events=events, session_id="ses_1")
    assert r.total_cost == 0.5
    assert r.token_usage.input == 10
    assert r.token_usage.output == 5
    assert r.token_usage.total == 15
    assert r.token_usage.cache_read == 2
    assert r.token_usage.cache_write == 1
    assert r.turns == 1
