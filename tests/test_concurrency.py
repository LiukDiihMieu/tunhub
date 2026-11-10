"""Races between start, stop, reload and shutdown (found in code review)."""

import asyncio
import sys

import pytest
import yaml

from tunhub.hub import Hub
from tunhub.service import Mode, ServiceRuntime, State

from .helpers import FAKE_SSH, free_port, make_config, start_echo_server, wait_until


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def supervisors() -> int:
    return sum(1 for task in asyncio.all_tasks() if task.get_name().startswith("service:") and not task.done())


async def ssh_runtime() -> ServiceRuntime:
    _, target = await start_echo_server()
    config = make_config(
        {"svc": {"type": "tcp", "local_port": free_port(), "routes": [{"ssh": "lab", "remote": target}]}}
    )
    runtime = ServiceRuntime(config.services["svc"], config.tunnel)
    runtime.start()
    await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
    return runtime


def test_start_during_stop_wins_and_stop_returns():
    async def scenario():
        runtime = await ssh_runtime()
        stopping = asyncio.create_task(runtime.stop())
        for _ in range(3):
            await asyncio.sleep(0)  # the supervisor is now tearing down
        runtime.start()

        await asyncio.wait_for(stopping, 5)  # used to hang forever
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime.mode is Mode.MANUAL and supervisors() == 1

        await runtime.stop()
        assert supervisors() == 0 and runtime.state is State.OFF

    run(scenario())


def test_start_while_supervisor_exits_never_runs_two():
    async def scenario():
        runtime = await ssh_runtime()
        stopping = asyncio.create_task(runtime.stop())
        while runtime.state is not State.OFF:
            await asyncio.sleep(0)  # the supervisor is in its `finally` block
        runtime.start()
        await stopping
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)

        runtime.start()  # used to create a second supervisor (and ssh)
        await asyncio.sleep(0.5)
        assert supervisors() == 1

        await runtime.stop()
        assert supervisors() == 0 and runtime._ssh is None

    run(scenario())


def test_closed_runtime_cannot_be_started_again():
    async def scenario():
        runtime = await ssh_runtime()
        await runtime.close()
        runtime.start()
        assert not await runtime.ensure_up(timeout=5)
        assert supervisors() == 0 and runtime.state is State.OFF

    run(scenario())


def test_overlapping_reloads_leave_one_runtime(tmp_path):
    async def scenario():
        _, target = await start_echo_server()
        local_port = free_port()

        def write_config(label: str) -> None:
            config = {
                "hub": {"port": free_port()},
                "tunnel": {"ssh_command": [sys.executable, FAKE_SSH]},
                "services": {
                    "svc": {
                        "type": "tcp",
                        "local_port": local_port,
                        "routes": [{"ssh": "lab", "remote": target, "label": label}],
                    }
                },
            }
            (tmp_path / "services.yaml").write_text(yaml.safe_dump(config))

        write_config("a")
        hub = Hub(tmp_path, tmp_path / "cache")
        await hub.start()
        hub.get("svc").start()
        await wait_until(lambda: hub.get("svc").state is State.UP, runtime=hub.get("svc"))

        write_config("b")
        await asyncio.gather(hub.reload(), hub.reload())  # e.g. a double click
        runtime = hub.get("svc")
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert supervisors() == 1

        await hub.shutdown()
        assert supervisors() == 0

    run(scenario())


@pytest.mark.parametrize("command", ["stop", "start"])
def test_stop_or_start_during_auto_probe_wins(command):
    """A user command that lands while the auto-direct probe runs must win."""

    async def scenario(command: str):
        _, target = await start_echo_server()
        config = make_config(
            {"svc": {"type": "tcp", "local_port": free_port(), "routes": [{"direct": f"127.0.0.1:{target}"}]}},
            auto_direct_interval=0.05,
        )
        runtime = ServiceRuntime(config.services["svc"], config.tunnel)
        probing, release = asyncio.Event(), asyncio.Event()

        async def controlled_probe() -> bool:
            probing.set()
            await release.wait()
            return True  # the direct route is reachable

        runtime._direct_reachable = controlled_probe
        runtime.activate()
        await asyncio.wait_for(probing.wait(), 5)

        if command == "stop":
            await runtime.stop()
        else:
            runtime.start()
        release.set()
        await asyncio.sleep(0.3)

        if command == "stop":
            # Stop means "back to auto", so auto-direct connecting is correct;
            # what matters is that nothing breaks and no ssh is involved.
            assert runtime.mode in (Mode.OFF, Mode.AUTO) and runtime._ssh is None
        else:
            # The user's Start wins: the probe must not launch on top of it
            # (it used to, downgrading the start to AUTO, without ssh fallback).
            # The final mode is AUTO either way now, since a direct connection
            # settles in auto, so check that the probe did not launch.
            assert not any("connecting automatically" in m for _, m in runtime.logs)
        await runtime.close()

    run(scenario(command))
