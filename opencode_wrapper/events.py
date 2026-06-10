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


def _session_id_from_event(ev: dict[str, Any]) -> str | None:
    sid = ev.get("sessionID")
    if isinstance(sid, str):
        return sid
    part = ev.get("part")
    if isinstance(part, dict) and isinstance(part.get("sessionID"), str):
        return part["sessionID"]
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
        summary = {k: v for k, v in ev.items() if k != "type"}
        summary["type"] = t
        return summary
    if t == "step_finish" and "tool" in ev:
        return {"type": "step_tool", "payload": ev.get("tool")}
    return None


@dataclass
class TokenUsage:
    """Aggregated token counts across all steps."""

    total: int = 0
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class RunResult:
    """Aggregated outcome of a completed ``opencode run``."""

    events: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    stderr: str = ""
    raw_stdout_lines: list[str] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    total_cost: float = 0.0
    turns: int = 0
    session_id: str | None = None

    def append_event(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        if self.session_id is None:
            self.session_id = _session_id_from_event(ev)
        chunk = _text_from_event(ev)
        if chunk:
            self.final_text += chunk
        tool = _tool_summary(ev)
        if tool is not None:
            self.tool_calls.append(tool)
        if ev.get("type") == "step_finish":
            self._accumulate_step(ev)

    def _accumulate_step(self, ev: dict[str, Any]) -> None:
        """Extract cost/token/turn info from a ``step_finish`` event."""
        part = ev.get("part") or ev
        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            self.total_cost += cost
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            u = self.token_usage
            for attr, key in (
                ("total", "total"),
                ("input", "input"),
                ("output", "output"),
                ("reasoning", "reasoning"),
            ):
                val = tokens.get(key)
                if isinstance(val, (int, float)):
                    setattr(u, attr, getattr(u, attr) + int(val))
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                for attr, key in (("cache_read", "read"), ("cache_write", "write")):
                    val = cache.get(key)
                    if isinstance(val, (int, float)):
                        setattr(u, attr, getattr(u, attr) + int(val))
        self.turns += 1


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


# ---------------------------------------------------------------------------
# Server-mode (opencode serve / SSE) aggregation
#
# Server event shapes differ from `opencode run --format json`:
#   {"type": "message.part.updated",
#    "properties": {"sessionID": "...", "part": {"type": "text"|"tool"|"reasoning",
#                                                 "text": "...", "id": "prt_...", ...}}}
#   {"type": "message.updated", "properties": {"info": {"role": "assistant",
#                                                       "tokens": {...}, "cost": ...}}}
# Run-mode parsing above is intentionally left untouched.
# ---------------------------------------------------------------------------


def _server_assistant_text_from_messages(messages: list[dict[str, Any]]) -> str:
    """Extract the last assistant message's concatenated text parts.

    ``GET /session/{id}/message`` returns ``[{info:{role,...}, parts:[...]}, ...]``.
    The authoritative final answer is the text parts of the final assistant turn.
    """
    last = ""
    for m in messages:
        info = m.get("info") if isinstance(m, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        parts = m.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [
            p["text"]
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        ]
        joined = "".join(texts).strip()
        if joined:
            last = joined
    return last


def _accumulate_token_usage(usage: TokenUsage, tokens: Any, cost_acc: list[float], info: dict) -> None:
    cost = info.get("cost")
    if isinstance(cost, (int, float)):
        cost_acc[0] += float(cost)
    if isinstance(tokens, dict):
        for attr, key in (("total", "total"), ("input", "input"), ("output", "output"), ("reasoning", "reasoning")):
            val = tokens.get(key)
            if isinstance(val, (int, float)):
                setattr(usage, attr, getattr(usage, attr) + int(val))
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for attr, key in (("cache_read", "read"), ("cache_write", "write")):
                val = cache.get(key)
                if isinstance(val, (int, float)):
                    setattr(usage, attr, getattr(usage, attr) + int(val))


def aggregate_server_result(
    *,
    events: list[dict[str, Any]],
    session_id: str | None,
    final_messages: list[dict[str, Any]] | None = None,
    exit_code: int | None = 0,
    stderr: str = "",
) -> RunResult:
    """Build a :class:`RunResult` from a server-mode turn's SSE events.

    ``events`` are the raw SSE event dicts collected during the turn.  When
    ``final_messages`` (the ``GET /session/{id}/message`` payload) is supplied it
    is treated as the authoritative source for final text and token/cost totals;
    otherwise those are reconstructed from the streamed events.
    """
    r = RunResult(events=list(events), exit_code=exit_code, stderr=stderr, session_id=session_id)

    # tool_calls + streamed text snapshots keyed by part id (parts are replaced,
    # not appended, as they stream — keep the latest snapshot per id).
    text_by_part: dict[str, str] = {}
    text_order: list[str] = []
    cost_acc = [0.0]
    seen_assistant_msgs: set[str] = set()

    for ev in events:
        etype = ev.get("type")
        props = ev.get("properties", {}) if isinstance(ev.get("properties"), dict) else {}
        if etype in ("message.part.updated", "message.part.delta"):
            part = props.get("part")
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            pid = part.get("id") or ""
            if ptype == "text" and isinstance(part.get("text"), str):
                if pid not in text_by_part:
                    text_order.append(pid)
                text_by_part[pid] = part["text"]
            elif ptype == "tool":
                r.tool_calls.append({
                    "type": "tool",
                    "tool": part.get("tool"),
                    "callID": part.get("callID"),
                    "state": part.get("state"),
                    "id": pid,
                })
        elif etype == "message.updated":
            info = props.get("info")
            if isinstance(info, dict) and info.get("role") == "assistant":
                mid = info.get("id") or ""
                if mid not in seen_assistant_msgs:
                    seen_assistant_msgs.add(mid)
                    r.turns += 1
                _accumulate_token_usage(r.token_usage, info.get("tokens"), cost_acc, info)

    r.total_cost = cost_acc[0]

    if final_messages is not None:
        r.final_text = _server_assistant_text_from_messages(final_messages)
    if not r.final_text:
        r.final_text = "".join(text_by_part[pid] for pid in text_order).strip()
    return r
