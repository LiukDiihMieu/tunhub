"""Auto-direct: services come up by themselves when a direct route is reachable."""

import asyncio
import json

import pytest

from tunhub.config import parse_config
from tunhub.service import Mode, ServiceRuntime, State

from .helpers import free_port, make_config, roundtrip, start_echo_server, wait_until

FAST = {"auto_direct_interval": 0.2}


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def make_runtime(routes: list, **tunnel) -> ServiceRuntime:
    config = make_config({"svc": {"type": "tcp", "local_port": free_port(), "routes": routes}}, **{**FAST, **tunnel})
    return ServiceRuntime(config.services["svc"], config.tunnel)


async def kill_target(server: asyncio.AbstractServer, runtime: ServiceRuntime) -> None:
    """Stop the direct target and make one client connection so the relay notices."""
    server.close()
    await server.wait_closed()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", runtime.service.local_port)
        await reader.read()
        writer.close()
    except OSError:
        pass


def test_comes_up_automatically_and_goes_idle_without_ssh():
    async def scenario():
        server, target = await start_echo_server()
        runtime = make_runtime([{"direct": f"127.0.0.1:{target}"}, {"ssh": "lab", "remote": target}])
        runtime.activate()

        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime.mode is Mode.AUTO and runtime.active_route == 0
        assert await roundtrip(runtime.service.local_port, b"hi") == b"hi"

        await kill_target(server, runtime)
        await wait_until(lambda: runtime.state is State.OFF, runtime=runtime)
        await asyncio.sleep(0.5)  # several probe intervals
        assert runtime.state is State.OFF and runtime._ssh is None  # never fell back to ssh
        assert runtime.last_error is None

        # Target comes back ("arrived home") -> auto-connects again.
        server, _ = await start_echo_server(target)
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime.mode is Mode.AUTO
        await runtime.close()
        server.close()

    run(scenario())


def test_stop_ends_an_on_ssh_connection_and_returns_to_auto():
    async def scenario():
        _, ssh_target = await start_echo_server()
        dead = free_port()  # direct route unreachable, like away from home
        runtime = make_runtime([{"direct": f"127.0.0.1:{dead}"}, {"ssh": "lab", "remote": ssh_target}],
                               auto_direct_interval=30)
        runtime.activate()
        assert await runtime.ensure_up(10)  # opened while off: kept up via ssh
        assert runtime.mode is Mode.ON and runtime.active_route == 1

        # Back home: the direct target answers now. Stop goes back to auto and
        # probes at once (not after auto_direct_interval).
        server, _ = await start_echo_server(dead)
        await runtime.stop()
        assert runtime._ssh is None
        await wait_until(lambda: runtime.state is State.UP and runtime.mode is Mode.AUTO, timeout=5, runtime=runtime)
        assert runtime.active_route == 0
        await runtime.close()
        server.close()

    run(scenario())


def test_open_keeps_a_direct_auto_service_in_auto():
    # Opening a service that auto-direct already has up must not turn it into
    # an "on" one: a laptop leaving home would then fall back to ssh.
    async def scenario():
        server, target = await start_echo_server()
        _, ssh_target = await start_echo_server()
        runtime = make_runtime([{"direct": f"127.0.0.1:{target}"}, {"ssh": "lab", "remote": ssh_target}])
        runtime.activate()
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)

        assert await runtime.ensure_up(5)  # like clicking the tile or a bookmark
        assert runtime.mode is Mode.AUTO

        await kill_target(server, runtime)  # leaving home
        await wait_until(lambda: runtime.state is State.OFF, runtime=runtime)
        assert runtime._ssh is None  # no ssh tunnel was started
        await runtime.close()

    run(scenario())


def test_open_while_off_keeps_it_up_with_ssh_fallback():
    async def scenario():
        _, ssh_target = await start_echo_server()
        runtime = make_runtime([{"direct": f"127.0.0.1:{free_port()}"}, {"ssh": "lab", "remote": ssh_target}])
        runtime.activate()
        assert await runtime.ensure_up(10)
        assert runtime.mode is Mode.ON and runtime.active_route == 1
        await runtime.close()

    run(scenario())


