"""Exceptions raised by the OpenCode async client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class OpenCodeError(Exception):
    """Base error for OpenCode wrapper operations."""


class OpenCodeBinaryNotFoundError(OpenCodeError):
    """The configured OpenCode executable path does not exist or is not a file."""


@dataclass
class OpenCodeProcessError(OpenCodeError):
    """``opencode`` exited with a non-zero status or was killed."""

    exit_code: int | None
    stderr: str
    events: list[dict[str, Any]] = field(default_factory=list)
    raw_stdout_lines: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"opencode exited with code {self.exit_code!r}"]
        if self.stderr.strip():
            tail = self.stderr.strip()[-2000:]
            parts.append(f"stderr (tail):\n{tail}")
        return "\n".join(parts)


class OpenCodeTimeoutError(OpenCodeError):
    """The OpenCode subprocess did not finish within the configured timeout."""


class OpenCodeCancelledError(OpenCodeError):
    """The run was cancelled (e.g. asyncio task cancellation)."""
