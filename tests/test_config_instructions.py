"""Tests for ``RunConfig.instructions`` field."""

from __future__ import annotations

import json

from opencode_wrapper.client import build_env
from opencode_wrapper.config import RunConfig


def test_instructions_single_file() -> None:
    cfg = RunConfig(instructions=["AGENT.md"])
    d = cfg.build_opencode_config_dict()
    assert d["instructions"] == ["AGENT.md"]


def test_instructions_multiple_files() -> None:
    cfg = RunConfig(instructions=["CONTRIBUTING.md", "docs/guidelines.md"])
    d = cfg.build_opencode_config_dict()
    assert d["instructions"] == ["CONTRIBUTING.md", "docs/guidelines.md"]


def test_instructions_glob_pattern() -> None:
    cfg = RunConfig(instructions=[".cursor/rules/*.md"])
    d = cfg.build_opencode_config_dict()
    assert d["instructions"] == [".cursor/rules/*.md"]


def test_instructions_none_omits_key() -> None:
    cfg = RunConfig(instructions=None)
    d = cfg.build_opencode_config_dict()
    assert "instructions" not in d


def test_instructions_empty_list() -> None:
    cfg = RunConfig(instructions=[])
    d = cfg.build_opencode_config_dict()
    assert d["instructions"] == []


def test_instructions_in_opencode_config_content_json() -> None:
    cfg = RunConfig(instructions=["AGENT.md", "docs/*.md"])
    env = build_env(cfg, base={})
    payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert payload["instructions"] == ["AGENT.md", "docs/*.md"]


def test_instructions_deep_merges_with_config_overrides() -> None:
    """instructions field wins over config_overrides when both set."""
    cfg = RunConfig(
        config_overrides={"instructions": ["old.md"]},
        instructions=["new.md"],
    )
    d = cfg.build_opencode_config_dict()
    # instructions applied after config_overrides, so it overwrites
    assert d["instructions"] == ["new.md"]


def test_instructions_config_overrides_used_when_no_instructions_field() -> None:
    cfg = RunConfig(config_overrides={"instructions": ["via_overrides.md"]})
    d = cfg.build_opencode_config_dict()
    assert d["instructions"] == ["via_overrides.md"]


def test_instructions_combined_with_permission_and_mcp() -> None:
    cfg = RunConfig(
        permission={"bash": "allow"},
        mcp={"my-server": {"type": "local", "command": ["npx", "my-mcp"]}},
        instructions=["AGENT.md"],
    )
    d = cfg.build_opencode_config_dict()
    assert d["instructions"] == ["AGENT.md"]
    assert d["permission"]["bash"] == "allow"
    assert d["mcp"]["my-server"]["type"] == "local"


def test_instructions_not_mutated() -> None:
    """build_opencode_config_dict returns a copy; original list is not mutated."""
    original = ["AGENT.md"]
    cfg = RunConfig(instructions=original)
    d = cfg.build_opencode_config_dict()
    d["instructions"].append("extra.md")
    assert cfg.instructions == ["AGENT.md"]
