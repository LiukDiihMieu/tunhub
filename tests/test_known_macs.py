"""known_macs: TOFU check that a direct target is still the same machine."""

import asyncio
import sys

import pytest
import yaml

from tunhub.cli import main
from tunhub.hub import Hub
from tunhub.known_macs import HEADER, KnownMacs, MacGuard, parse
from tunhub.nexthop import NetworkFingerprint
from tunhub.service import Mode, ServiceRuntime, State

from .helpers import FAKE_SSH, free_port, make_config, start_echo_server, wait_until

NAS_MAC = "aa:bb:cc:dd:ee:01"
OTHER_MAC = "aa:bb:cc:dd:ee:99"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


# ------------------------------------------------------------------ the file


def test_parse_skips_comments_and_malformed_lines():
    text = (
        "# comment\n"
        "192.168.1.10 AA:BB:CC:DD:EE:01   # trailing comment\n"
        "192.168.1.10 aa-bb-cc-dd-ee-02\n"
        "not-an-ip aa:bb:cc:dd:ee:03\n"
        "192.168.1.11 not-a-mac\n"
        "192.168.1.12\n"
    )
    entries = parse(text)
    assert [(e.mac, e.line) for e in entries["192.168.1.10"]] == [
        ("aa:bb:cc:dd:ee:01", 2),
        ("aa:bb:cc:dd:ee:02", 3),
    ]
    assert set(entries) == {"192.168.1.10"}


def test_add_creates_file_with_header_and_appends(tmp_path):
    known = KnownMacs(tmp_path / "known_macs")
    known.add("192.168.1.10", NAS_MAC)
    known.add("192.168.1.11", OTHER_MAC)
    text = (tmp_path / "known_macs").read_text()
    assert text.startswith(HEADER)
    assert text.endswith(f"192.168.1.10 {NAS_MAC}\n192.168.1.11 {OTHER_MAC}\n")
    assert known.lookup("192.168.1.10")[0].mac == NAS_MAC


def test_manual_edits_are_kept_and_picked_up(tmp_path):
    path = tmp_path / "known_macs"
    path.write_text(f"# my NAS\n192.168.1.10 {NAS_MAC}")  # no trailing newline
    known = KnownMacs(path)
    assert known.lookup("192.168.1.10")[0].line == 2

    known.add("192.168.1.11", OTHER_MAC)
    assert path.read_text() == f"# my NAS\n192.168.1.10 {NAS_MAC}\n192.168.1.11 {OTHER_MAC}\n"

    path.write_text(f"192.168.1.10 {OTHER_MAC}\n")  # edited by hand
    assert [e.mac for e in known.lookup("192.168.1.10")] == [OTHER_MAC]


def test_forget_removes_only_that_ip(tmp_path):
    path = tmp_path / "known_macs"
    path.write_text(f"# keep me\n192.168.1.10 {NAS_MAC}\n192.168.1.11 {OTHER_MAC}\n192.168.1.10 {OTHER_MAC}\n")
    known = KnownMacs(path)
    assert known.forget("192.168.1.10") == 2
    assert path.read_text() == f"# keep me\n192.168.1.11 {OTHER_MAC}\n"
    assert known.lookup("192.168.1.10") == []
    assert known.forget("192.168.1.10") == 0


def test_cli_forget(tmp_path, capsys):
    (tmp_path / "known_macs").write_text(f"192.168.1.10 {NAS_MAC}\n")
    assert main(["forget", "192.168.1.10", "--config-dir", str(tmp_path)]) == 0
    assert "removed 1 line" in capsys.readouterr().out
    assert (tmp_path / "known_macs").read_text() == ""


# ----------------------------------------------------------------- the guard


def make_guard(tmp_path, macs: dict):
    return MacGuard(KnownMacs(tmp_path / "known_macs"), lookup=macs.get, skip_loopback=False)


def test_guard_verdicts(tmp_path):
    async def scenario():
        macs = {"192.168.1.10": NAS_MAC}
        guard = make_guard(tmp_path, macs)

        first = await guard.check("192.168.1.10")
        assert first.allowed and "Permanently added" in first.note  # learned on first use
        assert await guard.check("192.168.1.10") == type(first)(True)  # same machine: silent

        macs["192.168.1.10"] = OTHER_MAC
        changed = await guard.check("192.168.1.10")
        assert not changed.allowed and "tunhub forget 192.168.1.10" in changed.note

        del macs["192.168.1.10"]
        unknown_known = await guard.check("192.168.1.10")
        assert not unknown_known.allowed  # recorded, but cannot be verified now

        never_seen = await guard.check("10.8.0.5")  # e.g. through a VPN
        assert never_seen.allowed and "cannot verify" in never_seen.note

        loopback = await MacGuard(guard.known, macs.get).check("127.0.0.1")
        assert loopback.allowed and loopback.note is None

    run(scenario())


# --------------------------------------------------------------- the runtime


