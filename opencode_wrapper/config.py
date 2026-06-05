"""Runtime config merge for ``OPENCODE_CONFIG_CONTENT`` and CLI flags."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping

# Permission values accepted by OpenCode
PermissionAction = str  # "allow" | "ask" | "deny"

# Nested permission maps: tool name -> action or pattern -> action
PermissionMap = Dict[str, Any]

# Top-level keys retained from the host's global opencode config when
# inherit_user_config=False.  Everything else (mcp / agent / command / skills /
# plugin / tools / instructions / permission / model / ...) is dropped to make
# benchmark runs reproducible.
PRESERVE_KEYS: frozenset[str] = frozenset(
    {
        "$schema",
        "provider",
        "disabled_providers",
        "enabled_providers",
    }
)


def sanitize_user_config_json(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Filter a parsed opencode config dict to the benchmark-safe preserve set.

    Only ``PRESERVE_KEYS`` survive; everything else — including capability keys
    like ``mcp`` / ``agent`` / ``command`` / ``tools`` / ``plugin`` / ``skills``
    and the default ``model`` — is removed.  Benchmark callers re-inject the
    bits they need via ``RunConfig``.
    """
    return {k: v for k, v in raw.items() if k in PRESERVE_KEYS}


def strip_jsonc(source: str) -> str:
    """Convert a JSONC source string to strict JSON.

    Supports the JSONC superset accepted by opencode (and VS Code's
    jsonc-parser): ``//`` line comments, ``/* ... */`` block comments, and
    trailing commas in objects/arrays.  Strings — including ones that contain
    ``//`` or ``/*`` — are preserved verbatim, with backslash escape sequences
    handled.  Unterminated block comments swallow the rest of the input.

    Stdlib-only — keeps the wrapper's zero-runtime-deps invariant.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        # JSON string: copy verbatim, handle escapes
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                cc = source[i]
                out.append(cc)
                if cc == "\\" and i + 1 < n:
                    out.append(source[i + 1])
                    i += 2
                    continue
                i += 1
                if cc == '"':
                    break
            continue
        # Line comment: drop through newline (keep newline for line accuracy)
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            nl = source.find("\n", i + 2)
            i = n if nl == -1 else nl
            continue
        # Block comment: drop through closing */
        if c == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        # Possible trailing comma: peek ahead past whitespace/comments
        if c == ",":
            j = i + 1
            while j < n:
                cj = source[j]
                if cj.isspace():
                    j += 1
                elif cj == "/" and j + 1 < n and source[j + 1] == "/":
                    nl = source.find("\n", j + 2)
                    j = n if nl == -1 else nl
                elif cj == "/" and j + 1 < n and source[j + 1] == "*":
                    end = source.find("*/", j + 2)
                    j = n if end == -1 else end + 2
                else:
                    break
            if j < n and source[j] in "}]":
                i += 1  # drop the trailing comma
                continue
        out.append(c)
        i += 1
    return "".join(out)


def loads_jsonc(source: str) -> Any:
    """Parse a JSON or JSONC string, falling back to JSONC stripping on failure.

    The fast path is plain ``json.loads`` — most files are strict JSON.
    On failure we strip JSONC syntax and retry once.  The caller catches
    ``json.JSONDecodeError`` if even the stripped form is invalid.
    """
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(source))


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, Mapping) and not isinstance(v, (str, bytes, bytearray)):
            existing = out.get(k)
            if isinstance(existing, dict):
                out[k] = _deep_merge(existing, dict(v))
            else:
                out[k] = _deep_merge({}, dict(v))
        else:
            out[k] = v
    return out


@dataclass
class RunConfig:
    """Per-invocation settings merged into env and CLI.

    Most fields are honored by both modes (run mode via CLI/env, server/session
    mode via the prompt body/env).  ``cli_kwargs`` is the exception: it is a
    raw passthrough of ``opencode run`` CLI flags and is **ignored by server/
    session mode**, which has no CLI surface.
    """

    # --- honored by both modes ---
    agent: str | None = None
    model: str | None = None
    disable_autoupdate: bool = True
    inherit_user_config: bool = False
    extra_env: Mapping[str, str] | None = None
    # Injected as JSON via OPENCODE_CONFIG_CONTENT (merged with config_overrides)
    permission: PermissionMap | None = None
    mcp: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    instructions: list[str] | None = None
    config_overrides: dict[str, Any] | None = None
    # --- run mode only: passed through verbatim to `opencode run` as CLI flags;
    # server/session mode ignores this entirely.
    # e.g. {"title": "x", "continue": True, "session": "ses_1", "fork": True,
    #       "thinking": True, "f": ["a.txt"]}
    # build_argv expands each entry: bool True -> "--flag", a value -> "--flag=value"
    # (long) or "-f value" (single-char), a list -> repeated, False/None -> skipped.
    # Note: server mode has no --thinking equivalent; reasoning parts are produced
    # per the model's reasoning config and published to the SSE bus unconditionally,
    # so they already land in result.events / log_file with no opt-in.
    cli_kwargs: dict[str, Any] | None = None

    def build_opencode_config_dict(self) -> dict[str, Any]:
        """Build the dict serialized to ``OPENCODE_CONFIG_CONTENT``."""
        merged: dict[str, Any] = {}
        if self.config_overrides:
            merged = _deep_merge(merged, self.config_overrides)
        if self.permission is not None:
            merged = _deep_merge(merged, {"permission": dict(self.permission)})
        if self.mcp is not None:
            merged = _deep_merge(merged, {"mcp": dict(self.mcp)})
        if self.tools is not None:
            merged = _deep_merge(merged, {"tools": dict(self.tools)})
        if self.instructions is not None:
            merged = _deep_merge(merged, {"instructions": list(self.instructions)})
        return merged

    def opencode_config_content_json(self) -> str | None:
        cfg = self.build_opencode_config_dict()
        if not cfg:
            return None
        return json.dumps(cfg, ensure_ascii=False)


def split_model(model: str) -> dict[str, str]:
    """Split a ``"providerID/modelID"`` string into the server prompt-body shape.

    opencode's server API wants ``{"providerID": ..., "modelID": ...}`` whereas
    ``opencode run -m`` takes the ``provider/model`` string.  Only the first ``/``
    separates provider from model (model ids may themselves contain ``/``).  A
    string with no ``/`` is treated as a bare model id with an empty provider,
    letting the server fall back to its default provider resolution.
    """
    provider, sep, rest = model.partition("/")
    if not sep:
        return {"providerID": "", "modelID": model}
    return {"providerID": provider, "modelID": rest}


def validate_permission_actions(obj: Any, *, _path: str = "", allow_ask: bool = False) -> None:
    """Ensure string leaves are valid OpenCode permission actions.

    ``"ask"`` is rejected by default because the run-mode subprocess has no
    terminal to prompt — it would block forever.  The server/session path passes
    ``allow_ask=True`` because there a ``permission.asked`` event is answerable via
    the ``on_permission`` callback.
    """
    allowed = frozenset({"allow", "deny", "ask"} if allow_ask else {"allow", "deny"})
    if isinstance(obj, str):
        if obj == "ask" and not allow_ask:
            loc = f" at {_path!r}" if _path else ""
            raise ValueError(
                f"Permission action 'ask' is not supported in non-interactive "
                f"subprocess mode{loc}; use 'allow' or 'deny' instead"
            )
        if obj not in allowed:
            raise ValueError(
                f"Invalid permission action {obj!r}{' at ' + repr(_path) if _path else ''}; "
                f"expected one of {sorted(allowed)}"
            )
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{_path}.{k}" if _path else k
            validate_permission_actions(v, _path=child_path, allow_ask=allow_ask)


def validate_config_for_run(cfg: RunConfig) -> None:
    """Strict checks before spawning."""
    if cfg.permission is not None:
        validate_permission_actions(cfg.permission)
