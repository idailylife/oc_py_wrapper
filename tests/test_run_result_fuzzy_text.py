"""Tests for ``run_result_fuzzy_text``."""

from opencode_wrapper.events import RunResult, run_result_fuzzy_text


def test_fuzzy_prefers_final_text() -> None:
    r = RunResult(final_text="  hello  ")
    assert run_result_fuzzy_text(r) == "hello"


def test_fuzzy_from_content_list_parts() -> None:
    r = RunResult(
        events=[
            {
                "type": "assistant",
                "content": [{"type": "text", "text": "北京晴"}],
            }
        ]
    )
    assert "北京" in run_result_fuzzy_text(r)