async def runtime_with_guard(tmp_path, macs, *, ssh_fallback=True, auto=False):
    """A service whose direct route goes to a local echo server (seen as a LAN host)."""
    _, target = await start_echo_server()
    routes = [{"direct": f"127.0.0.1:{target}"}]
    if ssh_fallback:
        routes.append({"ssh": "nas", "remote": target})
    config = make_config(
        {"svc": {"type": "tcp", "local_port": free_port(), "routes": routes, "auto_direct": auto}},
        auto_direct_interval=0.05,
    )
    runtime = ServiceRuntime(config.services["svc"], config.tunnel)
    runtime.guard = make_guard(tmp_path, macs)
    return runtime


def test_first_use_learns_then_same_machine_connects_directly(tmp_path):
    async def scenario():
        runtime = await runtime_with_guard(tmp_path, {"127.0.0.1": NAS_MAC})
        runtime.start()
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime.active_route == 0
        assert f"127.0.0.1 {NAS_MAC}" in (tmp_path / "known_macs").read_text()
        await runtime.stop()

    run(scenario())


def test_different_machine_falls_back_to_ssh_with_a_warning(tmp_path):
    async def scenario():
        (tmp_path / "known_macs").write_text(f"127.0.0.1 {NAS_MAC}\n")
        runtime = await runtime_with_guard(tmp_path, {"127.0.0.1": OTHER_MAC})
        runtime.start()
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime.active_route == 1  # ssh, not the stranger
        assert "tunhub forget 127.0.0.1" in runtime.identity_warning
        assert runtime.snapshot()["identity_warning"] == runtime.identity_warning
        await runtime.stop()

    run(scenario())


def test_auto_direct_does_not_start_on_a_different_machine(tmp_path):
    async def scenario():
        (tmp_path / "known_macs").write_text(f"127.0.0.1 {NAS_MAC}\n")
        runtime = await runtime_with_guard(tmp_path, {"127.0.0.1": OTHER_MAC}, ssh_fallback=False, auto=True)
        runtime.activate()
        await wait_until(lambda: runtime.identity_warning is not None, runtime=runtime)
        await asyncio.sleep(0.3)  # several probe rounds
        assert runtime.mode is Mode.OFF and runtime.state is State.OFF
        # Not even attempted (the bring-up check would also stop it, later).
        assert not any("connecting automatically" in m for _, m in runtime.logs)
        mismatches = [m for _, m in runtime.logs if "expects" in m]
        assert len(mismatches) == 1  # logged once, not on every probe
        await runtime.close()

    run(scenario())


def test_reverify_keeps_same_machine_and_drops_a_different_one(tmp_path):
    async def scenario():
        macs = {"127.0.0.1": NAS_MAC}
        runtime = await runtime_with_guard(tmp_path, macs)
        runtime.start()
        await wait_until(lambda: runtime.state is State.UP and runtime.active_route == 0, runtime=runtime)
        relay = runtime._relay

        await runtime.reverify_direct()  # network changed, same NAS
        await asyncio.sleep(0.3)
        assert runtime._relay is relay and runtime.active_route == 0  # not interrupted

        macs["127.0.0.1"] = OTHER_MAC  # now another machine answers
        await runtime.reverify_direct()
        await wait_until(lambda: runtime.state is State.UP and runtime.active_route == 1, runtime=runtime)
        await runtime.stop()

    run(scenario())


def test_reverify_drops_route_whose_target_is_gone(tmp_path):
    async def scenario():
        server, target = await start_echo_server()
        config = make_config({"svc": {"type": "tcp", "local_port": free_port(), "routes": [{"direct": f"127.0.0.1:{target}"}]}})
        runtime = ServiceRuntime(config.services["svc"], config.tunnel)
        runtime.start()
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        server.close()
        await server.wait_closed()
        await runtime.reverify_direct()
        await wait_until(lambda: any("unreachable after a network change" in m for _, m in runtime.logs), runtime=runtime)
        await runtime.stop()

    run(scenario())


# ---------------------------------------------------------- the hub watcher


@pytest.mark.parametrize(
    "fingerprints, reverified_on_ticks",
    [
        (["A", "A", "A"], []),          # first tick only records
        (["A", "A", "B", "B"], [2]),    # a change
        (["A", None, None, "A"], [1, 2, 3]),  # UNKNOWN is not "unchanged"; back to A after unknown counts as a change
        ([None, None], [0, 1]),         # unknown from the start
    ],
)
def test_network_watcher(tmp_path, fingerprints, reverified_on_ticks):
    prints = {
        "A": NetworkFingerprint("192.168.1.1", "aa:bb:cc:dd:ee:a1", "192.168.1.0/24"),
        "B": NetworkFingerprint("10.0.0.1", "aa:bb:cc:dd:ee:b1", "10.0.0.0/24"),
        None: None,
    }

    async def scenario():
        (tmp_path / "services.yaml").write_text(yaml.safe_dump({
            "hub": {"port": free_port()},
            "tunnel": {"ssh_command": [sys.executable, FAKE_SSH], "auto_direct_interval": 0.05},
            "services": {"svc": {"type": "tcp", "local_port": free_port(), "routes": [{"ssh": "nas", "remote": 22}]}},
        }))
        sequence = iter([prints[key] for key in fingerprints])
        tick = {"n": -1}

        def fingerprint():
            tick["n"] += 1
            return next(sequence, prints[fingerprints[-1]])

        hub = Hub(tmp_path, tmp_path / "cache", fingerprint=fingerprint)
        calls = []

        async def reverify():
            calls.append(tick["n"])

        hub.get("svc").reverify_direct = reverify
        await hub.start()
        await wait_until(lambda: tick["n"] >= len(fingerprints) - 1)
        await asyncio.sleep(0.02)
        await hub.shutdown()
        assert [n for n in calls if n < len(fingerprints)] == reverified_on_ticks

    run(scenario())


