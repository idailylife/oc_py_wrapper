"""Spike: drive opencode in *server mode* with a permission callback.

Goal of this throwaway script: prove that a stdlib-only async client can
- spawn `opencode serve`
- create a session
- subscribe to the `/event` SSE bus
- send a prompt
- answer a `permission.updated` request via a Python callback (once/always/reject)
- aggregate the assistant's final text

If this runs green end-to-end, the server-mode architecture is viable and we can
judge how much of the existing `opencode run` machinery it would replace/simplify.

Run:  .venv/bin/python research/server_mode_spike.py
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, Optional

HOST = "127.0.0.1"
PORT = 4099
BASE = f"http://{HOST}:{PORT}"
MODEL = {"providerID": "opencode", "modelID": "big-pickle"}

# response = one of "once" | "always" | "reject"
PermissionCallback = Callable[[dict[str, Any]], Awaitable[str]]


# ---------------------------------------------------------------------------
# tiny stdlib HTTP helpers (unary requests run in a thread via asyncio.to_thread)
# ---------------------------------------------------------------------------
def _post(path: str, body: dict[str, Any]) -> Any:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _get(path: str) -> Any:
    with urllib.request.urlopen(BASE + path, timeout=10) as resp:
        return json.loads(resp.read())


async def post(path: str, body: dict[str, Any]) -> Any:
    return await asyncio.to_thread(_post, path, body)


# ---------------------------------------------------------------------------
# SSE listener: blocking urllib read in a thread, events pushed to an asyncio.Queue
# ---------------------------------------------------------------------------
def _sse_thread(loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[dict]", stop: threading.Event) -> None:
    try:
        with urllib.request.urlopen(BASE + "/event", timeout=600) as resp:
            for raw_line in resp:
                if stop.is_set():
                    break
                line = raw_line.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, ev)
    except Exception as exc:  # noqa: BLE001 - spike
        loop.call_soon_threadsafe(queue.put_nowait, {"type": "_sse_error", "error": repr(exc)})


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------
async def spawn_server() -> asyncio.subprocess.Process:
    env = {
        **_os_environ(),
        # ask before running bash so we can exercise the permission callback
        "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"bash": "ask"}}),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        "opencode", "serve", "--port", str(PORT), "--hostname", HOST,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    # poll until the HTTP API answers
    for _ in range(100):
        try:
            await asyncio.to_thread(_get, "/session")
            return proc
        except (urllib.error.URLError, ConnectionError, OSError):
            await asyncio.sleep(0.1)
    raise RuntimeError("server did not come up")


def _os_environ() -> dict[str, str]:
    import os
    return dict(os.environ)


# ---------------------------------------------------------------------------
# the actual run loop
# ---------------------------------------------------------------------------
async def run_prompt(text: str, on_permission: PermissionCallback) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[dict]" = asyncio.Queue()
    stop = threading.Event()
    t = threading.Thread(target=_sse_thread, args=(loop, queue, stop), daemon=True)
    t.start()
    await asyncio.sleep(0.3)  # let SSE connect

    session = await post("/session", {})
    sid = session["id"]

    # fire the prompt asynchronously; completion is signalled by session.idle on SSE
    await post(
        f"/session/{sid}/prompt_async",
        {"model": MODEL, "parts": [{"type": "text", "text": text}]},
    )

    final_text: list[str] = []
    permissions_asked: list[str] = []
    answered: list[tuple[str, str]] = []

    while True:
        ev = await asyncio.wait_for(queue.get(), timeout=120)
        etype = ev.get("type")
        props = ev.get("properties", {})

        # NOTE: validated wire shapes (this opencode version) — the SDK .d.ts
        # types disagree, trust these: the event is `permission.asked` (not
        # `permission.updated`), it has no `title`, and turn-done is
        # `session.status -> status.type == "idle"` (no top-level `session.idle`).
        if etype in ("permission.asked", "permission.updated") and props.get("sessionID") == sid:
            permissions_asked.append(props.get("permission", ""))
            decision = await on_permission(props)
            await post(
                f"/session/{sid}/permissions/{props['id']}",
                {"response": decision},
            )
            answered.append((props.get("permission", ""), decision))

        elif etype == "message.part.updated" and props.get("part", {}).get("sessionID") == sid:
            part = props.get("part", {})
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                # parts stream incrementally; keep the latest snapshot per part id
                final_text.append(part["text"])

        elif etype == "session.idle" and props.get("sessionID") == sid:
            break
        elif etype == "session.status" and props.get("sessionID") == sid:
            status = props.get("status")
            if isinstance(status, dict) and status.get("type") == "idle":
                break
        elif etype == "_sse_error":
            raise RuntimeError(f"SSE error: {ev.get('error')}")

    stop.set()
    return {
        "session_id": sid,
        "text": final_text[-1] if final_text else "",
        "permissions_asked": permissions_asked,
        "answered": answered,
    }


async def main() -> None:
    proc = await spawn_server()
    print(f"server up on {BASE} (pid {proc.pid})")

    async def approve(perm: dict[str, Any]) -> str:
        print(f"  [permission asked] title={perm.get('title')!r} type={perm.get('type')!r} -> once")
        return "once"

    try:
        # prompt that should require a bash tool call -> triggers permission.updated
        result = await run_prompt(
            "Run the shell command `echo hello-from-opencode` and tell me its output.",
            approve,
        )
        print("\n=== RESULT ===")
        print("session_id        :", result["session_id"])
        print("permissions_asked :", result["permissions_asked"])
        print("answered          :", result["answered"])
        print("final_text        :", repr(result["text"])[:300])
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
