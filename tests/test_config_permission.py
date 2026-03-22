"""Tests for ``RunConfig.permission`` shapes accepted by OpenCode (incl. external_directory)."""

from __future__ import annotations

import json

import pytest

from opencode_wrapper.client import build_env
from opencode_wrapper.config import RunConfig, validate_config_for_run


def test_permission_external_directory_allow_and_edit_deny_validates_and_merges() -> None:
    """Matches OpenCode docs: allow paths outside cwd via ``external_directory``, restrict ``edit``."""
    cfg = RunConfig(
        permission={
            "external_directory": {
                "~/projects/personal/**": "allow",
            },
            "edit": {
                "~/projects/personal/**": "deny",
            },
        },
    )
    validate_config_for_run(cfg)
    d = cfg.build_opencode_config_dict()
    perm = d["permission"]
    assert perm["external_directory"]["~/projects/personal/**"] == "allow"
    assert perm["edit"]["~/projects/personal/**"] == "deny"


def test_permission_external_directory_in_opencode_config_content_json() -> None:
    cfg = RunConfig(
        permission={
            "external_directory": {
                "$HOME/some-trusted/**": "allow",
            },
        },
        disable_autoupdate=True,
    )
    env = build_env(cfg, base={"HOME": "/tmp/x"})
    payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert payload["permission"]["external_directory"]["$HOME/some-trusted/**"] == "allow"


def test_permission_external_directory_deep_merges_with_config_overrides() -> None:
    cfg = RunConfig(
        config_overrides={"permission": {"read": {"*": "allow", "*.env": "deny"}}},
        permission={
            "external_directory": {"/opt/shared/**": "allow"},
        },
    )
    validate_config_for_run(cfg)
    perm = cfg.build_opencode_config_dict()["permission"]
    assert perm["read"]["*"] == "allow"
    assert perm["read"]["*.env"] == "deny"
    assert perm["external_directory"]["/opt/shared/**"] == "allow"


@pytest.mark.parametrize("action", ["allow", "deny"])
def test_permission_external_directory_accepts_non_interactive_actions(action: str) -> None:
    cfg = RunConfig(permission={"external_directory": {"/tmp/out/**": action}})
    validate_config_for_run(cfg)


def test_permission_ask_rejected() -> None:
    """``ask`` blocks forever in subprocess mode — must be rejected."""
    cfg = RunConfig(permission={"bash": "ask"})
    with pytest.raises(ValueError, match="'ask' is not supported"):
        validate_config_for_run(cfg)


def test_permission_ask_rejected_nested() -> None:
    cfg = RunConfig(permission={"edit": {"*.py": "ask"}})
    with pytest.raises(ValueError, match="'ask' is not supported.*edit\\.\\*\\.py"):
        validate_config_for_run(cfg)
