"""SSH tunnel for guacd-backed sessions (RDP/VNC through an SSH connection).

The backend listens on an ephemeral port on its address in the guacd network,
accepts connections only from guacd's address, and forwards each one via an
SSH ``direct-tcpip`` channel to the real target.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import asyncssh

from app.config import Settings
from app.logging import get_logger
from app.services.sessions.ssh_native import SshConnectionSet

log = get_logger(__name__)


async def _resolve_all(host: str) -> set[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return set()
    return {i[4][0] for i in infos}


def _local_address_towards(host: str, port: int) -> str:
    """The local IP the kernel would use to reach guacd (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, port))
        return s.getsockname()[0]


class SshTunnel:
    def __init__(self, conns: SshConnectionSet, dest_host: str, dest_port: int, allowed_peers: set[str]):
        self.conns = conns
        self.dest_host = dest_host
        self.dest_port = dest_port
        self.allowed_peers = allowed_peers
        self.server: asyncio.base_events.Server | None = None
        self.bind_host = ""
        self.port = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @classmethod
    async def start(cls, conns: SshConnectionSet, dest_host: str, dest_port: int, settings: Settings) -> SshTunnel:
        loop = asyncio.get_running_loop()
        guacd_ips = await _resolve_all(settings.GUACD_HOST)
        bind = settings.SSH_TUNNEL_BIND_HOST
        if not bind:
            target = next(iter(guacd_ips), settings.GUACD_HOST)
            bind = await loop.run_in_executor(None, _local_address_towards, target, settings.GUACD_PORT)
        tunnel = cls(conns, dest_host, dest_port, guacd_ips)
        tunnel.server = await asyncio.start_server(tunnel._handle, host=bind, port=0)
        tunnel.bind_host, tunnel.port = tunnel.server.sockets[0].getsockname()[:2]
        log.info("ssh_tunnel_started", bind=tunnel.bind_host, port=tunnel.port, dest=f"{dest_host}:{dest_port}")
        return tunnel

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else None
        if self.allowed_peers and peer_ip not in self.allowed_peers:
            log.warning("ssh_tunnel_peer_rejected", peer=peer_ip)
            writer.close()
            return
        try:
            r2, w2 = await self.conns.conn.open_connection(self.dest_host, self.dest_port)
        except (asyncssh.Error, OSError) as exc:
            log.warning("ssh_tunnel_open_failed", error=str(exc)[:200])
            writer.close()
            return

        async def pipe(src: asyncio.StreamReader | asyncssh.SSHReader, dst) -> None:  # noqa: ANN001
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncssh.Error, ConnectionError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        task = asyncio.ensure_future(asyncio.gather(pipe(reader, w2), pipe(r2, writer)))
        self._tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(self._tasks.discard)  # type: ignore[arg-type]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
        for t in list(self._tasks):
            t.cancel()
        self.conns.close()
        await self.conns.wait_closed()