# ------------------------------------------------- file errors (from review)


def test_unreadable_file_skips_direct(tmp_path):
    async def scenario():
        (tmp_path / "known_macs").mkdir()  # reading it fails on every OS
        verdict = await make_guard(tmp_path, {"192.168.1.10": NAS_MAC}).check("192.168.1.10")
        assert not verdict.allowed and "cannot read" in verdict.note

    run(scenario())


class ReadOnlyKnownMacs(KnownMacs):
    """Like a read-only config directory: reading works, writing fails."""

    def add(self, ip, mac):
        raise PermissionError(13, "Permission denied", str(self.path))


def test_file_that_cannot_be_written_still_allows_first_use(tmp_path):
    async def scenario():
        guard = MacGuard(ReadOnlyKnownMacs(tmp_path / "known_macs"), {"192.168.1.10": NAS_MAC}.get,
                         skip_loopback=False)
        verdict = await guard.check("192.168.1.10")
        assert verdict.allowed and "could not record" in verdict.note

    run(scenario())


def test_unreadable_file_falls_back_to_ssh_instead_of_crashing(tmp_path):
    async def scenario():
        (tmp_path / "known_macs").mkdir()
        runtime = await runtime_with_guard(tmp_path, {"127.0.0.1": NAS_MAC})
        runtime.start()
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime.active_route == 1 and runtime.mode is Mode.MANUAL  # ssh, still supervised
        assert "internal error" not in (runtime.last_error or "")
        await runtime.stop()

    run(scenario())


class ExplodingGuard:
    async def check(self, ip):
        raise RuntimeError("boom")


def test_unexpected_guard_error_does_not_end_auto_direct(tmp_path):
    async def scenario():
        runtime = await runtime_with_guard(tmp_path, {}, ssh_fallback=False, auto=True)
        runtime.guard = ExplodingGuard()
        runtime.activate()
        await wait_until(lambda: runtime.identity_warning is not None, runtime=runtime)
        await asyncio.sleep(0.2)
        assert not runtime._watcher.done()  # still probing
        await runtime.close()

    run(scenario())


def test_network_watcher_survives_a_failing_service(tmp_path):
    async def scenario():
        (tmp_path / "services.yaml").write_text(yaml.safe_dump({
            "hub": {"port": free_port()},
            "tunnel": {"ssh_command": [sys.executable, FAKE_SSH], "auto_direct_interval": 0.05},
            "services": {
                "bad": {"type": "tcp", "local_port": free_port(), "routes": [{"ssh": "a", "remote": 22}]},
                "good": {"type": "tcp", "local_port": free_port(), "routes": [{"ssh": "b", "remote": 22}]},
            },
        }))
        hub = Hub(tmp_path, tmp_path / "cache", fingerprint=lambda: None)  # UNKNOWN: re-verify every tick
        good_calls = []

        async def boom():
            raise RuntimeError("boom")

        async def good():
            good_calls.append(1)

        hub.get("bad").reverify_direct = boom
        hub.get("good").reverify_direct = good
        await hub.start()
        await wait_until(lambda: len(good_calls) >= 3)  # several ticks despite "bad"
        assert not hub._network_watcher.done()
        await hub.shutdown()

    run(scenario())


def test_relay_forwards_to_the_verified_ip_not_the_host_name(tmp_path):
    # A host name could resolve to another machine on a later connection
    # (e.g. 'nas.local' on another network); the relay must stick to the IP
    # that was checked against known_macs.
    async def scenario():
        _, target = await start_echo_server()
        config = make_config({"svc": {"type": "tcp", "local_port": free_port(), "routes": [{"direct": f"localhost:{target}"}]}})
        runtime = ServiceRuntime(config.services["svc"], config.tunnel)
        runtime.guard = make_guard(tmp_path, {"127.0.0.1": NAS_MAC})
        runtime.start()
        await wait_until(lambda: runtime.state is State.UP, runtime=runtime)
        assert runtime._relay.target_host == "127.0.0.1"
        assert f"127.0.0.1 {NAS_MAC}" in (tmp_path / "known_macs").read_text()
        await runtime.stop()

    run(scenario())
