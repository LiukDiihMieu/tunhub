"""Configuration loading.

The shared ``services.yaml`` (synced via git) defines every service and the
ordered list of routes to try. An optional, untracked ``local.yaml`` on each
machine is deep-merged on top of it to override or disable services locally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SHARED_FILE = "services.yaml"
LOCAL_FILE = "local.yaml"

SERVICE_TYPES = ("http", "https", "tcp")
SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

DEFAULT_SSH_OPTIONS = {
    "ConnectTimeout": "10",
    "ServerAliveInterval": "15",
    "ServerAliveCountMax": "3",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    """One way to reach a service.

    kind == "direct": connect straight to target_host:target_port.
    kind == "ssh":    ``ssh -L`` through ssh_host; target is seen from the SSH server.
    """

    kind: str
    target_host: str
    target_port: int
    ssh_host: str | None = None
    label: str = ""

    @property
    def target(self) -> str:
        return format_endpoint(self.target_host, self.target_port)

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.kind == "direct":
            return f"direct {self.target}"
        return f"ssh {self.ssh_host} → {self.target}"


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    type: str
    local_port: int
    hostname: str
    path: str
    autostart: bool
    health_check: bool
    routes: tuple[Route, ...]
    description: str = ""
    auto_direct: bool = True
    icon: str | None = None  # image file, relative to the config directory

    @property
    def direct_route_indices(self) -> list[int]:
        return [i for i, route in enumerate(self.routes) if route.kind == "direct"]

    @property
    def is_web(self) -> bool:
        return self.type in ("http", "https")

    @property
    def address(self) -> str:
        """What the user types into a browser / RDP client (without scheme)."""
        host = self.hostname if self.is_web else "127.0.0.1"
        return f"{host}:{self.local_port}"

    @property
    def url(self) -> str | None:
        if not self.is_web:
            return None
        return f"{self.type}://{self.address}{self.path}"


@dataclass(frozen=True)
class TunnelSettings:
    """Settings that affect how tunnels are built; a change restarts tunnels."""

    ssh_command: tuple[str, ...]
    ssh_options: tuple[tuple[str, str], ...]
    probe_timeout: float
    ready_timeout: float
    health_timeout: float
    auto_direct_interval: float


@dataclass(frozen=True)
class HubSettings:
    listen_host: str
    port: int
    hostname: str
    open_timeout: float

    @property
    def allowed_hosts(self) -> frozenset[str]:
        names = {self.hostname, "localhost", "127.0.0.1"}
        return frozenset(f"{name}:{self.port}" for name in names)


@dataclass(frozen=True)
class Config:
    hub: HubSettings
    tunnel: TunnelSettings
    services: dict[str, Service]


# ---------------------------------------------------------------------------
# Public API


def load_config(config_dir: Path) -> Config:
    shared_path = config_dir / SHARED_FILE
    if not shared_path.exists():
        raise ConfigError(f"{shared_path} not found")
    raw = _read_yaml(shared_path)

    local_path = config_dir / LOCAL_FILE
    if local_path.exists():
        raw = deep_merge(raw, _read_yaml(local_path))

    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    hub = _parse_hub(raw.get("hub") or {})
    tunnel_raw = raw.get("tunnel") or {}
    tunnel = _parse_tunnel(tunnel_raw)
    auto_direct_default = bool(tunnel_raw.get("auto_direct", True))

    services: dict[str, Service] = {}
    for service_id, body in (raw.get("services") or {}).items():
        service_id = str(service_id)
        body = body or {}
        if not isinstance(body, dict):
            raise ConfigError(f"service '{service_id}': expected a mapping")
        if body.get("enabled", True) is False:
            continue
        services[service_id] = _parse_service(service_id, body, auto_direct_default)

    _check_port_conflicts(hub, services)
    return Config(hub=hub, tunnel=tunnel, services=services)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; any non-mapping value (incl. lists) replaces."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def parse_endpoint(value: Any, where: str, default_host: str | None = None) -> tuple[str, int]:
    """Parse ``host:port``, ``[v6]:port`` or a bare port into (host, port)."""
    if isinstance(value, int):
        if default_host is None:
            raise ConfigError(f"{where}: expected 'host:port', got bare port {value}")
        return default_host, _check_port(value, where)

    text = str(value).strip()
    if text.isdigit():
        return parse_endpoint(int(text), where, default_host)

    match = re.fullmatch(r"\[([^\]]+)\]:(\d+)|([^:\s]+):(\d+)", text)
    if not match:
        raise ConfigError(f"{where}: cannot parse endpoint '{text}' (expected host:port)")
    host = match.group(1) or match.group(3)
    port = int(match.group(2) or match.group(4))
    return host, _check_port(port, where)


def format_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# ---------------------------------------------------------------------------
# Section parsers


def _parse_hub(raw: dict[str, Any]) -> HubSettings:
    return HubSettings(
        listen_host=str(raw.get("listen", "127.0.0.1")),
        port=_check_port(raw.get("port", 8765), "hub.port"),
        hostname=str(raw.get("hostname", "hub.localhost")).lower(),
        open_timeout=float(raw.get("open_timeout", 40)),
    )


def _parse_tunnel(raw: dict[str, Any]) -> TunnelSettings:
    command = raw.get("ssh_command", "ssh")
    if isinstance(command, str):
        command = [command]
    if not command or not all(isinstance(part, str) for part in command):
        raise ConfigError("tunnel.ssh_command: expected a string or a list of strings")

    options = dict(DEFAULT_SSH_OPTIONS)
    for key, value in (raw.get("ssh_options") or {}).items():
        options[str(key)] = _ssh_option_value(value)

    return TunnelSettings(
        ssh_command=tuple(command),
        ssh_options=tuple(options.items()),
        probe_timeout=float(raw.get("probe_timeout", 0.8)),
        ready_timeout=float(raw.get("ready_timeout", 15)),
        health_timeout=float(raw.get("health_timeout", 8)),
        auto_direct_interval=float(raw.get("auto_direct_interval", 15)),
    )


def _parse_service(service_id: str, body: dict[str, Any], auto_direct_default: bool = True) -> Service:
    where = f"service '{service_id}'"
    if not SERVICE_ID_RE.match(service_id):
        raise ConfigError(f"{where}: id must match {SERVICE_ID_RE.pattern}")

    service_type = str(body.get("type", "http")).lower()
    if service_type not in SERVICE_TYPES:
        raise ConfigError(f"{where}: type must be one of {', '.join(SERVICE_TYPES)}")

    if "local_port" not in body:
        raise ConfigError(f"{where}: local_port is required")
    local_port = _check_port(body["local_port"], f"{where}.local_port")

    path = str(body.get("path", "/"))
    if not path.startswith("/"):
        path = "/" + path

    raw_routes = body.get("routes") or []
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ConfigError(f"{where}: routes must be a non-empty list")
    routes = tuple(
        _parse_route(item, f"{where}.routes[{index}]") for index, item in enumerate(raw_routes)
    )

    return Service(
        id=service_id,
        name=str(body.get("name", service_id)),
        type=service_type,
        local_port=local_port,
        hostname=str(body.get("hostname", f"{service_id}.localhost")).lower(),
        path=path,
        autostart=bool(body.get("autostart", False)),
        health_check=bool(body.get("health_check", service_type != "tcp")),
        routes=routes,
        description=str(body.get("description", "")),
        auto_direct=bool(body.get("auto_direct", auto_direct_default)),
        icon=str(body["icon"]) if body.get("icon") else None,
    )


def _parse_route(item: Any, where: str) -> Route:
    if not isinstance(item, dict):
        raise ConfigError(f"{where}: expected a mapping with 'direct' or 'ssh'")
    label = str(item.get("label", ""))

    if "direct" in item:
        host, port = parse_endpoint(item["direct"], where)
        return Route(kind="direct", target_host=host, target_port=port, label=label)

    if "ssh" in item:
        if "remote" not in item:
            raise ConfigError(f"{where}: ssh route needs 'remote' (host:port seen from the SSH server)")
        host, port = parse_endpoint(item["remote"], where, default_host="127.0.0.1")
        return Route(
            kind="ssh", target_host=host, target_port=port, ssh_host=str(item["ssh"]), label=label
        )

    raise ConfigError(f"{where}: expected 'direct' or 'ssh'")


def _check_port_conflicts(hub: HubSettings, services: dict[str, Service]) -> None:
    used = {hub.port: "hub"}
    for service in services.values():
        owner = used.get(service.local_port)
        if owner is not None:
            raise ConfigError(
                f"service '{service.id}': local_port {service.local_port} already used by {owner}"
            )
        used[service.local_port] = f"service '{service.id}'"


# ---------------------------------------------------------------------------
# Helpers


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _check_port(value: Any, where: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: invalid port {value!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}: port {port} out of range")
    return port


def _ssh_option_value(value: Any) -> str:
    # YAML turns `yes` into True; ssh wants yes/no.
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