def test_auto_probe_ignores_ssh_and_pinned_ssh_routes():
    async def scenario():
        _, target = await start_echo_server()
        ssh_only = make_runtime([{"ssh": "lab", "remote": target}])
        ssh_only.activate()
        assert ssh_only._watcher is None

        pinned = make_runtime([{"direct": f"127.0.0.1:{target}"}, {"ssh": "lab", "remote": target}])
        pinned.set_pinned_route(1)
        pinned.activate()
        await asyncio.sleep(0.6)
        assert pinned.state is State.OFF
        await pinned.close()

    run(scenario())


@pytest.mark.parametrize(
    "tunnel, service, expected",
    [({}, {}, True), ({"auto_direct": False}, {}, False), ({"auto_direct": False}, {"auto_direct": True}, True),
     ({}, {"auto_direct": False}, False)],
)
def test_auto_direct_config(tunnel, service, expected):
    raw = {"local_port": 1000, "routes": [{"direct": "10.0.0.1:80"}], **service}
    config = parse_config({"tunnel": tunnel, "services": {"svc": raw}})
    assert config.services["svc"].auto_direct is expected


def test_disconnect_ssh_keeps_direct_and_does_not_pause_auto(tmp_path):
    from tunhub.hub import Hub

    async def scenario():
        _, target = await start_echo_server()
        (tmp_path / "services.yaml").write_text(
            f"""
hub: {{port: {free_port()}}}
tunnel: {{ssh_command: {json.dumps(list(make_config({}).tunnel.ssh_command))}, auto_direct_interval: 0.2, probe_timeout: 0.5}}
services:
  direct:
    type: tcp
    local_port: {free_port()}
    routes: [{{direct: "127.0.0.1:{target}"}}]
  tunnel:
    type: tcp
    local_port: {free_port()}
    routes: [{{direct: "127.0.0.1:{free_port()}"}}, {{ssh: lab, remote: {target}}}]
"""
        )
        hub = Hub(tmp_path)
        await hub.start()
        direct, tunnel = hub.get("direct"), hub.get("tunnel")
        assert await tunnel.ensure_up(10) and tunnel.uses_ssh
        await wait_until(lambda: direct.state is State.UP, runtime=direct)
        assert not direct.uses_ssh

        assert await hub.disconnect_ssh() == ["tunnel"]
        assert tunnel.state is State.OFF and tunnel._ssh is None
        assert direct.state is State.UP
        assert tunnel.mode is Mode.OFF and not tunnel._watcher.done()  # back to auto
        await hub.shutdown()

    run(scenario())


def test_opening_that_lands_on_direct_settles_in_auto():
    # E.g. arriving home and clicking before the next auto-direct probe.
    async def scenario():
        _, target = await start_echo_server()
        _, ssh_target = await start_echo_server()
        runtime = make_runtime([{"direct": f"127.0.0.1:{target}"}, {"ssh": "lab", "remote": ssh_target}],
                               auto_direct_interval=30)
        runtime.activate()
        assert await runtime.ensure_up(5)
        assert runtime.active_route == 0 and runtime.mode is Mode.AUTO
        await runtime.close()

    run(scenario())


def test_without_auto_direct_a_direct_connection_stays_on():
    async def scenario():
        _, target = await start_echo_server()
        config = make_config({"svc": {"type": "tcp", "local_port": free_port(), "auto_direct": False,
                                      "routes": [{"direct": f"127.0.0.1:{target}"}]}})
        runtime = ServiceRuntime(config.services["svc"], config.tunnel)
        runtime.activate()  # no-op: auto-direct is off
        assert await runtime.ensure_up(5)
        assert runtime.mode is Mode.ON  # it has no auto mode to settle in
        await runtime.close()

    run(scenario())


def test_ssh_that_dies_at_home_reconnects_direct_and_settles_in_auto():
    # The laptop comes home: its ssh connection dies with the old network, the
    # reconnect tries the direct route first, and that settles in auto.
    async def scenario():
        _, ssh_target = await start_echo_server()
        home = free_port()
        runtime = make_runtime([{"direct": f"127.0.0.1:{home}"}, {"ssh": "lab", "remote": ssh_target}],
                               auto_direct_interval=30)
        runtime.activate()
        assert await runtime.ensure_up(10)
        assert runtime.mode is Mode.ON and runtime.active_route == 1

        server, _ = await start_echo_server(home)  # the direct target is reachable now
        runtime._ssh._proc.kill()  # the old network is gone
        await wait_until(lambda: runtime.state is State.UP and runtime.active_route == 0, runtime=runtime)
        assert runtime.mode is Mode.AUTO and runtime._ssh is None
        await runtime.close()
        server.close()

    run(scenario())
