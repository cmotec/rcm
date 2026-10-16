"""WebSocket relay between xterm.js and a native SSH session (asyncssh).

Frames
------
server → browser: binary = terminal output; text = JSON control messages
  {"type": "status", "state": "connecting" | "connected"}
  {"type": "error", "code": "...", "message": "..."}
  {"type": "exit", "status": <int | null>}
  {"type": "pong"}                    (answer to ping; the browser's heartbeat)
browser → server: binary = keyboard input; text = JSON control messages
  {"type": "resize", "cols": <int>, "rows": <int>}
  {"type": "ping"}
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import asyncssh
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.config import Settings
from app.db import session_scope
from app.errors import AppError
from app.logging import get_logger
from app.services.sessions.manager import Session, SessionManager
from app.services.sessions.ssh_native import SshConnectionSet, open_connection_set

log = get_logger(__name__)


def _clamp(v: object, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


async def run_terminal(ws: WebSocket, session: Session, manager: SessionManager, settings: Settings,
                       cols: int, rows: int) -> None:
    launch = session.launch
    conns: SshConnectionSet | None = None
    process: asyncssh.SSHClientProcess | None = None  # type: ignore[type-arg]
    stop = asyncio.Event()

    async def detach() -> None:
        stop.set()
        with contextlib.suppress(Exception):
            await ws.close(code=1000)

    manager.begin_attach(session, detach)
    error: str | None = None
    try:
        await ws.send_text(json.dumps({"type": "status", "state": "connecting"}))
        async with session_scope() as db:
            conns = await open_connection_set(db, launch, settings)
        s = launch.settings
        process = await conns.conn.create_process(
            s.command or None,
            term_type=s.term_type or "xterm-256color",
            term_size=(cols, rows),
            encoding=None,
            stderr=asyncssh.STDOUT,
        )
        await manager.mark_connected(session)
        await ws.send_text(json.dumps({"type": "status", "state": "connected"}))
        await _relay(ws, process, session, stop)
        if process.exit_status is not None or process.exit_signal is not None:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"type": "exit", "status": process.exit_status}))
    except AppError as exc:
        error = exc.detail
        await _send_error(ws, exc.code, exc.detail)
    except asyncssh.ChannelOpenError as exc:
        error = f"cannot open shell: {exc.reason}"
        await _send_error(ws, "channel_open_failed", error)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except (OSError, asyncssh.Error) as exc:
        error = f"connection lost: {exc}"
        await _send_error(ws, "connection_lost", error)
    except Exception as exc:  # noqa: BLE001
        error = "internal error"
        log.exception("terminal_failed", session_id=str(session.id), error=str(exc)[:200])
        await _send_error(ws, "internal_error", "internal error")
    finally:
        manager.detach_now(session, error=error)  # before any await: cleanup may be cancelled
        if process is not None:
            process.close()
        if conns is not None:
            conns.close()

        async def cleanup() -> None:
            with contextlib.suppress(Exception):
                if ws.application_state != WebSocketState.DISCONNECTED:
                    await ws.close(code=1000)
            await manager.end_attach(session, error=error)

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(cleanup())


async def _send_error(ws: WebSocket, code: str, message: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({"type": "error", "code": code, "message": message[:1000]}))


async def _relay(ws: WebSocket, process: asyncssh.SSHClientProcess, session: Session,  # type: ignore[type-arg]
                 stop: asyncio.Event) -> None:
    async def upstream() -> None:
        while True:
            data = await process.stdout.read(65536)
            if not data:
                return
            await ws.send_bytes(data)
            session.touch()

    async def downstream() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                process.stdin.write(msg["bytes"])
                session.touch()
                continue
            try:
                ctl = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            kind = ctl.get("type")
            if kind == "resize":
                process.change_terminal_size(_clamp(ctl.get("cols"), 2, 1000, 80), _clamp(ctl.get("rows"), 1, 500, 24))
            elif kind == "input" and isinstance(ctl.get("data"), str):
                process.stdin.write(ctl["data"].encode("utf-8"))
                session.touch()
            elif kind == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream()),
             asyncio.create_task(stop.wait())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError, asyncssh.Error)):
                raise exc
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
