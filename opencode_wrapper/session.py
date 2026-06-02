"""Stateful multi-turn conversation over a single opencode session."""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from opencode_wrapper.config import RunConfig
from opencode_wrapper.events import RunResult

if TYPE_CHECKING:
    from opencode_wrapper.client import AsyncOpenCodeClient

_UNSET: object = object()


class OpenCodeSession:
    """Multi-turn conversation backed by one persistent opencode session.

    On ``__aenter__`` the session allocates a private ``XDG_DATA_HOME`` tmpdir.
    Every :meth:`send` reuses it, so opencode's SQLite session DB survives across
    turns — the first turn creates the session, later turns continue it via
    ``--session <id>``.  The dir is a per-session island (no shared global DB, so
    no cross-session lock contention) and is removed on ``__aexit__``.

    Parameters
    ----------
    client:
        The :class:`AsyncOpenCodeClient` used to spawn each turn.
    workspace:
        Project directory passed to every ``opencode run``.
    run_cfg:
        Base config applied to each turn; ``session_id`` is injected automatically
        after the first turn.  A per-call override may be passed to :meth:`send`.
    timeout_s:
        Default per-turn timeout; overridable per :meth:`send` call.
    """

    def __init__(
        self,
        client: "AsyncOpenCodeClient",
        workspace: str | Path,
        *,
        run_cfg: RunConfig | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._client = client
        self._workspace = workspace
        self._base_cfg = run_cfg or RunConfig()
        self._timeout_s = timeout_s
        self._data_home: str | None = None
        self.session_id: str | None = None

    async def __aenter__(self) -> "OpenCodeSession":
        self._data_home = tempfile.mkdtemp(prefix="oc_session_")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._data_home is not None:
            shutil.rmtree(self._data_home, ignore_errors=True)
            self._data_home = None

    async def send(
        self,
        prompt: str,
        *,
        run_cfg: RunConfig | None = None,
        timeout_s: float | object = _UNSET,
    ) -> RunResult:
        """Run one turn and return its :class:`RunResult`, continuing the session."""
        if self._data_home is None:
            raise RuntimeError(
                "OpenCodeSession.send() must be called inside 'async with'"
            )
        cfg = run_cfg or self._base_cfg
        if self.session_id:
            cfg = dataclasses.replace(cfg, session_id=self.session_id)
        result = await self._client.async_run(
            prompt,
            self._workspace,
            run_cfg=cfg,
            timeout_s=self._timeout_s if timeout_s is _UNSET else timeout_s,  # type: ignore[arg-type]
            data_home=self._data_home,
        )
        if self.session_id is None and result.session_id:
            self.session_id = result.session_id
        return result
