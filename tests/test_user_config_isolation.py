"""Tests for the ``inherit_user_config`` flag and global-config sanitisation."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from opencode_wrapper.client import (
    AsyncOpenCodeClient,
    _isolate_user_config,
    _sanitize_and_copy,
)
from opencode_wrapper.config import (
    PRESERVE_KEYS,
    RunConfig,
    loads_jsonc,
    sanitize_user_config_json,
    strip_jsonc,
)


# ---------------------------------------------------------------------------
# sanitize_user_config_json
# ---------------------------------------------------------------------------


def test_sanitize_user_config_json_drops_capability_keys() -> None:
    raw = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"openai": {"api_base": "https://example/v1"}},
        "disabled_providers": ["anthropic"],
        "enabled_providers": ["openai"],
        "model": "anthropic/claude-haiku-4-5",
        "small_model": "anthropic/claude-haiku-4-5",
        "default_agent": "build",
        "mcp": {"my_server": {"command": "uvx", "args": ["my-mcp"]}},
        "agent": {"my_agent": {"description": "..."}},
        "mode": {"plan": {"tools": {"bash": False}}},
        "command": {"my_cmd": {"description": "..."}},
        "skills": ["~/.opencode/skills"],
        "plugin": ["my_plugin"],
        "tools": {"bash": True},
        "formatter": {"py": "ruff format"},
        "lsp": {"go": {"command": ["gopls"]}},
        "instructions": ["AGENTS.md"],
        "permission": {"bash": "deny"},
        "experimental": {"primary_tools": ["bash"]},
        "watcher": {"ignore": ["dist"]},
        "share": "disabled",
        "autoupdate": False,
        "username": "alice",
        "shell": "bash",
        "logLevel": "DEBUG",
    }
    out = sanitize_user_config_json(raw)
    assert set(out.keys()) == {
        "$schema",
        "provider",
        "disabled_providers",
        "enabled_providers",
    }
    assert out["provider"] == {"openai": {"api_base": "https://example/v1"}}


def test_sanitize_user_config_json_passthrough_when_empty() -> None:
    assert sanitize_user_config_json({}) == {}


def test_preserve_keys_contains_only_provider_set() -> None:
    """Guard against accidental scope creep in PRESERVE_KEYS."""
    assert PRESERVE_KEYS == frozenset(
        {"$schema", "provider", "disabled_providers", "enabled_providers"}
    )


# ---------------------------------------------------------------------------
# strip_jsonc / loads_jsonc
# ---------------------------------------------------------------------------


def test_strip_jsonc_strips_line_comments() -> None:
    out = strip_jsonc('// header\n{"a": 1} // tail\n')
    assert json.loads(out) == {"a": 1}


def test_strip_jsonc_strips_block_comments() -> None:
    out = strip_jsonc('/* leading */ {"a": /* inline */ 1}')
    assert json.loads(out) == {"a": 1}


def test_strip_jsonc_strips_trailing_commas_in_objects() -> None:
    out = strip_jsonc('{"a": 1, "b": 2,}')
    assert json.loads(out) == {"a": 1, "b": 2}


def test_strip_jsonc_strips_trailing_commas_in_arrays() -> None:
    out = strip_jsonc('[1, 2, 3,]')
    assert json.loads(out) == [1, 2, 3]


def test_strip_jsonc_preserves_comment_syntax_inside_strings() -> None:
    src = '{"url": "https://x.com/path", "note": "/* not a comment */"}'
    out = strip_jsonc(src)
    parsed = json.loads(out)
    assert parsed == {"url": "https://x.com/path", "note": "/* not a comment */"}


def test_strip_jsonc_handles_escaped_quotes_in_strings() -> None:
    src = r'{"s": "she said \"hi // there\""}'
    out = strip_jsonc(src)
    assert json.loads(out) == {"s": 'she said "hi // there"'}


def test_strip_jsonc_preserves_commas_inside_values() -> None:
    """Commas that aren't immediately before } or ] are left alone."""
    src = '{"a": 1, "b": 2}'
    assert strip_jsonc(src) == '{"a": 1, "b": 2}'


def test_strip_jsonc_handles_trailing_comma_before_close_after_comment() -> None:
    """Trailing comma followed by a comment then } still recognised as trailing."""
    out = strip_jsonc('{"a": 1, /* x */ }')
    assert json.loads(out) == {"a": 1}


def test_loads_jsonc_fast_path_for_strict_json() -> None:
    """Strict JSON parses on the fast path (no JSONC stripping)."""
    assert loads_jsonc('{"a": 1}') == {"a": 1}


