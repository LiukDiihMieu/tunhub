"""Per-service runtime: route selection, tunnel lifecycle and auto-reconnect.

Every service always listens on its fixed 127.0.0.1:<local_port>, whichever
route is active:

* direct route -> an in-process TcpRelay listens and forwards to the target;
* ssh route    -> ``ssh -L 127.0.0.1:<local_port>:...`` listens.

So bookmarks, cookies and saved passwords stay valid when the route changes.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections import deque
from enum import Enum
from typing import Callable

from .config import Route, Service, TunnelSettings
from .net import TcpRelay, ensure_port_free, http_probe, is_port_open, probe_tcp
from .ssh import SshProcess, build_ssh_command

logger = logging.getLogger("tunhub")

LOCAL_HOST = "127.0.0.1"
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 60.0
RECONNECT_DELAY = 1.0
PORT_RELEASE_TIMEOUT = 3.0


class State(str, Enum):
    OFF = "off"
    CONNECTING = "connecting"
    UP = "up"
    RETRYING = "retrying"


class Mode(str, Enum):
    """Why the service is (or is not) running.

    OFF:    not running.
    AUTO:   started by the background probe; direct routes only. When the
            direct route is lost the service goes back to OFF (never ssh).
    MANUAL: started by the user (Start / Open / bookmark / autostart); all
            routes are tried and failures are retried until stopped.
    """

    OFF = "off"
    AUTO = "auto"
    MANUAL = "manual"


class RouteFailed(Exception):
    pass


class ServiceRuntime:
    def __init__(self, service: Service, settings: TunnelSettings) -> None:
        self.service = service
        self.settings = settings

        self.state = State.OFF
        self.state_since = time.time()
        self.active_route: int | None = None
        self.pinned_route: int | None = None  # None = auto (try routes in order)
        self.last_error: str | None = None
        self.logs: deque[tuple[float, str]] = deque(maxlen=300)

        self.mode = Mode.OFF
        # A manual Stop pauses auto-direct until the next manual Start.
        self.manually_stopped = False
        self._task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None
        self._wake = asyncio.Event()  # cut a retry back-off short
        self._down = asyncio.Event()  # tear the active route down
        self._down_reason = ""
        self._attempt_done = asyncio.Condition()  # notified after every attempt
        self._ssh: SshProcess | None = None
        self._relay: TcpRelay | None = None
        # Called every time the service comes up (the hub uses it for icons).
        self.on_up: Callable[[ServiceRuntime], None] | None = None

    # ------------------------------------------------------------------
    # Commands

    @property
    def desired(self) -> bool:
        return self.mode is not Mode.OFF

    def activate(self) -> None:
        """Begin background probing for auto-direct (no-op if not applicable)."""
        if self._watcher is None and self.service.auto_direct and self.service.direct_route_indices:
            self._watcher = asyncio.create_task(self._auto_watch(), name=f"watch:{self.service.id}")

    async def close(self) -> None:
        """Stop everything, including the background probe (shutdown / reload)."""
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except asyncio.CancelledError:
                pass
            self._watcher = None
        await self._halt("hub shutting down")

    def start(self) -> None:
        """Manual start; also upgrades a running auto-direct service to manual."""
        self.manually_stopped = False
        if self.mode is Mode.AUTO:
            self.log("now kept up manually (ssh fallback enabled)")
        self._launch(Mode.MANUAL)

    async def stop(self) -> None:
        """Manual stop; pauses auto-direct until the next manual start."""
        self.manually_stopped = True
        await self._halt("stopped by user")

    @property
    def uses_ssh(self) -> bool:
        """Running, and not settled on a direct route (so ssh is or may be in use)."""
        if not self.desired:
            return False
        route = self.service.routes[self.active_route] if self.active_route is not None else None
        return not (self.state is State.UP and route is not None and route.kind == "direct")

    async def disconnect_ssh(self) -> None:
        """Stop without pausing auto-direct; it may still come back via direct."""
        await self._halt("all ssh connections disconnected by user")

    def set_pinned_route(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self.service.routes):
            raise ValueError(f"route index {index} out of range")
        self.pinned_route = index
        label = "auto" if index is None else self.service.routes[index].describe()
        self.log(f"route preference set to: {label}")
        if self.desired:
            self.log("re-selecting route")
            self._request_down("route preference changed")
            self._wake.set()

    async def ensure_up(self, timeout: float) -> bool:
        """Start the service if needed and wait for the outcome of an attempt."""
        self.start()
        if self.state is State.UP:
            return True
        async with self._attempt_done:
            if self.state is State.UP:
                return True
            try:
                await asyncio.wait_for(self._attempt_done.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            return self.state is State.UP

    # ------------------------------------------------------------------
    # Introspection

    def log(self, message: str) -> None:
        # deque.append is thread-safe; the ssh stderr reader thread calls this.
        self.logs.append((time.time(), message))
        logger.info("[%s] %s", self.service.id, message)

    def snapshot(self) -> dict:
        service = self.service
        return {
            "id": service.id,
            "name": service.name,
            "description": service.description,
            "type": service.type,
            "url": service.url,
            "address": service.address,
            "local_port": service.local_port,
            "state": self.state.value,
            "state_since": self.state_since,
            "desired": self.desired,
            "mode": self.mode.value,
            "auto_direct": self._watcher is not None,
            "auto_paused": self._watcher is not None and self.manually_stopped,
            "uses_ssh": self.uses_ssh,
            "active_route": self.active_route,
            "pinned_route": self.pinned_route,
            "last_error": self.last_error,
            "connections": self._relay.active_connections if self._relay else None,
            "routes": [
                {"index": i, "kind": r.kind, "label": r.describe()}
                for i, r in enumerate(service.routes)
            ],
        }

    # ------------------------------------------------------------------
    # Supervisor loop

    def _launch(self, mode: Mode) -> None:
        self.mode = mode
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise(), name=f"service:{self.service.id}")
        else:
            self._wake.set()

    async def _halt(self, reason: str) -> None:
        self.mode = Mode.OFF
        self._request_down(reason)
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _auto_watch(self) -> None:
        """Bring the service up via direct whenever a direct route is reachable."""
        while True:
            if (
                self.mode is Mode.OFF
                and not self.manually_stopped
                and (self._task is None or self._task.done())
                and await self._direct_reachable()
            ):
                self.log("direct route reachable, connecting automatically")
                self._launch(Mode.AUTO)
            await asyncio.sleep(self.settings.auto_direct_interval)

    async def _direct_reachable(self) -> bool:
        for index in self._candidate_routes(Mode.AUTO):
            route = self.service.routes[index]
            try:
                await probe_tcp(route.target_host, route.target_port, self.settings.probe_timeout)
                return True
            except (OSError, asyncio.TimeoutError):
                continue
        return False

    def _candidate_routes(self, mode: Mode) -> list[int]:
        if self.pinned_route is not None:
            candidates = [self.pinned_route]
        else:
            candidates = list(range(len(self.service.routes)))
        if mode is Mode.AUTO:
            candidates = [i for i in candidates if self.service.routes[i].kind == "direct"]
        return candidates

    async def _supervise(self) -> None:
        backoff = INITIAL_BACKOFF
        try:
            while self.mode is not Mode.OFF:
                self._down.clear()
                self._wake.clear()
                self._down_reason = ""
                await self._set_state(State.CONNECTING)

                attempt_mode = self.mode
                if await self._bring_up(attempt_mode):
                    backoff = INITIAL_BACKOFF
                    await self._set_state(State.UP, attempt_finished=True)
                    if self.on_up is not None:
                        self.on_up(self)
                    reason = await self._wait_down()
                    await self._teardown()
                    self.active_route = None
                    if self.mode is Mode.OFF:
                        break
                    self.log(f"route down: {reason}")
                    if self.mode is Mode.AUTO:
                        self._go_idle("direct route lost; auto mode does not fall back to ssh")
                        break
                    delay = RECONNECT_DELAY
                elif self.mode is Mode.OFF:
                    break
                elif attempt_mode is Mode.AUTO:
                    if self.mode is Mode.AUTO:
                        self._go_idle("auto-connect failed")
                        break
                    # Upgraded to manual during the attempt: try all routes now.
                    continue
                else:
                    await self._set_state(State.RETRYING, attempt_finished=True)
                    delay = backoff
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    self.log(f"all routes failed, retrying in {delay:.0f}s")

                await self._sleep_or_wake(delay)
        except Exception as exc:  # never leave a half-open tunnel behind
            logger.exception("[%s] supervisor crashed", self.service.id)
            self.last_error = f"internal error: {exc}"
            self.mode = Mode.OFF
        except BaseException:
            # Cancelled (e.g. the event loop is shutting down): do not relaunch.
            self.mode = Mode.OFF
            raise
        finally:
            await self._teardown()
            self.active_route = None
            await self._set_state(State.OFF, attempt_finished=True)
            # start() may have been called while we were shutting down.
            if self.mode is not Mode.OFF:
                self._task = asyncio.create_task(self._supervise(), name=f"service:{self.service.id}")

    def _go_idle(self, reason: str) -> None:
        self.log(f"{reason}; idle until a direct route is reachable again")
        self.mode = Mode.OFF
        self.last_error = None  # background activity; keep the card clean

    async def _set_state(self, state: State, attempt_finished: bool = False) -> None:
        async with self._attempt_done:
            if state is not self.state:
                self.state = state
                self.state_since = time.time()
            if attempt_finished:
                self._attempt_done.notify_all()

    async def _sleep_or_wake(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
        except asyncio.TimeoutError:
            pass

    def _request_down(self, reason: str) -> None:
        if not self._down.is_set():
            self._down_reason = reason
            self._down.set()

    async def _wait_down(self) -> str:
        while True:
            if self._ssh is not None and not self._ssh.alive():
                detail = f": {self._ssh.last_line}" if self._ssh.last_line else ""
                return f"ssh exited with code {self._ssh.returncode}{detail}"
            try:
                await asyncio.wait_for(self._down.wait(), 1.0)
                return self._down_reason
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Route handling

    async def _bring_up(self, mode: Mode) -> bool:
        routes = self.service.routes
        errors = []
        for index in self._candidate_routes(mode):
            route = routes[index]
            self.log(f"trying route #{index + 1}: {route.describe()}")
            try:
                await self._open_route(route)
                if self.service.is_web and self.service.health_check:
                    await self._health_check()
            except RouteFailed as exc:
                self.log(f"route #{index + 1} failed: {exc}")
                errors.append(f"#{index + 1} {route.describe()}: {exc}")
                await self._teardown()
                if self.mode is Mode.OFF:
                    break
                continue

            self.active_route = index
            self.last_error = None
            self.log(f"up via route #{index + 1} on {self.service.address}")
            return True

        self.last_error = " | ".join(errors) or "no route tried"
        return False

    async def _open_route(self, route: Route) -> None:
        if route.kind == "direct":
            await self._open_direct(route)
        else:
            await self._open_ssh(route)

    async def _open_direct(self, route: Route) -> None:
        try:
            await probe_tcp(route.target_host, route.target_port, self.settings.probe_timeout)
        except asyncio.TimeoutError:
            raise RouteFailed(f"{route.target} timed out") from None
        except OSError as exc:
            raise RouteFailed(f"{route.target} unreachable ({exc.strerror or exc})") from None

        relay = TcpRelay(
            LOCAL_HOST,
            self.service.local_port,
            route.target_host,
            route.target_port,
            connect_timeout=max(3.0, self.settings.probe_timeout * 3),
            on_connect_error=self._on_relay_error,
        )
        try:
            await relay.start()
        except OSError as exc:
            raise RouteFailed(self._port_busy_message(exc)) from None
        self._relay = relay

    async def _open_ssh(self, route: Route) -> None:
        port = self.service.local_port
        try:
            ensure_port_free(LOCAL_HOST, port)
        except OSError as exc:
            raise RouteFailed(self._port_busy_message(exc)) from None

        argv = build_ssh_command(self.settings, route, port)
        self.log("$ " + shlex.join(argv))
        process = SshProcess(argv, on_line=lambda line: self.log(f"ssh: {line}"))
        try:
            process.start()
        except OSError as exc:
            raise RouteFailed(f"cannot run {argv[0]!r}: {exc}") from None
        self._ssh = process

        deadline = time.monotonic() + self.settings.ready_timeout
        while True:
            if not process.alive():
                detail = process.last_line or "no output"
                raise RouteFailed(f"ssh exited with code {process.returncode}: {detail}")
            if await is_port_open(LOCAL_HOST, port, 0.5):
                return
            if time.monotonic() > deadline:
                raise RouteFailed(f"ssh did not open the forward within {self.settings.ready_timeout:.0f}s")
            await asyncio.sleep(0.2)

    async def _health_check(self) -> None:
        service = self.service
        deadline = time.monotonic() + self.settings.health_timeout
        last_error: Exception | None = None
        while True:
            try:
                status = await http_probe(
                    service.local_port,
                    host_header=service.address,
                    path=service.path,
                    use_tls=service.type == "https",
                    timeout=max(1.0, deadline - time.monotonic()),
                )
                self.log(f"health check: HTTP {status}")
                return
            except (OSError, asyncio.TimeoutError, ValueError) as exc:
                last_error = exc
            if self._ssh is not None and not self._ssh.alive():
                raise RouteFailed(f"ssh exited during health check: {self._ssh.last_line}")
            if time.monotonic() > deadline:
                detail = str(last_error) or type(last_error).__name__
                raise RouteFailed(f"tunnel is open but the web service does not answer ({detail})")
            await asyncio.sleep(0.5)

    def _on_relay_error(self, exc: Exception) -> None:
        detail = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        self.log(f"relay could not reach target: {detail}")
        # Only a live route is torn down; during health checks errors are expected.
        if self.state is State.UP:
            self._request_down("direct target became unreachable")

    async def _teardown(self) -> None:
        if self._relay is not None:
            await self._relay.stop()
            self._relay = None
        if self._ssh is not None:
            await asyncio.to_thread(self._ssh.stop)
            self._ssh = None
            await self._wait_port_released()

    async def _wait_port_released(self, timeout: float = PORT_RELEASE_TIMEOUT) -> None:
        """Wait until the local port can be bound again.

        On Windows a terminated process may hold its listening socket for a few
        hundred milliseconds; trying the next route meanwhile would fail with
        "address in use" and wrongly fall back to a lower-priority route.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                ensure_port_free(LOCAL_HOST, self.service.local_port)
                return
            except OSError:
                if time.monotonic() > deadline:
                    return  # the next bind reports the problem
                await asyncio.sleep(0.1)

    def _port_busy_message(self, exc: OSError) -> str:
        return (
            f"cannot listen on {LOCAL_HOST}:{self.service.local_port} "
            f"({exc.strerror or exc}); is another program or a leftover ssh using it?"
        )
