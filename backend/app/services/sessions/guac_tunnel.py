"""WebSocket relay between guacamole-common-js and guacd.

See docs/guacd-integration.md for the handshake, framing and filtering rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.config import Settings
from app.db import session_scope
from app.errors import AppError
from app.logging import get_logger
from app.services.sessions import guacd_params
from app.services.sessions.guacd_protocol import (
    INTERNAL_OPCODE,
    GuacdClient,
    GuacdError,
    GuacProtocolError,
    Parser,
    encode,
)
from app.services.sessions.manager import Session, SessionManager, Stopped, unless_stopped
from app.services.sessions.ssh_native import open_connection_set, trusted_key
from app.services.sessions.ssh_tunnel import SshTunnel

log = get_logger(__name__)

# Opcodes a browser may send after the handshake.
CLIENT_OPCODES = {
    "key", "mouse", "touch", "size", "clipboard", "blob", "end", "ack", "file", "pipe", "put", "get",
    "audio", "argv", "sync", "nop", "disconnect",
}
UPLOAD_OPCODES = {"file", "put", "pipe"}
_AUDIO = re.compile(r"^audio/L(8|16)(;rate=\d{4,6},channels=[12])?$")
_IMAGE = {"image/png", "image/jpeg", "image/webp"}
_TZ = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")


@dataclass
class ClientInfo:
    width: int
    height: int
    dpi: int
    timezone: str | None
    audio: list[str]
    image: list[str]


def parse_client_info(params: dict[str, list[str]] | object, launch_settings: object) -> ClientInfo:
    q = params  # starlette QueryParams supports getlist()

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(q.get(name) or default)  # type: ignore[attr-defined]
        except ValueError:
            v = default
        return max(lo, min(hi, v))

    width = _int("width", 1024, 320, 8192)
    height = _int("height", 768, 200, 8192)
    dpi = _int("dpi", 96, 48, 480)
    fixed_w = getattr(launch_settings, "width", None)
    fixed_h = getattr(launch_settings, "height", None)
    if fixed_w and fixed_h:
        width, height = fixed_w, fixed_h
    if getattr(launch_settings, "dpi", None):
        dpi = launch_settings.dpi  # type: ignore[attr-defined]
    tz = q.get("tz")  # type: ignore[attr-defined]
    audio = [a for a in q.getlist("audio") if _AUDIO.match(a)][:8]  # type: ignore[attr-defined]
    image = [i for i in q.getlist("image") if i in _IMAGE] or ["image/png", "image/jpeg"]  # type: ignore[attr-defined]
    return ClientInfo(width, height, dpi, tz if tz and _TZ.match(tz) else None, audio, image)


def _known_hosts_line(host: str, port: int, public_key: str) -> str:
    entry = host if port == 22 else f"[{host}]:{port}"
    parts = public_key.split()
    return f"{entry} {parts[0]} {parts[1]}" if len(parts) >= 2 else ""


async def run_guac_tunnel(ws: WebSocket, session: Session, manager: SessionManager, settings: Settings,
                          info: ClientInfo) -> None:
    launch = session.launch
    client: GuacdClient | None = None
    tunnel: SshTunnel | None = None
    stop = asyncio.Event()

    async def detach() -> None:
        stop.set()
        with contextlib.suppress(Exception):
            await ws.close(code=1000, reason="0")

    manager.begin_attach(session, detach)
    error: str | None = None

    async def open_guacd() -> None:
        """SSH tunnel, guacd connection and handshake (guacd connects to the remote host meanwhile)."""
        nonlocal client, tunnel
        host, port = launch.host, launch.port
        if launch.tunnel_via is not None:
            async with session_scope() as db:
                conns = await open_connection_set(db, launch.tunnel_via, settings)
            try:
                tunnel = await SshTunnel.start(conns, launch.host, launch.port, settings)
            except BaseException:
                conns.close()
                raise
            host, port = tunnel.bind_host, tunnel.port

        params: dict[str, str]
        if session.protocol == "rdp":
            params = guacd_params.rdp_params(launch, settings, user_id=session.owner_id, session_id=session.id,
                                             client_timezone=info.timezone, host=host, port=port)
        elif session.protocol == "vnc":
            params = guacd_params.vnc_params(launch, settings, user_id=session.owner_id, session_id=session.id,
                                             host=host, port=port)
        else:  # ssh via guacd
            async with session_scope() as db:
                keys = await trusted_key(db, launch.host, launch.port)
            host_key = _known_hosts_line(launch.host, launch.port, keys[0].export_public_key("openssh").decode()) \
                if keys else (launch.settings.pinned_host_key or "")
            params = guacd_params.ssh_params(launch, settings, user_id=session.owner_id, session_id=session.id,
                                             client_timezone=info.timezone, host=host, port=port,
                                             host_key=host_key)

        client = await GuacdClient.connect(
            settings.GUACD_HOST, settings.GUACD_PORT, timeout=settings.GUACD_CONNECT_TIMEOUT_SECONDS,
            tls=settings.GUACD_TLS, tls_verify=settings.GUACD_TLS_VERIFY, ca_file=settings.GUACD_CA_FILE,
        )
        result = await client.handshake(
            session.protocol, params, width=info.width, height=info.height, dpi=info.dpi,
            audio=info.audio, image=info.image, timezone=info.timezone, name=session.owner_name,
            timeout=settings.GUACD_HANDSHAKE_TIMEOUT_SECONDS,
        )
        if result.unsupported:
            log.warning("guacd_params_unsupported", params=result.unsupported, protocol=session.protocol)

    try:
        # closing the session (e.g. Cancel while the host does not answer) stops connecting at once
        await unless_stopped(open_guacd(), stop)
        assert client is not None
        await ws.send_text(encode(INTERNAL_OPCODE, str(session.id)))
        leftover = client.leftover()
        if leftover:
            await ws.send_text(leftover)
        await manager.mark_connected(session)
        await _relay(ws, client, session, stop)
    except GuacdError as exc:
        error = str(exc)
        log.info("guacd_session_error", error=error, status=exc.status, session_id=str(session.id))
        await _send_error(ws, error, exc.status)
    except AppError as exc:
        error = exc.detail
        await _send_error(ws, exc.detail, 519)
    except (WebSocketDisconnect, asyncio.CancelledError, Stopped):
        pass
    except GuacProtocolError as exc:
        error = f"protocol error: {exc}"
        log.warning("guac_protocol_violation", error=str(exc), session_id=str(session.id))
        await _send_error(ws, "protocol error", 768)
    except Exception as exc:  # noqa: BLE001
        error = "internal error"
        log.exception("guac_tunnel_failed", session_id=str(session.id), error=str(exc)[:200])
        await _send_error(ws, "internal error", 512)
    finally:
        manager.detach_now(session, error=error)  # before any await: cleanup may be cancelled

        async def cleanup() -> None:
            if client is not None:
                await client.close()
            if tunnel is not None:
                await tunnel.close()
            with contextlib.suppress(Exception):
                if ws.application_state != WebSocketState.DISCONNECTED:
                    await ws.close(code=1000)
            await manager.end_attach(session, error=error)

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(cleanup())


async def _send_error(ws: WebSocket, message: str, status: int) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(encode("error", message[:500], status))
        await ws.close(code=1000, reason=str(status))


async def _relay(ws: WebSocket, client: GuacdClient, session: Session, stop: asyncio.Event) -> None:
    allow_upload = bool(session.client_settings.get("drive_enabled")) and not session.client_settings.get(
        "disable_upload"
    )
    read_only = bool(session.client_settings.get("read_only"))

    async def upstream() -> None:
        while True:
            chunk = await client.read_chunk()
            if chunk is None:
                return
            await ws.send_text(chunk)
            session.touch()

    async def downstream() -> None:
        parser = Parser()
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            text = msg.get("text")
            if text is None:
                data = msg.get("bytes") or b""
                text = data.decode("utf-8", errors="strict")
            out: list[str] = []
            for instr in parser.feed(text):
                op = instr[0]
                if op == INTERNAL_OPCODE:
                    if len(instr) >= 3 and instr[1] == "ping":
                        await ws.send_text(encode(INTERNAL_OPCODE, "ping", instr[2]))
                    continue
                if op not in CLIENT_OPCODES:
                    raise GuacProtocolError(f"client sent forbidden opcode {op!r}")
                if op in UPLOAD_OPCODES and not allow_upload and session.protocol == "rdp":
                    continue  # uploads disabled by policy
                if read_only and op in ("key", "mouse", "touch", "clipboard"):
                    continue
                out.append(encode(op, *instr[1:]))
            if out:
                await client.send("".join(out))
                session.touch()

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream()),
             asyncio.create_task(stop.wait())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                raise exc
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
