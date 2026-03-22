"""Runtime config merge for ``OPENCODE_CONFIG_CONTENT`` and CLI flags."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# Permission values accepted by OpenCode
PermissionAction = str  # "allow" | "ask" | "deny"

# Nested permission maps: tool name -> action or pattern -> action
PermissionMap = dict[str, Any]


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
    """Per-invocation settings merged into env and CLI."""

    agent: str | None = None
    model: str | None = None
    files: tuple[str | Path, ...] = ()
    title: str | None = None
    command: str | None = None
    continue_session: bool = False
    session_id: str | None = None
    fork: bool = False
    share: bool | None = None
    attach: str | None = None
    password: str | None = None
    remote_dir: str | None = None
    port: int | None = None
    variant: str | None = None
    thinking: bool | None = None
    print_logs: bool | None = None
    log_level: str | None = None
    disable_autoupdate: bool = True
    extra_env: Mapping[str, str] | None = None
    # Injected as JSON via OPENCODE_CONFIG_CONTENT (merged with config_overrides)
    permission: PermissionMap | None = None
    mcp: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    config_overrides: dict[str, Any] | None = None

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
        return merged

    def opencode_config_content_json(self) -> str | None:
        cfg = self.build_opencode_config_dict()
        if not cfg:
            return None
        return json.dumps(cfg, ensure_ascii=False)


def validate_permission_actions(obj: Any, *, _path: str = "") -> None:
    """Ensure string leaves are non-interactive OpenCode permission actions.

    ``"ask"`` is rejected because the subprocess has no terminal to prompt —
    it would block forever.
    """
    allowed = frozenset({"allow", "deny"})
    if isinstance(obj, str):
        if obj == "ask":
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
            validate_permission_actions(v, _path=child_path)


def validate_config_for_run(cfg: RunConfig) -> None:
    """Strict checks before spawning."""
    if cfg.permission is not None:
        validate_permission_actions(cfg.permission)
