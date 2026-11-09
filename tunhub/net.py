"""Low-level networking: TCP relay, reachability probes and HTTP health check."""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys
from typing import Callable

BUFFER_SIZE = 64 * 1024


class TcpRelay:
    """Byte-level TCP forwarder (like ``socat TCP-LISTEN:a TCP:host:b``).

    It never parses the payload, so HTTP, WebSocket, RDP, ... all pass through.
    """

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        target_host: str,
        target_port: int,
        connect_timeout: float,
        on_connect_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.connect_timeout = connect_timeout
        self._on_connect_error = on_connect_error
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def active_connections(self) -> int:
        # Each relayed connection owns a client writer and an upstream writer.
        return len(self._writers) // 2

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), 3)
        except asyncio.TimeoutError:
            pass
        self._server = None

    async def _handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        self._writers.add(client_writer)
        try:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(self.target_host, self.target_port),
                    self.connect_timeout,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                if self._on_connect_error is not None:
                    self._on_connect_error(exc)
                return

            self._writers.add(upstream_writer)

            def abort() -> None:
                client_writer.close()
                upstream_writer.close()

            try:
                await asyncio.gather(
                    _pipe(client_reader, upstream_writer, abort),
                    _pipe(upstream_reader, client_writer, abort),
                )
            finally:
                self._writers.discard(upstream_writer)
                upstream_writer.close()
        finally:
            self._writers.discard(client_writer)
            client_writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, abort: Callable[[], None]) -> None:
    try:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        # Propagate half-close so request/response protocols finish cleanly.
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError, RuntimeError):
        abort()


async def probe_tcp(host: str, port: int, timeout: float) -> str:
    """Open and close a TCP connection; return the peer IP it reached.

    Raises OSError/TimeoutError if the connection cannot be opened.
    """
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    peer_ip = writer.get_extra_info("peername")[0]
    writer.close()
    return peer_ip


async def is_port_open(host: str, port: int, timeout: float) -> bool:
    try:
        await probe_tcp(host, port, timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    return True


def ensure_port_free(host: str, port: int) -> None:
    """Raise OSError if host:port cannot be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # On POSIX this ignores TIME_WAIT leftovers. On Windows SO_REUSEADDR
        # would allow stealing a live port, so it is not set there.
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))


async def http_probe(port: int, host_header: str, path: str, use_tls: bool, timeout: float) -> int:
    """Send one GET to 127.0.0.1:port and return the HTTP status code.

    Any status (even 401/403/500) proves that the web service answered.
    """
    context = None
    if use_tls:
        # Self-signed certificates are the norm for LAN services.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            "127.0.0.1",
            port,
            ssl=context,
            server_hostname=host_header.split(":")[0] if use_tls else None,
        ),
        timeout,
    )
    try:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "User-Agent: tunhub\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout)
    finally:
        writer.close()

    parts = status_line.split()
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        raise ValueError(f"not an HTTP response: {status_line[:60]!r}")
    return int(parts[1])
