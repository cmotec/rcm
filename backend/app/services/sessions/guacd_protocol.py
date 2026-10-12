"""Guacamole protocol codec and guacd client.

Wire format: ``LENGTH.VALUE,LENGTH.VALUE,...;`` where LENGTH counts Unicode
code points. Python ``str`` indexes by code point, so parsing operates on
decoded text.
"""

from __future__ import annotations

import asyncio
import codecs
import ssl
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

PROTOCOL_VERSION = "VERSION_1_5_0"
INTERNAL_OPCODE = ""
MAX_INSTRUCTION_CHARS = 8 * 1024 * 1024  # guard against unbounded buffering


class GuacProtocolError(Exception):
    pass


class GuacdError(Exception):
    """guacd reported an error (``error`` instruction) or failed the handshake."""

    def __init__(self, message: str, status: int = 519) -> None:
        super().__init__(message)
        self.status = status


def encode(opcode: str, *args: object) -> str:
    parts = [opcode, *("" if a is None else str(a) for a in args)]
    return ",".join(f"{len(p)}.{p}" for p in parts) + ";"


def encode_many(instructions: Iterable[Sequence[str]]) -> str:
    return "".join(encode(i[0], *i[1:]) for i in instructions)


class Parser:
    """Incremental parser. Feed text, get complete instructions.

    ``feed_split`` returns the longest prefix made of complete instructions
    (for relaying in whole-instruction chunks) plus the parsed instructions.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._pos = 0  # start of the first incomplete instruction in _buf

    def feed(self, text: str) -> list[list[str]]:
        _, instructions = self.feed_split(text)
        return instructions

    def feed_split(self, text: str, *, collect: bool = True) -> tuple[str, list[list[str]]]:
        if self._pos:
            self._buf = self._buf[self._pos :]
            self._pos = 0
        buf = self._buf + text
        instructions: list[list[str]] = []
        start = 0
        n = len(buf)
        complete_end = 0
        while start < n:
            elements: list[str] = []
            pos = start
            done = False
            while True:
                dot = buf.find(".", pos, min(n, pos + 12))
                if dot == -1:
                    if n - pos >= 12:
                        raise GuacProtocolError("invalid element length")
                    break  # need more data
                length_str = buf[pos:dot]
                if not length_str.isdigit():
                    raise GuacProtocolError(f"invalid element length {length_str[:12]!r}")
                length = int(length_str)
                end = dot + 1 + length
                if end >= n:
                    break  # value or terminator not yet received
                term = buf[end]
                if collect:
                    elements.append(buf[dot + 1 : end])
                if term == ";":
                    done = True
                    pos = end + 1
                    break
                if term != ",":
                    raise GuacProtocolError(f"unexpected terminator {term!r}")
                pos = end + 1
            if not done:
                break
            if collect:
                instructions.append(elements)
            start = pos
            complete_end = pos
        if n - complete_end > MAX_INSTRUCTION_CHARS:
            raise GuacProtocolError("instruction too large")
        self._buf = buf
        self._pos = complete_end
        return buf[:complete_end], instructions

    @property
    def pending(self) -> int:
        return len(self._buf) - self._pos


@dataclass
class HandshakeResult:
    connection_id: str
    requested_args: list[str]
    unsupported: list[str]


class GuacdClient:
    """A single connection to guacd."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._parser = Parser()
        self._pending: list[list[str]] = []

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout: float = 10.0,
        tls: bool = False,
        tls_verify: bool = True,
        ca_file: str | None = None,
    ) -> GuacdClient:
        ssl_ctx: ssl.SSLContext | None = None
        if tls:
            ssl_ctx = ssl.create_default_context(cafile=ca_file)
            if not tls_verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx, limit=2**20), timeout
            )
        except (TimeoutError, OSError) as exc:
            raise GuacdError(f"cannot reach guacd at {host}:{port}: {exc}", status=514) from exc
        return cls(reader, writer)

    async def send(self, text: str) -> None:
        self.writer.write(text.encode("utf-8"))
        await self.writer.drain()

    async def read_instruction(self) -> list[str]:
        while not self._pending:
            data = await self.reader.read(65536)
            if not data:
                raise GuacdError("guacd closed the connection", status=519)
            self._pending.extend(self._parser.feed(self._decoder.decode(data)))
        return self._pending.pop(0)

    async def read_chunk(self) -> str | None:
        """Read the next chunk of whole instructions as raw text; ``None`` on EOF."""
        if self._pending:
            raise GuacProtocolError("read_chunk() after partial instruction reads")
        while True:
            data = await self.reader.read(65536)
            if not data:
                return None
            chunk, _ = self._parser.feed_split(self._decoder.decode(data), collect=False)
            if chunk:
                return chunk

    async def handshake(
        self,
        protocol: str,
        params: dict[str, str],
        *,
        width: int,
        height: int,
        dpi: int,
        audio: Sequence[str] = (),
        video: Sequence[str] = (),
        image: Sequence[str] = ("image/png", "image/jpeg", "image/webp"),
        timezone: str | None = None,
        name: str | None = None,
        timeout: float = 20.0,
    ) -> HandshakeResult:
        async def _do() -> HandshakeResult:
            await self.send(encode("select", protocol))
            instr = await self.read_instruction()
            if instr[0] == "error":
                raise GuacdError(instr[1] if len(instr) > 1 else "guacd error", _status(instr))
            if instr[0] != "args":
                raise GuacdError(f"expected 'args', got {instr[0]!r}")
            requested = instr[1:]
            version = requested[0] if requested and requested[0].startswith("VERSION_") else None
            arg_names = requested[1:] if version else requested

            theirs = _version_tuple(version)
            out = [
                encode("size", width, height, dpi),
                encode("audio", *audio),
                encode("video", *video),
                encode("image", *image),
            ]
            if timezone and theirs >= (1, 1, 0):
                out.append(encode("timezone", timezone))
            if name and theirs >= (1, 5, 0):
                out.append(encode("name", name))
            values = [params.get(a, "") for a in arg_names]
            if version:
                ours = _version_tuple(PROTOCOL_VERSION)
                values.insert(0, PROTOCOL_VERSION if theirs >= ours else version)
            out.append(encode("connect", *values))
            await self.send("".join(out))

            instr = await self.read_instruction()
            if instr[0] == "error":
                raise GuacdError(instr[1] if len(instr) > 1 else "guacd error", _status(instr))
            if instr[0] != "ready":
                raise GuacdError(f"expected 'ready', got {instr[0]!r}")
            unsupported = sorted(k for k, v in params.items() if v != "" and k not in arg_names)
            return HandshakeResult(
                connection_id=instr[1] if len(instr) > 1 else "",
                requested_args=list(arg_names),
                unsupported=unsupported,
            )

        try:
            return await asyncio.wait_for(_do(), timeout)
        except TimeoutError as exc:
            raise GuacdError("guacd handshake timed out", status=520) from exc

    def leftover(self) -> str:
        """Instructions already read from guacd but not yet consumed (after the handshake)."""
        out = encode_many(self._pending)
        self._pending.clear()
        return out

    async def close(self) -> None:
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except (TimeoutError, OSError, ssl.SSLError):
            pass


def _version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return (1, 0, 0)
    try:
        return tuple(int(p) for p in version.removeprefix("VERSION_").split("_"))
    except ValueError:
        return (1, 0, 0)


def _status(instr: list[str]) -> int:
    try:
        return int(instr[2])
    except (IndexError, ValueError):
        return 519