def test_loads_jsonc_handles_real_jsonc() -> None:
    assert loads_jsonc('// hi\n{"a": 1,}') == {"a": 1}


def test_loads_jsonc_raises_on_garbage() -> None:
    with pytest.raises(json.JSONDecodeError):
        loads_jsonc("this is not json")


# ---------------------------------------------------------------------------
# _sanitize_and_copy
# ---------------------------------------------------------------------------


def test_sanitize_and_copy_writes_filtered_json(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "opencode.json").write_text(
        json.dumps({"provider": {"openai": {}}, "mcp": {"x": {}}, "model": "m"}),
        encoding="utf-8",
    )
    dst = tmp_path / "dst"

    _sanitize_and_copy(src, dst)

    out = json.loads((dst / "opencode.json").read_text(encoding="utf-8"))
    assert out == {"provider": {"openai": {}}}


def test_sanitize_and_copy_missing_source_dir_creates_empty_dst(tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    _sanitize_and_copy(tmp_path / "does_not_exist", dst)
    assert dst.is_dir()
    assert list(dst.iterdir()) == []


def test_sanitize_and_copy_jsonc_with_comments_and_trailing_commas(
    tmp_path: Path,
) -> None:
    """JSONC superset (line/block comments + trailing commas) is supported."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "opencode.jsonc").write_text(
        """// header
        {
          "provider": {
            "openai": {
              "api_base": "X", /* inline */
            },
          },
          "mcp": {"x": {}},
        }
        """,
        encoding="utf-8",
    )
    dst = tmp_path / "dst"
    _sanitize_and_copy(src, dst)
    out = json.loads((dst / "opencode.jsonc").read_text(encoding="utf-8"))
    assert out == {"provider": {"openai": {"api_base": "X"}}}


def test_sanitize_and_copy_truly_unparseable_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Files that are neither JSON nor JSONC are skipped with a warning."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "opencode.jsonc").write_text("this is not json at all", encoding="utf-8")
    dst = tmp_path / "dst"
    with caplog.at_level(logging.WARNING, logger="opencode_wrapper.client"):
        _sanitize_and_copy(src, dst)
    assert not (dst / "opencode.jsonc").exists()
    assert any("not parseable" in r.message for r in caplog.records)


def test_sanitize_and_copy_jsonc_without_comments_succeeds(tmp_path: Path) -> None:
    """Pure-JSON .jsonc files are sanitised and preserved by filename."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "opencode.jsonc").write_text(
        json.dumps({"provider": {"openai": {}}, "mcp": {"x": {}}}),
        encoding="utf-8",
    )
    dst = tmp_path / "dst"
    _sanitize_and_copy(src, dst)
    out = json.loads((dst / "opencode.jsonc").read_text(encoding="utf-8"))
    assert out == {"provider": {"openai": {}}}


