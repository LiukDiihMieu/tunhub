from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

from tunhub.config import parse_config

FAKE_SSH = str(Path(__file__).with_name("fake_ssh.py"))


_handed_out_ports: set[int] = set()


def free_port() -> int:
    """Return a free local port that no earlier call in this test run returned.

    The OS may hand out the same ephemeral port twice in a row once the probing
    socket is closed, so two "free" ports picked for one config could collide.
    """
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _handed_out_ports:
            _handed_out_ports.add(port)
            return port


def make_config(services: dict, **tunnel):
    tunnel_raw = {
        "ssh_command": [sys.executable, FAKE_SSH],
        "probe_timeout": 0.5,
        "ready_timeout": 5,
        "health_timeout": 3,
        **tunnel,
    }
    return parse_config({"hub": {"port": free_port()}, "tunnel": tunnel_raw, "services": services})


async def start_echo_server(port: int = 0) -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    return server, server.sockets[0].getsockname()[1]


async def start_http_server(body: bytes = b"hello") -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def roundtrip(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.readexactly(len(payload)), 5)
    writer.close()
    return data


async def wait_until(predicate, timeout: float = 10, runtime=None) -> None:
    """Poll predicate; on timeout, include the runtime's log to ease debugging (CI)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            detail = ""
            if runtime is not None:
                start = runtime.logs[0][0] if runtime.logs else 0
                detail = "\n" + "\n".join(f"  +{t - start:6.2f}s {m}" for t, m in runtime.logs)
            raise AssertionError("condition not reached in time" + detail)
        await asyncio.sleep(0.05)
