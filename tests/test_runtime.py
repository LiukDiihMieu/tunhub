"""Relay and service runtime tests, using local servers and a fake ssh binary."""

import asyncio

from tunhub.net import TcpRelay
from tunhub.service import ServiceRuntime, State

from .helpers import free_port, make_config, roundtrip, start_echo_server, start_http_server, wait_until


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def make_runtime(service_raw: dict, **tunnel) -> ServiceRuntime:
    config = make_config({"svc": service_raw}, **tunnel)
    return ServiceRuntime(config.services["svc"], config.tunnel)


def test_relay_roundtrip_and_connect_error():
    async def scenario():
        server, target_port = await start_echo_server()
        errors = []
        relay = TcpRelay("127.0.0.1", free_port(), "127.0.0.1", target_port, 2, errors.append)
        await relay.start()
        payload = b"x" * 300_000
        assert await roundtrip(relay.listen_port, payload) == payload

        server.close()
        await server.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.listen_port)
        assert await reader.read() == b""  # relay closes the client side
        writer.close()
        await relay.stop()
        # Windows reports a refused loopback connect only after ~2 s, so it may time out
        # instead; on Python 3.10 asyncio.TimeoutError is not an OSError.
        assert errors and isinstance(errors[0], (OSError, asyncio.TimeoutError))

    run(scenario())


def test_direct_route_preferred_when_reachable():
    async def scenario():
        _, target = await start_echo_server()
        runtime = make_runtime(
            {"type": "tcp", "local_port": free_port(),
             "routes": [{"direct": f"127.0.0.1:{target}"}, {"ssh": "lab", "remote": target}]}
        )
        assert await runtime.ensure_up(10)
        assert runtime.active_route == 0
        assert await roundtrip(runtime.service.local_port, b"ping") == b"ping"
        await runtime.stop()
        assert runtime.state is State.OFF

    run(scenario())


def test_falls_back_to_ssh_when_direct_is_down():
    async def scenario():
        _, target = await start_echo_server()
        runtime = make_runtime(
            {"type": "tcp", "local_port": free_port(),
             "routes": [{"direct": f"127.0.0.1:{free_port()}"}, {"ssh": "lab", "remote": target}]}
        )
        assert await runtime.ensure_up(10)
        assert runtime.active_route == 1
        assert await roundtrip(runtime.service.local_port, b"via ssh") == b"via ssh"
        await runtime.stop()

    run(scenario())


def test_ssh_reconnects_after_process_dies():
    async def scenario():
        _, target = await start_echo_server()
        runtime = make_runtime({"type": "tcp", "local_port": free_port(),
                                "routes": [{"ssh": "lab", "remote": target}]})
        assert await runtime.ensure_up(10)
        first_pid = runtime._ssh.pid
        runtime._ssh._proc.kill()

        await wait_until(lambda: runtime.state is State.UP and runtime._ssh and runtime._ssh.pid != first_pid, runtime=runtime)
        assert await roundtrip(runtime.service.local_port, b"again") == b"again"
        assert any("route down: ssh exited" in m for _, m in runtime.logs)
        await runtime.stop()

    run(scenario())


def test_failure_reports_every_route_and_ssh_stderr():
    async def scenario():
        runtime = make_runtime(
            {"type": "tcp", "local_port": free_port(),
             "routes": [{"direct": f"127.0.0.1:{free_port()}"}, {"ssh": "unreachable", "remote": 22}]}
        )
        assert not await runtime.ensure_up(10)
        assert runtime.state is State.RETRYING
        assert "#1" in runtime.last_error and "Connection refused" in runtime.last_error
        await runtime.stop()

    run(scenario())


def test_pinned_route_skips_auto_selection():
    async def scenario():
        _, target = await start_echo_server()
        runtime = make_runtime(
            {"type": "tcp", "local_port": free_port(),
             "routes": [{"direct": f"127.0.0.1:{target}"}, {"ssh": "lab", "remote": target}]}
        )
        runtime.set_pinned_route(1)
        assert await runtime.ensure_up(10)
        assert runtime.active_route == 1

        runtime.set_pinned_route(None)  # back to auto -> re-detects -> direct
        await wait_until(lambda: runtime.state is State.UP and runtime.active_route == 0, runtime=runtime)
        await runtime.stop()

    run(scenario())


def test_web_health_check_through_ssh():
    async def scenario():
        _, http_port = await start_http_server()
        runtime = make_runtime({"local_port": free_port(), "routes": [{"ssh": "lab", "remote": http_port}]})
        assert await runtime.ensure_up(10)
        assert any("health check: HTTP 200" in m for _, m in runtime.logs)
        await runtime.stop()

    run(scenario())


def test_health_check_fails_when_web_service_is_down():
    async def scenario():
        # ssh connects fine but nothing listens on the remote port.
        runtime = make_runtime(
            {"local_port": free_port(), "routes": [{"ssh": "lab", "remote": free_port()}]},
            health_timeout=1,
        )
        assert not await runtime.ensure_up(10)
        assert "does not answer" in runtime.last_error
        await runtime.stop()

    run(scenario())


def test_busy_local_port_is_reported():
    async def scenario():
        blocker, busy_port = await start_echo_server()
        _, target = await start_echo_server()
        runtime = make_runtime({"type": "tcp", "local_port": busy_port,
                                "routes": [{"ssh": "lab", "remote": target}]})
        assert not await runtime.ensure_up(10)
        assert "leftover ssh" in runtime.last_error
        await runtime.stop()
        blocker.close()

    run(scenario())
