"""Stateful multi-turn conversation over an ``opencode serve`` session.

``OpenCodeSession`` is an async context manager that owns a headless
``opencode serve`` process for the duration of the ``async with`` block.  On
enter it spawns the server (with the same hermetic isolation run mode uses) and
creates one opencode session pinned to the workspace directory; every
:meth:`send` re-prompts that same session, so the model retains context natively
across turns.  On exit the session is deleted and the server torn down.

Unlike run mode, server mode can answer interactive prompts: pass an
``on_permission`` async callback to pause on a ``permission.asked`` event and
resume with ``"once"`` / ``"always"`` / ``"reject"``, and/or an ``on_question``
callback to answer the ``question`` tool's ``question.asked`` event — neither is
possible with the one-shot run-mode subprocess.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import quote

from opencode_wrapper.config import RunConfig, split_model, validate_permission_actions
from opencode_wrapper.errors import OpenCodeProcessError, OpenCodeTimeoutError
from opencode_wrapper.events import RunResult, aggregate_server_result
from opencode_wrapper.server import _OpenCodeServer

if TYPE_CHECKING:
    from opencode_wrapper.client import AsyncOpenCodeClient

_UNSET: Any = object()

# An async callback invoked on each permission request; returns the decision.
PermissionCallback = Callable[[dict[str, Any]], Awaitable[str]]

# An async callback invoked on each ``question.asked`` request. Returns the
# answers: a list with one entry per question, each entry a list of selected
# option labels (multiple labels only when the question allows ``multiple``).
# Returning ``None`` rejects the question (the model is told it was dismissed).
QuestionCallback = Callable[[dict[str, Any]], Awaitable[Optional[list[list[str]]]]]


class OpenCodeSession:
    """Multi-turn conversation backed by one ``opencode serve`` session.

    Parameters
    ----------
    client:
        The :class:`AsyncOpenCodeClient`; used only to resolve the ``opencode``
        binary path that the server is spawned from.
    workspace:
        Project directory the session is pinned to (``?directory=``); every
        turn's tools resolve against it.
    run_cfg:
        Base config.  ``permission`` / ``mcp`` / ``instructions`` /
        ``config_overrides`` are baked into the server at enter (server-global).
        ``model`` / ``agent`` / ``tools`` are sent per turn and may be overridden
        per :meth:`send`.
    timeout_s:
        Default per-turn timeout; overridable per :meth:`send`.
    on_permission:
        Async callback ``(permission_props) -> "once" | "always" | "reject"``.
        When ``None`` (the default), any ``permission.asked`` is auto-rejected so
        a turn never blocks waiting for input.
    on_question:
        Async callback ``(question_props) -> answers | None`` for the ``question``
        tool's ``question.asked`` event. ``answers`` is a list with one entry per
        question, each a list of selected option labels; ``None`` rejects the
        question. When ``None`` (the default), any ``question.asked`` is
        auto-rejected so a turn never blocks. The ``question`` tool is enabled by
        default in ``opencode serve`` (it is gated on ``OPENCODE_CLIENT``, whose
        default ``"cli"`` enables it).
    log_file:
        Session-level event log.  When given, every event from every turn is
        appended to this file as a JSON line (flushed immediately), so partial
        progress survives crashes.  These are the same event dicts that land in
        each turn's ``result.events``.  The file is truncated once at
        ``__aenter__`` and accumulates across all turns until ``__aexit__``.
    """

    def __init__(
        self,
        client: "AsyncOpenCodeClient",
        workspace: str | Path,
        *,
        run_cfg: RunConfig | None = None,
        timeout_s: float | None = None,
        on_permission: Optional[PermissionCallback] = None,
        on_question: Optional[QuestionCallback] = None,
        log_file: str | Path | None = None,
    ) -> None:
        self._client = client
        self._workspace = str(Path(workspace).expanduser().resolve())
        self._base_cfg = run_cfg or RunConfig()
        self._timeout_s = timeout_s
        self._on_permission = on_permission
        self._on_question = on_question
        self._log_file = log_file
        self._log_fh = None
        self._server: _OpenCodeServer | None = None
        self.session_id: str | None = None

    async def __aenter__(self) -> "OpenCodeSession":
        if self._base_cfg.permission is not None:
            # In server mode "ask" is answerable via on_permission.
            validate_permission_actions(self._base_cfg.permission, allow_ask=True)
        bin_path = self._client.resolved_binary()
        self._server = _OpenCodeServer(bin_path, self._base_cfg, self._workspace)
        await self._server.start()
        session = await self._server.post(f"/session?directory={self._dir_q()}", {})
        self.session_id = session["id"]
        if self._log_file is not None:
            self._log_fh = open(self._log_file, "w")
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self._server is not None:
                if self.session_id:
                    try:
                        await self._server.delete(f"/session/{self.session_id}")
                    except Exception:
                        pass
                await self._server.aclose()
                self._server = None
                self.session_id = None
        finally:
            if self._log_fh is not None:
                self._log_fh.close()
                self._log_fh = None

    def _dir_q(self) -> str:
        return quote(self._workspace, safe="")

    def _build_prompt_body(self, prompt: str, cfg: RunConfig) -> dict[str, Any]:
        # cfg.cli_kwargs is run-mode only (it expands to `opencode run` CLI
        # flags) and is intentionally ignored here — server mode has no CLI.
        body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if cfg.model:
            body["model"] = split_model(cfg.model)
        if cfg.agent:
            body["agent"] = cfg.agent
        if cfg.tools:
            body["tools"] = {k: bool(v) for k, v in cfg.tools.items()}
        return body

    async def send(
        self,
        prompt: str,
        *,
        run_cfg: RunConfig | None = None,
        timeout_s: float | object = _UNSET,
        on_permission: Optional[PermissionCallback] | object = _UNSET,
        on_question: Optional[QuestionCallback] | object = _UNSET,
    ) -> RunResult:
        """Run one turn on the persistent session and return its :class:`RunResult`.

        Per-call ``run_cfg`` only affects prompt-body knobs (``model`` / ``agent``
        / ``tools``); ``permission`` / ``mcp`` / ``instructions`` are fixed at
        ``__aenter__``.  ``on_permission`` and ``on_question`` may be overridden
        per call.
        """
        if self._server is None or self.session_id is None:
            raise RuntimeError("OpenCodeSession.send() must be called inside 'async with'")
        cfg = run_cfg or self._base_cfg
        on_perm = self._on_permission if on_permission is _UNSET else on_permission  # type: ignore[assignment]
        on_q = self._on_question if on_question is _UNSET else on_question  # type: ignore[assignment]
        eff_timeout = self._timeout_s if timeout_s is _UNSET else timeout_s  # type: ignore[assignment]

        coro = self._run_turn(prompt, cfg, on_perm, on_q)  # type: ignore[arg-type]
        if eff_timeout is not None:
            try:
                return await asyncio.wait_for(coro, timeout=eff_timeout)  # type: ignore[arg-type]
            except asyncio.TimeoutError as e:
                raise OpenCodeTimeoutError(
                    f"OpenCode session turn exceeded timeout_s={eff_timeout!r}"
                ) from e
        return await coro

    async def _run_turn(
        self,
        prompt: str,
        cfg: RunConfig,
        on_perm: Optional[PermissionCallback],
        on_question: Optional[QuestionCallback] = None,
    ) -> RunResult:
        assert self._server is not None and self.session_id is not None
        sid = self.session_id
        server = self._server
        queue = server.subscribe(sid)
        events: list[dict[str, Any]] = []
        try:
            body = self._build_prompt_body(prompt, cfg)
            await server.post(f"/session/{sid}/prompt_async?directory={self._dir_q()}", body)

            while True:
                ev = await queue.get()
                events.append(ev)
                if self._log_fh is not None:
                    self._log_fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    self._log_fh.flush()
                etype = ev.get("type")
                props = ev.get("properties", {}) if isinstance(ev.get("properties"), dict) else {}

                if etype == "_sse_error":
                    raise OpenCodeProcessError(
                        exit_code=-1,
                        stderr=str(ev.get("error", "")) + "\n" + server.stderr_tail,
                        events=events,
                        raw_stdout_lines=[],
                    )
                if etype in ("permission.asked", "permission.updated", "permission.ask"):
                    pid = props.get("id")
                    decision = await on_perm(props) if on_perm is not None else "reject"
                    if pid:
                        await server.post(
                            f"/session/{sid}/permissions/{pid}", {"response": decision}
                        )
                    continue
                if etype == "question.asked":
                    qid = props.get("id")
                    answers = await on_question(props) if on_question is not None else None
                    if qid:
                        if answers is None:
                            await server.post(f"/question/{qid}/reject?directory={self._dir_q()}")
                        else:
                            await server.post(
                                f"/question/{qid}/reply?directory={self._dir_q()}",
                                {"answers": answers},
                            )
                    continue
                if etype == "session.error":
                    raise OpenCodeProcessError(
                        exit_code=-1,
                        stderr=f"session.error: {props!r}\n{server.stderr_tail}",
                        events=events,
                        raw_stdout_lines=[],
                    )
                if etype == "session.idle":
                    break
                if etype == "session.status":
                    status = props.get("status")
                    if isinstance(status, dict) and status.get("type") == "idle":
                        break

            try:
                final_messages = await server.get(f"/session/{sid}/message?directory={self._dir_q()}")
            except Exception:
                final_messages = None
            if not isinstance(final_messages, list):
                final_messages = None
            return aggregate_server_result(
                events=events, session_id=sid, final_messages=final_messages
            )
        finally:
            server.unsubscribe(sid)
