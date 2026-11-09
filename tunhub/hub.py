"""The registry of service runtimes, plus config reload."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import Config, ConfigError, HubSettings, Service, TunnelSettings, load_config
from .icons import IconStore
from .service import Mode, ServiceRuntime

logger = logging.getLogger("tunhub")


class Hub:
    def __init__(self, config_dir: Path, cache_dir: Path | None = None) -> None:
        self.config_dir = config_dir
        self.icons = IconStore(config_dir, cache_dir)
        self._background: set[asyncio.Task] = set()
        # Reload and shutdown replace runtimes; running two at once would leave
        # an orphaned runtime holding a port.
        self._lifecycle_lock = asyncio.Lock()
        self.config: Config = load_config(config_dir)
        # The listening address is fixed for the process lifetime.
        self.hub_settings: HubSettings = self.config.hub
        self.runtimes: dict[str, ServiceRuntime] = {
            service_id: self._make_runtime(service, self.config.tunnel)
            for service_id, service in self.config.services.items()
        }

    def get(self, service_id: str) -> ServiceRuntime | None:
        return self.runtimes.get(service_id)

    def _make_runtime(self, service: Service, settings: TunnelSettings) -> ServiceRuntime:
        runtime = ServiceRuntime(service, settings)
        runtime.on_up = self._on_service_up
        return runtime

    def _on_service_up(self, runtime: ServiceRuntime) -> None:
        # Keep a reference so the task is not garbage-collected mid-flight.
        task = asyncio.create_task(self.icons.ensure(runtime.service, runtime.log))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def start(self) -> None:
        for runtime in self.runtimes.values():
            runtime.activate()
            if runtime.service.autostart:
                runtime.log("autostart")
                runtime.start()

    async def disconnect_ssh(self) -> list[str]:
        """Stop every service that uses (or is trying) ssh; direct ones stay up."""
        targets = [runtime for runtime in self.runtimes.values() if runtime.uses_ssh]
        await asyncio.gather(*(runtime.disconnect_ssh() for runtime in targets))
        return [runtime.service.id for runtime in targets]

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            for task in self._background:
                task.cancel()
            await asyncio.gather(*(runtime.close() for runtime in self.runtimes.values()))

    async def reload(self) -> list[str]:
        """Reload config files; unchanged services keep their tunnels running.

        Raises ConfigError (and keeps the old config) if the new files are invalid.
        """
        async with self._lifecycle_lock:
            return await self._reload()

    async def _reload(self) -> list[str]:
        new_config = load_config(self.config_dir)
        messages: list[str] = []
        if new_config.hub != self.hub_settings:
            messages.append("hub settings changed; restart tunhub to apply them")

        old_runtimes = self.runtimes
        new_runtimes: dict[str, ServiceRuntime] = {}
        to_stop: list[ServiceRuntime] = []
        to_start: list[ServiceRuntime] = []  # manual starts carried over

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
                runtime.manually_stopped = old.manually_stopped
                if old.pinned_route is not None and old.pinned_route < len(service.routes):
                    runtime.pinned_route = old.pinned_route
                # An auto-direct service is picked up again by its new probe.
                if old.mode is Mode.MANUAL:
                    to_start.append(runtime)

        for service_id, old in old_runtimes.items():
            if service_id not in new_config.services:
                messages.append(f"removed {service_id}")
                to_stop.append(old)

        # Stop first so that the fixed local ports are free again.
        await asyncio.gather(*(runtime.close() for runtime in to_stop))
        self.config = new_config
        self.runtimes = new_runtimes
        for runtime in new_runtimes.values():
            runtime.activate()  # no-op for runtimes that kept running
        for runtime in to_start:
            runtime.start()

        if not messages:
            messages.append("no changes")
        for message in messages:
            logger.info("reload: %s", message)
        return messages


__all__ = ["Hub", "ConfigError"]
