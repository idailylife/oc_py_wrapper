"""Headless ``opencode serve`` lifecycle + a stdlib HTTP/SSE client.

Backs :class:`opencode_wrapper.session.OpenCodeSession`.  One
:class:`_OpenCodeServer` owns a single ``opencode serve`` subprocess for the
lifetime of an ``async with`` session block: it spawns the server with the same
hermetic env run mode uses, subscribes to the ``/event`` SSE bus, and exposes
unary POST/GET/DELETE helpers.  Stdlib-only (``urllib`` + ``asyncio`` +
``threading``) — preserves the wrapper's zero-runtime-deps invariant.

The SSE bus is consumed in a daemon thread via ``urllib`` (which decodes chunked
transfer-encoding transparently) and events are dispatched onto per-session
``asyncio.Queue``s, so one server can fan events out to many sessions.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from opencode_wrapper.client import build_env, _isolate_user_config
from opencode_wrapper.config import RunConfig
from opencode_wrapper.errors import OpenCodeProcessError


def _free_port() -> int:
    """Pick an ephemeral free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _event_session_id(ev: dict[str, Any]) -> str | None:
    """Best-effort extract the sessionID a server SSE event belongs to."""
    props = ev.get("properties")
    if not isinstance(props, dict):
        return None
    sid = props.get("sessionID")
    if isinstance(sid, str):
        return sid
    for key in ("part", "info"):
        nested = props.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("sessionID"), str):
            return nested["sessionID"]
    return None


class _OpenCodeServer:
    """Owns one ``opencode serve`` process and its ``/event`` SSE subscription."""

    def __init__(
        self,
        binary_resolved: str,
        run_cfg: RunConfig,
        workspace: str,
    ) -> None:
        self._binary = binary_resolved
        self._run_cfg = run_cfg
        self._workspace = workspace
        self._port = _free_port()
        self.base = f"http://127.0.0.1:{self._port}"

        self._proc: asyncio.subprocess.Process | None = None
        self._cleanup_dirs: list[str] = []
        self._stderr_tail: deque[str] = deque(maxlen=200)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._sse_thread: threading.Thread | None = None
        self._sse_resp: Any = None
        self._sse_stop = threading.Event()
        self._sse_connected = threading.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    # -- env -----------------------------------------------------------------
    def _build_server_env(self) -> dict[str, str]:
        """Compose the child env: hermetic config + private SQLite data dir.

        Reuses run mode's :func:`build_env` (OPENCODE_CONFIG_CONTENT, autoupdate,
        PWD) and :func:`_isolate_user_config` (sanitized XDG_CONFIG_HOME) so the
        session server is isolated exactly like an ``opencode run`` subprocess.
        A private ``XDG_DATA_HOME`` tmpdir keeps the session's SQLite DB off the
        host's global ``opencode.db`` and is removed on :meth:`aclose`.
        """
        env = build_env(self._run_cfg, cwd=self._workspace)
        if not self._run_cfg.inherit_user_config:
            cfg_tmp = tempfile.mkdtemp(prefix="oc_srv_cfg_")
            self._cleanup_dirs.append(cfg_tmp)
            env = _isolate_user_config(dict(env), Path(cfg_tmp))

        data_tmp = tempfile.mkdtemp(prefix="oc_srv_data_")
        self._cleanup_dirs.append(data_tmp)
        real_xdg = env.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        real_auth = Path(real_xdg) / "opencode" / "auth.json"
        if real_auth.is_file():
            iso_oc = Path(data_tmp) / "opencode"
            iso_oc.mkdir(parents=True, exist_ok=True)
            link = iso_oc / "auth.json"
            if not link.exists():
                link.symlink_to(real_auth)
        env["XDG_DATA_HOME"] = data_tmp
        return env

    # -- lifecycle -----------------------------------------------------------
    async def start(self, *, health_timeout_s: float = 15.0) -> None:
        self._loop = asyncio.get_running_loop()
        env = self._build_server_env()
        self._proc = await asyncio.create_subprocess_exec(
            self._binary, "serve", "--port", str(self._port), "--hostname", "127.0.0.1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace,
            env=env,
        )
        self._tasks.append(asyncio.create_task(self._drain_stderr()))

        deadline = self._loop.time() + health_timeout_s
        while True:
            if self._proc.returncode is not None:
                raise OpenCodeProcessError(
                    exit_code=self._proc.returncode,
                    stderr="".join(self._stderr_tail),
                    events=[],
                    raw_stdout_lines=[],
                )
            try:
                await asyncio.to_thread(self._get_sync, "/session")
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                if self._loop.time() >= deadline:
                    await self.aclose()
                    raise OpenCodeProcessError(
                        exit_code=-1,
                        stderr="opencode serve did not become healthy in "
                        f"{health_timeout_s}s\n" + "".join(self._stderr_tail),
                        events=[],
                        raw_stdout_lines=[],
                    )
                await asyncio.sleep(0.1)

        # Connect the SSE bus before any prompt is sent so no events are missed.
        self._sse_thread = threading.Thread(target=self._run_sse, daemon=True)
        self._sse_thread.start()
        await asyncio.to_thread(self._sse_connected.wait, 5.0)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            self._stderr_tail.append(line.decode(errors="replace"))

    async def aclose(self) -> None:
        self._sse_stop.set()
        if self._sse_resp is not None:
            try:
                self._sse_resp.close()
            except Exception:
                pass
        for t in self._tasks:
            t.cancel()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
            except ProcessLookupError:
                pass
        for d in self._cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)

    # -- SSE -----------------------------------------------------------------
    def _run_sse(self) -> None:
        try:
            resp = urllib.request.urlopen(self.base + "/event", timeout=None)
            self._sse_resp = resp
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._sse_connected.set)
            for raw in resp:
                if self._sse_stop.is_set():
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._loop.call_soon_threadsafe(self._dispatch, ev)
        except Exception as exc:  # noqa: BLE001 - surface to consumers as an event
            if self._loop is not None and not self._sse_stop.is_set():
                self._loop.call_soon_threadsafe(
                    self._dispatch, {"type": "_sse_error", "error": repr(exc)}
                )
        finally:
            self._sse_connected.set()

    def _dispatch(self, ev: dict[str, Any]) -> None:
        if ev.get("type") == "_sse_error":
            for q in self._queues.values():
                q.put_nowait(ev)
            return
        sid = _event_session_id(ev)
        if sid is not None and sid in self._queues:
            self._queues[sid].put_nowait(ev)

    def subscribe(self, session_id: str) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[session_id] = q
        return q

    def unsubscribe(self, session_id: str) -> None:
        self._queues.pop(session_id, None)

    # -- HTTP (stdlib, run in a thread) --------------------------------------
    def _request_sync(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def _get_sync(self, path: str) -> Any:
        return self._request_sync("GET", path, None)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._request_sync, "POST", path, body)

    async def get(self, path: str) -> Any:
        return await asyncio.to_thread(self._request_sync, "GET", path, None)

    async def delete(self, path: str) -> Any:
        return await asyncio.to_thread(self._request_sync, "DELETE", path, None)