def test_sanitize_and_copy_non_object_root_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "opencode.json").write_text("[]", encoding="utf-8")
    dst = tmp_path / "dst"
    with caplog.at_level(logging.WARNING, logger="opencode_wrapper.client"):
        _sanitize_and_copy(src, dst)
    assert not (dst / "opencode.json").exists()
    assert any("root is not a JSON object" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _isolate_user_config
# ---------------------------------------------------------------------------


def test_isolate_user_config_sets_xdg_and_test_home(tmp_path: Path) -> None:
    fake_home = tmp_path / "real_home"
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps({"provider": {"openai": {}}, "mcp": {"x": {}}}),
        encoding="utf-8",
    )
    (fake_home / ".opencode").mkdir(parents=True)
    (fake_home / ".opencode" / "opencode.json").write_text(
        json.dumps({"provider": {"anthropic": {}}, "agent": {"a": {}}}),
        encoding="utf-8",
    )
    tmp_root = tmp_path / "iso"
    tmp_root.mkdir()

    env = {"HOME": str(fake_home), "OPENCODE_CONFIG": "/leak", "OPENCODE_CONFIG_DIR": "/leak"}
    out = _isolate_user_config(env, tmp_root)

    assert out["XDG_CONFIG_HOME"] == str(tmp_root / "xdg")
    assert out["OPENCODE_TEST_HOME"] == str(tmp_root / "home")
    assert "OPENCODE_CONFIG" not in out
    assert "OPENCODE_CONFIG_DIR" not in out

    xdg_cfg = json.loads(
        (tmp_root / "xdg" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert xdg_cfg == {"provider": {"openai": {}}}
    home_cfg = json.loads(
        (tmp_root / "home" / ".opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert home_cfg == {"provider": {"anthropic": {}}}


def test_isolate_user_config_honours_explicit_xdg_config_home(tmp_path: Path) -> None:
    custom_xdg = tmp_path / "custom_xdg"
    (custom_xdg / "opencode").mkdir(parents=True)
    (custom_xdg / "opencode" / "opencode.json").write_text(
        json.dumps({"provider": {"p": {}}, "mcp": {"x": {}}}),
        encoding="utf-8",
    )
    tmp_root = tmp_path / "iso"
    tmp_root.mkdir()

    env = {"HOME": "/nonexistent", "XDG_CONFIG_HOME": str(custom_xdg)}
    out = _isolate_user_config(env, tmp_root)

    assert out["XDG_CONFIG_HOME"] == str(tmp_root / "xdg")
    cfg = json.loads(
        (tmp_root / "xdg" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert cfg == {"provider": {"p": {}}}


# ---------------------------------------------------------------------------
# Default flag + end-to-end env wiring through async_run
# ---------------------------------------------------------------------------


def test_run_config_default_inherit_user_config_is_false() -> None:
    assert RunConfig().inherit_user_config is False


class _CapturedEnvProc:
    """Fake subprocess that records the env it was spawned with."""

    def __init__(self) -> None:
        self.stdout = _OneShotStdout(b'{"type":"text","content":"ok"}\n')
        self.stderr = _EmptyStream()
        self.returncode: int | None = 0

    async def wait(self) -> int:
        return 0


class _OneShotStdout:
    def __init__(self, line: bytes) -> None:
        self._line: bytes | None = line

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if self._line is None:
            return b""
        out, self._line = self._line, None
        return out

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        return await self.readline()

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("unused")


class _EmptyStream:
    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return b""

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        await asyncio.sleep(0)
        return b""

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("unused")


@pytest.mark.asyncio
async def test_async_run_default_isolates_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """By default, the env passed to opencode redirects XDG_CONFIG_HOME and OPENCODE_TEST_HOME."""
    fake_home = tmp_path / "real_home"
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps({"provider": {"o": {}}, "mcp": {"x": {}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OPENCODE_CONFIG", "/leak/path")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    captured: dict[str, dict[str, str]] = {}

    async def fake_exec(*args, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return _CapturedEnvProc()

    client = AsyncOpenCodeClient(
        binary="opencode", isolate_db=False, startup_delay_s=0
    )
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        await client.async_run("hi", tmp_path, run_cfg=RunConfig())

    env = captured["env"]
    assert "XDG_CONFIG_HOME" in env
    assert env["XDG_CONFIG_HOME"] != str(fake_home / ".config")
    assert "oc_cfg_" in env["XDG_CONFIG_HOME"]
    assert "OPENCODE_TEST_HOME" in env
    assert "oc_cfg_" in env["OPENCODE_TEST_HOME"]
    assert "OPENCODE_CONFIG" not in env


@pytest.mark.asyncio
async def test_async_run_default_writes_sanitised_global_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sanitised opencode.json under the isolated XDG is visible to the child env."""
    fake_home = tmp_path / "real_home"
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps({"provider": {"openai": {"api_base": "X"}}, "mcp": {"x": {}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    seen: dict[str, dict] = {}

    async def fake_exec(*args, **kwargs):
        env = kwargs["env"]
        cfg_path = Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
        seen["cfg"] = json.loads(cfg_path.read_text(encoding="utf-8"))
        return _CapturedEnvProc()

    client = AsyncOpenCodeClient(binary="opencode", isolate_db=False, startup_delay_s=0)
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        await client.async_run("hi", tmp_path, run_cfg=RunConfig())

    assert seen["cfg"] == {"provider": {"openai": {"api_base": "X"}}}


@pytest.mark.asyncio
async def test_async_run_inherit_true_does_not_mutate_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Setting inherit_user_config=True keeps the inherited XDG_CONFIG_HOME / HOME."""
    fake_home = tmp_path / "real_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))

    captured: dict[str, dict[str, str]] = {}

    async def fake_exec(*args, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return _CapturedEnvProc()

    client = AsyncOpenCodeClient(binary="opencode", isolate_db=False, startup_delay_s=0)
    monkeypatch.setattr(client, "resolved_binary", lambda: "/fake/opencode")

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        await client.async_run(
            "hi", tmp_path, run_cfg=RunConfig(inherit_user_config=True)
        )

    env = captured["env"]
    assert env.get("XDG_CONFIG_HOME") == str(fake_home / ".config")
    assert "OPENCODE_TEST_HOME" not in env
