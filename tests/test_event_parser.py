"""Tests for stdout line parsing and aggregation."""

from opencode_wrapper.events import (
    RunResult,
    aggregate_run_result,
    parse_event_line,
)


def test_parse_valid_json_object() -> None:
    ev = parse_event_line('{"type":"text","content":"hello"}')
    assert ev["type"] == "text"
    assert ev["content"] == "hello"


def test_parse_invalid_json_becomes_diagnostic() -> None:
    ev = parse_event_line("not json")
    assert ev["type"] == "diagnostic"
    assert ev["kind"] == "json_decode_error"


def test_aggregate_run_result_text_and_tool() -> None:
    events = [
        {"type": "text", "content": "a"},
        {"type": "tool_use", "name": "bash", "status": "completed"},
    ]
    r = aggregate_run_result(
        events=events,
        raw_stdout_lines=['{"type":"text","content":"a"}\n'],
        exit_code=0,
        stderr="",
    )
    assert r.final_text == "a"
    assert len(r.tool_calls) == 1
    assert r.events == events


def test_run_result_append_event() -> None:
    r = RunResult()
    r.append_event({"type": "text", "delta": "x"})
    assert r.final_text == "x"
