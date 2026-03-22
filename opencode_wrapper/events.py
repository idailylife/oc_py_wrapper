"""Parse ``opencode run --format json`` stdout lines and aggregate ``RunResult``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator


def parse_event_line(line: str) -> dict[str, Any]:
    """
    Parse one stdout line into an event dict.

    Non-JSON lines become a ``diagnostic`` event so the stream never breaks.
    """
    stripped = line.strip()
    if not stripped:
        return {"type": "diagnostic", "kind": "empty_line", "raw": line}
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
        return {"type": "diagnostic", "kind": "non_object_json", "value": obj}
    except json.JSONDecodeError as e:
        return {
            "type": "diagnostic",
            "kind": "json_decode_error",
            "raw": stripped,
            "error": str(e),
        }


def iter_parse_lines(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        yield parse_event_line(line)


def _text_from_event(ev: dict[str, Any]) -> str | None:
    t = ev.get("type")
    if t == "text":
        # OpenCode nested: {"type":"text","part":{"type":"text","text":"..."}}
        part = ev.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
        # Flat shapes: {"type":"text","content":"..."} or {"text":"..."}
        if "content" in ev and isinstance(ev["content"], str):
            return ev["content"]
        if "text" in ev and isinstance(ev["text"], str):
            return ev["text"]
        if "delta" in ev and isinstance(ev["delta"], str):
            return ev["delta"]
    if t in ("message", "assistant", "model"):
        content = ev.get("content")
        if isinstance(content, str):
            return content
    # OpenCode / provider streaming: content as list of parts
    content = ev.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
            elif isinstance(part, str):
                parts.append(part)
        if parts:
            return "".join(parts)
    return None


def run_result_fuzzy_text(result: "RunResult") -> str:
    """
    Best-effort extract human-visible model output across varying ``--format json`` shapes.

    Uses :attr:`RunResult.final_text` when non-empty; otherwise scans events and raw lines.
    """
    if (result.final_text or "").strip():
        return result.final_text.strip()
    pieces: list[str] = []
    for ev in result.events:
        if ev.get("type") == "diagnostic":
            continue
        chunk = _text_from_event(ev)
        if chunk and chunk.strip():
            pieces.append(chunk.strip())
            continue
        for key in ("content", "text", "delta", "output", "message", "result", "value"):
            val = ev.get(key)
            if isinstance(val, str) and val.strip():
                pieces.append(val.strip())
        msg = ev.get("message")
        if isinstance(msg, dict):
            for key in ("content", "text"):
                v = msg.get(key)
                if isinstance(v, str) and v.strip():
                    pieces.append(v.strip())
    if pieces:
        return "\n".join(pieces).strip()
    raw = "\n".join(x.strip() for x in result.raw_stdout_lines if x.strip())
    return raw.strip()


def _tool_summary(ev: dict[str, Any]) -> dict[str, Any] | None:
    t = ev.get("type")
    if t in ("tool_use", "tool_call", "tool_result", "tool"):
        return {k: v for k, v in ev.items() if k != "type"} | {"type": t}
    if t == "step_finish" and "tool" in ev:
        return {"type": "step_tool", "payload": ev.get("tool")}
    return None


@dataclass
class RunResult:
    """Aggregated outcome of a completed ``opencode run``."""

    events: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    stderr: str = ""
    raw_stdout_lines: list[str] = field(default_factory=list)

    def append_event(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        chunk = _text_from_event(ev)
        if chunk:
            self.final_text += chunk
        tool = _tool_summary(ev)
        if tool is not None:
            self.tool_calls.append(tool)


def aggregate_run_result(
    *,
    events: list[dict[str, Any]],
    raw_stdout_lines: list[str],
    exit_code: int | None,
    stderr: str,
) -> RunResult:
    r = RunResult(
        raw_stdout_lines=list(raw_stdout_lines),
        exit_code=exit_code,
        stderr=stderr,
    )
    for ev in events:
        r.append_event(ev)
    return r
