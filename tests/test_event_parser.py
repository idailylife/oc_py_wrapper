"""Tests for stdout line parsing and aggregation."""

from opencode_wrapper.events import (
    RunResult,
    TokenUsage,
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


def test_step_finish_accumulates_token_usage() -> None:
    """step_finish events populate token_usage, total_cost, and turns."""
    step1 = {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "reason": "tool-calls",
            "cost": 0.001,
            "tokens": {
                "total": 1000,
                "input": 500,
                "output": 100,
                "reasoning": 0,
                "cache": {"read": 300, "write": 100},
            },
        },
    }
    step2 = {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "reason": "stop",
            "cost": 0.002,
            "tokens": {
                "total": 2000,
                "input": 800,
                "output": 200,
                "reasoning": 50,
                "cache": {"read": 700, "write": 250},
            },
        },
    }
    r = aggregate_run_result(
        events=[step1, step2],
        raw_stdout_lines=[],
        exit_code=0,
        stderr="",
    )
    assert r.turns == 2
    assert abs(r.total_cost - 0.003) < 1e-9
    assert r.token_usage.total == 3000
    assert r.token_usage.input == 1300
    assert r.token_usage.output == 300
    assert r.token_usage.reasoning == 50
    assert r.token_usage.cache_read == 1000
    assert r.token_usage.cache_write == 350


def test_step_finish_without_part_uses_top_level() -> None:
    """step_finish with cost/tokens at top level (no part wrapper)."""
    ev = {
        "type": "step_finish",
        "cost": 0.005,
        "tokens": {"total": 500, "input": 200, "output": 100, "reasoning": 0},
    }
    r = RunResult()
    r.append_event(ev)
    assert r.turns == 1
    assert abs(r.total_cost - 0.005) < 1e-9
    assert r.token_usage.total == 500


def test_step_finish_missing_tokens_is_safe() -> None:
    """step_finish with no cost/tokens fields doesn't crash."""
    ev = {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}}
    r = RunResult()
    r.append_event(ev)
    assert r.turns == 1
    assert r.total_cost == 0.0
    assert r.token_usage.total == 0


def test_token_usage_defaults() -> None:
    assert TokenUsage() == TokenUsage(0, 0, 0, 0, 0, 0)
