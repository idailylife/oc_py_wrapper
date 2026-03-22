"""OpenCode CLI async wrapper for Python orchestration."""

from opencode_wrapper.client import AsyncOpenCodeClient, build_argv, build_env, resolve_binary
from opencode_wrapper.config import RunConfig, validate_config_for_run, validate_permission_actions
from opencode_wrapper.errors import (
    OpenCodeBinaryNotFoundError,
    OpenCodeCancelledError,
    OpenCodeError,
    OpenCodeProcessError,
    OpenCodeTimeoutError,
)
from opencode_wrapper.events import (
    RunResult,
    aggregate_run_result,
    parse_event_line,
    run_result_fuzzy_text,
)

__all__ = [
    "AsyncOpenCodeClient",
    "RunConfig",
    "RunResult",
    "aggregate_run_result",
    "build_argv",
    "build_env",
    "parse_event_line",
    "run_result_fuzzy_text",
    "resolve_binary",
    "validate_config_for_run",
    "validate_permission_actions",
    "OpenCodeError",
    "OpenCodeBinaryNotFoundError",
    "OpenCodeProcessError",
    "OpenCodeTimeoutError",
    "OpenCodeCancelledError",
]
