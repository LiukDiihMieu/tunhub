"""Stand-in for the ssh binary in tests.

Understands ``-L bind:lport:host:hport <alias>`` and forwards locally.
Alias "unreachable" behaves like a host that refuses SSH connections.
"""

import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def main(argv):
    alias = argv[-1]
    if alias == "unreachable":
        print(f"ssh: connect to host {alias} port 22: Connection refused", file=sys.stderr)
        return 255

    forward = argv[argv.index("-L") + 1]
    bind_host, local_port, target_host, target_port = forward.rsplit(":", 3)

    async def handle(client_reader, client_writer):
        try:
            up_reader, up_writer = await asyncio.open_connection(target_host.strip("[]"), int(target_port))
        except OSError:
            print(f"channel 2: open failed: connect failed: Connection refused", file=sys.stderr, flush=True)
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer))

    try:
        server = await asyncio.start_server(handle, bind_host, int(local_port))
    except OSError as exc:
        print(f"bind [{bind_host}]:{local_port}: {exc.strerror}", file=sys.stderr)
        return 255
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
