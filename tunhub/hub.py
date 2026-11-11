"""The registry of service runtimes, plus config reload."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Awaitable, Callable

from .config import (
    SHARED_FILE,
    Config,
    ConfigError,
    HubSettings,
    Service,
    TunnelSettings,
    empty_config,
    load_config,
    load_hub_settings,
    parse_config,
)
from .icons import IconStore
from .known_macs import FILE_NAME as KNOWN_MACS_FILE
from .known_macs import KnownMacs, MacGuard
from .nexthop import NetworkFingerprint, network_fingerprint, next_hop_mac
from .service import Mode, ServiceRuntime
from .state import StateStore

logger = logging.getLogger("tunhub")

# How long the host name check waits for one name to resolve.
RESOLVE_TIMEOUT = 2.0


async def resolve_host(name: str) -> list[str]:
    """The addresses a host name resolves to (as the browser would see them)."""
    infos = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _needs_resolving(name: str) -> bool:
    """`*.localhost` always means this machine in browsers; IP addresses need no lookup."""
    if name == "localhost" or name.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return True
    return False


class Hub:
    def __init__(
        self,
        config_dir: Path,
        cache_dir: Path | None = None,
        *,
        mac_lookup: Callable[[str], str | None] = next_hop_mac,
        fingerprint: Callable[[], NetworkFingerprint | None] = network_fingerprint,
        resolve: Callable[[str], Awaitable[list[str]]] = resolve_host,
        machine: str | None = None,
    ) -> None:
        self.config_dir = config_dir
        # Picks local.<machine>.yaml; None: $TUNHUB_MACHINE or the host name.
        self.machine = machine
        self.icons = IconStore(config_dir, cache_dir)
        self.guard = MacGuard(KnownMacs(config_dir / KNOWN_MACS_FILE), mac_lookup)
        self._fingerprint = fingerprint
        self._resolve = resolve
        # Host names that do not lead to this machine; found in the background
        # after each (re)load, for the config they were computed from.
        self._host_warnings: tuple[Config, list[str]] | None = None
        self._network_watcher: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        # Reload and shutdown replace runtimes; running two at once would leave
        # an orphaned runtime holding a port.
        self._lifecycle_lock = asyncio.Lock()
        self.state = StateStore(config_dir)
        # Why the config files cannot be used right now (shown on the dashboard),
        # and whether some earlier version of them is in use.
        self.config_error: str | None = None
        self.config_loaded = False
        try:
            self.config: Config = load_config(config_dir, self.machine)
        except ConfigError as exc:
            # Start anyway, so that the dashboard can show the error.
            self.config = self._config_without_services(exc)
            self.config_error = str(exc)
            logger.error("config: %s", exc)
        else:
            self._config_loaded()
        # The listening address is fixed for the process lifetime.
        self.hub_settings: HubSettings = self.config.hub
        self.runtimes: dict[str, ServiceRuntime] = {
            service_id: self._make_runtime(service, self.config.tunnel)
            for service_id, service in self.config.services.items()
        }
        _log_warnings(self.config.warnings)

    def _config_without_services(self, error: ConfigError) -> Config:
        """Listen where the files say, or where the last working config did.

        Re-raises `error` when neither is known: then there is nowhere to listen.
        """
        try:
            hub = load_hub_settings(self.config_dir, self.machine)
        except ConfigError:
            remembered = self.state.get("hub")
            try:
                hub = parse_config({"hub": remembered}).hub if isinstance(remembered, dict) else None
            except ConfigError:
                hub = None
            if hub is None:
                raise error from None
            logger.warning("config: listening as the last working config did (%s:%s)", hub.listen_host, hub.port)
        return empty_config(self.config_dir, hub, self.machine)

    def _config_loaded(self) -> None:
        self.config_error = None
        self.config_loaded = True
        # Remembered so that a later start with broken files still listens here.
        settings = self.config.hub
        hub = {
            "listen": settings.listen_host,
            "port": settings.port,
            "hostname": settings.hostname,
            "open_timeout": settings.open_timeout,
        }
        if self.state.get("hub") != hub:
            self.state.set("hub", hub)

    @property
    def warnings(self) -> list[str]:
        """Config warnings for the dashboard; recomputed on every (re)load."""
        found = self._host_warnings
        host_warnings = found[1] if found is not None and found[0] is self.config else []
        return [*self.config.warnings, *host_warnings]

    def get(self, service_id: str) -> ServiceRuntime | None:
        return self.runtimes.get(service_id)

    def _make_runtime(self, service: Service, settings: TunnelSettings) -> ServiceRuntime:
        runtime = ServiceRuntime(service, settings)
        runtime.on_up = self._on_service_up
        runtime.guard = self.guard
        return runtime

    def _on_service_up(self, runtime: ServiceRuntime) -> None:
        self._spawn(self.icons.ensure(runtime.service, runtime.log))

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        # Keep a reference so the task is not garbage-collected mid-flight.
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def check_hostnames(self, config: Config) -> list[str]:
        """Warn about host names that do not resolve to this machine.

        The browser must reach tunhub under each service's host name; a name
        outside `.localhost` works only with an entry in the hosts file (or DNS).
        """
        users: dict[str, list[str]] = {}
        if _needs_resolving(config.hub.hostname):
            users.setdefault(config.hub.hostname, []).append("the dashboard")
        for service in config.services.values():
            if service.is_web and _needs_resolving(service.hostname):
                users.setdefault(service.hostname, []).append(f"service '{service.id}'")

        async def check(name: str) -> str | None:
            who = ", ".join(users[name])
            try:
                addresses = await asyncio.wait_for(self._resolve(name), RESOLVE_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                addresses = []
            if not addresses:
                return f"host name {name} ({who}) does not resolve; add '127.0.0.1 {name}' to the hosts file"
            if not all(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in addresses):
                return (
                    f"host name {name} ({who}) resolves to {', '.join(addresses)}, not to this machine; "
                    "the browser would bypass tunhub"
                )
            return None

        results = await asyncio.gather(*(check(name) for name in users))
        warnings = [warning for warning in results if warning]
        if self.config is config:
            self._host_warnings = (config, warnings)
            _log_warnings(warnings)
        return warnings

    async def start(self) -> None:
        self._spawn(self.check_hostnames(self.config))
        for runtime in self.runtimes.values():
            runtime.activate()
            if runtime.service.autostart:
                runtime.log("autostart")
                runtime.start()
        self._network_watcher = asyncio.create_task(self._watch_network(), name="network-watcher")

    async def _watch_network(self) -> None:
        """Re-verify running direct routes whenever the network may have changed.

        A running direct route can only reach a different machine if the
        network changed under it. A fingerprint that cannot be determined is
        UNKNOWN, not "unchanged": then routes are re-verified on every tick.
        """
        last: NetworkFingerprint | None = None
        first = True
        while True:
            try:
                current = await asyncio.to_thread(self._fingerprint)
            except Exception:  # treated as UNKNOWN, which re-verifies
                logger.exception("network fingerprint failed")
                current = None
            if current is None:
                if first or last is not None:
                    logger.info("network: unknown (no gateway found); re-verifying direct routes")
                changed = True
            else:
                changed = not first and current != last
                if changed:
                    logger.info("network: changed to %s; re-verifying direct routes", current)
            if changed:
                await asyncio.gather(*(self._reverify(runtime) for runtime in list(self.runtimes.values())))
            last, first = current, False
            await asyncio.sleep(self.config.tunnel.auto_direct_interval)

    async def disconnect_ssh(self) -> list[str]:
        """Stop every service that uses (or is trying) ssh; direct ones stay up."""
        targets = [runtime for runtime in self.runtimes.values() if runtime.uses_ssh]
        await asyncio.gather(*(runtime.disconnect_ssh() for runtime in targets))
        return [runtime.service.id for runtime in targets]

    @staticmethod
    async def _reverify(runtime: ServiceRuntime) -> None:
        # One failing service must not stop the watcher for all the others.
        try:
            await runtime.reverify_direct()
        except Exception as exc:
            logger.exception("[%s] re-verifying the direct route failed", runtime.service.id)
            runtime.log(f"re-verifying the direct route failed: {exc}")

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._network_watcher is not None:
                self._network_watcher.cancel()
            for task in self._background:
                task.cancel()
            await asyncio.gather(*(runtime.close() for runtime in self.runtimes.values()))

    async def reload(self) -> list[str]:
        """Reload config files; unchanged services keep their tunnels running.

        Raises ConfigError (and keeps the old config) if the new files are invalid.
        """
        async with self._lifecycle_lock:
            try:
                messages = await self._reload()
            except ConfigError as exc:
                self.config_error = str(exc)
                raise
            self._config_loaded()
            return messages

    async def _reload(self) -> list[str]:
        new_config = load_config(self.config_dir, self.machine)
        # A missing services.yaml means "empty" only for a first start. Once a
        # file was loaded, its absence is more likely a move, a rename or an
        # editor's save in progress than a wish to drop every service.
        if self.config.found and not new_config.found:
            raise ConfigError(f"{self.config_dir / SHARED_FILE} not found")
        # load_config() checks ports against the *new* hub port, but the
        # dashboard keeps listening on the old one until tunhub restarts.
        running_port = self.hub_settings.port
        for service in new_config.services.values():
            if service.local_port == running_port:
                raise ConfigError(
                    f"service '{service.id}': local_port {running_port} is used by the running "
                    "dashboard (hub changes apply only after a restart)"
                )
        messages: list[str] = []
        if new_config.hub != self.hub_settings:
            messages.append("hub settings changed; restart tunhub to apply them")

        old_runtimes = self.runtimes
        new_runtimes: dict[str, ServiceRuntime] = {}
        to_stop: list[ServiceRuntime] = []
        to_start: list[ServiceRuntime] = []  # "on" services carried over

        for service_id, service in new_config.services.items():
            old = old_runtimes.get(service_id)
            if old is not None and old.service == service and old.settings == new_config.tunnel:
                new_runtimes[service_id] = old
                continue

            runtime = self._make_runtime(service, new_config.tunnel)
            new_runtimes[service_id] = runtime
            if old is None:
                messages.append(f"added {service_id}")
                if service.autostart:
                    to_start.append(runtime)
            else:
                messages.append(f"changed {service_id}")
                to_stop.append(old)
                runtime.logs.extend(old.logs)
                if old.pinned_route is not None and old.pinned_route < len(service.routes):
                    runtime.pinned_route = old.pinned_route
                # An auto-direct service is picked up again by its new probe.
                if old.mode is Mode.ON:
                    to_start.append(runtime)

        for service_id, old in old_runtimes.items():
            if service_id not in new_config.services:
                messages.append(f"removed {service_id}")
                to_stop.append(old)

        # Stop first so that the fixed local ports are free again.
        await asyncio.gather(*(runtime.close() for runtime in to_stop))
        self.config = new_config
        self.runtimes = new_runtimes
        _log_warnings(new_config.warnings)
        self._spawn(self.check_hostnames(new_config))
        for runtime in new_runtimes.values():
            runtime.activate()  # no-op for runtimes that kept running
        for runtime in to_start:
            runtime.start()

        if not messages:
            messages.append("no changes")
        for message in messages:
            logger.info("reload: %s", message)
        return messages


def _log_warnings(warnings: tuple[str, ...] | list[str]) -> None:
    for warning in warnings:
        logger.warning("config: %s", warning)


__all__ = ["Hub", "ConfigError"]
