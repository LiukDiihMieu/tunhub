"""Configuration loading.

The shared ``services.yaml`` defines every service and the ordered list of
routes to try. An optional ``local.<machine>.yaml`` (named after each machine,
so the whole directory can be synced) is deep-merged on top of it to override
or disable services on that machine only.
"""

from __future__ import annotations

import difflib
import ipaddress
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from . import __version__
from .paths import MACHINE_ENV, machine_name

SHARED_FILE = "services.yaml"
# The dashboard's port when hub.port is not set (tunhub's release date, 11-10).
DEFAULT_HUB_PORT = 1110
# The per-machine override file, as shown in messages.
LOCAL_FILE = "local.<machine>.yaml"


def local_file_name(machine: str) -> str:
    return f"local.{machine}.yaml"


SERVICE_TYPES = ("http", "https", "tcp")
# A service id becomes a DNS label (<id>.localhost): letters, digits and
# inner hyphens, at most 63 characters.
SERVICE_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Known keys per section. Unknown keys are warnings, not errors, so that an
# older tunhub can still read a config written for a newer one.
TOP_KEYS = ("requires_tunhub", "hub", "tunnel", "services")
HUB_KEYS = ("listen", "port", "hostname", "open_timeout")
TUNNEL_KEYS = (
    "ssh_command",
    "ssh_options",
    "probe_timeout",
    "ready_timeout",
    "health_timeout",
    "auto_direct_interval",
    "auto_direct",
)
SERVICE_KEYS = (
    "name",
    "description",
    "type",
    "local_port",
    "hostname",
    "path",
    "icon",
    "routes",
    "autostart",
    "health_check",
    "auto_direct",
    "enabled",
)
DIRECT_ROUTE_KEYS = ("direct", "label")
SSH_ROUTE_KEYS = ("ssh", "remote", "label")

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
        names = {self.hostname, "localhost", "127.0.0.1", "[::1]"}
        return frozenset(f"{name}:{self.port}" for name in names)


@dataclass(frozen=True)
class Config:
    hub: HubSettings
    tunnel: TunnelSettings
    services: dict[str, Service]
    # False when services.yaml does not exist yet (the dashboard then shows
    # how to create it instead of refusing to start).
    found: bool = True
    # Problems that do not stop tunhub (e.g. an unknown key); shown on the dashboard.
    warnings: tuple[str, ...] = ()
    # This machine's name and the override file it reads (which may not exist).
    machine: str = ""
    local_path: Path | None = None
    local_found: bool = False


# ---------------------------------------------------------------------------
# Public API


def load_config(config_dir: Path, machine: str | None = None) -> Config:
    """Read services.yaml (+ local.<machine>.yaml). A missing services.yaml is an empty config."""
    shared_path = config_dir / SHARED_FILE
    found = shared_path.exists()
    raw = _read_yaml(shared_path) if found else {}
    shared_warnings = _shared_file_warnings(raw)

    machine = machine_name() if machine is None else machine
    local_path: Path | None = None
    local_found = False
    if _usable_machine_name(machine):
        local_path = config_dir / local_file_name(machine)
        local_found = local_path.exists()
        if local_found:
            raw = deep_merge(raw, _read_yaml(local_path))
    else:
        shared_warnings.append(
            f"machine name {machine!r} cannot be used in a file name, so no {LOCAL_FILE} is read; "
            f"set {MACHINE_ENV} to a simple name"
        )

    config = parse_config(raw)
    return replace(
        config,
        found=found,
        warnings=config.warnings + tuple(shared_warnings),
        machine=machine,
        local_path=local_path,
        local_found=local_found,
    )


def _usable_machine_name(machine: str) -> bool:
    return bool(machine) and machine.isprintable() and not any(char in machine for char in '/\\:*?"<>|')


def parse_config(raw: dict[str, Any]) -> Config:
    # First: a config for a newer tunhub may not parse at all.
    _check_required_version(raw.get("requires_tunhub"))
    warnings: list[str] = []
    _warn_unknown_keys(raw, TOP_KEYS, "config", warnings)
    hub_raw = _mapping(raw.get("hub"), "hub")
    _warn_unknown_keys(hub_raw, HUB_KEYS, "hub", warnings)
    hub = _parse_hub(hub_raw)
    tunnel_raw = _mapping(raw.get("tunnel"), "tunnel")
    _warn_unknown_keys(tunnel_raw, TUNNEL_KEYS, "tunnel", warnings)
    tunnel = _parse_tunnel(tunnel_raw)
    auto_direct_default = _flag(tunnel_raw, "auto_direct", True, "tunnel")

    services: dict[str, Service] = {}
    for service_id, body in _mapping(raw.get("services"), "services").items():
        service_id = str(service_id)
        body = _mapping(body, f"service '{service_id}'")
        if not _flag(body, "enabled", True, f"service '{service_id}'"):
            continue
        services[service_id] = _parse_service(service_id, body, auto_direct_default, warnings)

    _check_port_conflicts(hub, services)
    _check_hostname_conflicts(hub, services)
    _warn_remote_ip_hostnames(hub, services, warnings)
    return Config(hub=hub, tunnel=tunnel, services=services, warnings=tuple(warnings))


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
    """Parse ``host:port``, ``[v6]:port`` or a bare port into (host, port).

    This only splits off the host; _check_port() alone decides what a valid
    port is.
    """
    if not (isinstance(value, str) and ":" in value):  # no host given
        if default_host is None:
            raise ConfigError(f"{where}: expected 'host:port', got {value!r}")
        return default_host, _check_port(value, where)

    text = value.strip()
    match = re.fullmatch(r"\[([^\]]+)\]:(\S+)|([^:\s]+):(\S+)", text)
    if not match:
        raise ConfigError(f"{where}: cannot parse endpoint '{text}' (expected host:port)")
    host = match.group(1) or match.group(3)
    return host, _check_port(match.group(2) or match.group(4), where)


def format_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# ---------------------------------------------------------------------------
# Section parsers


def _parse_hub(raw: dict[str, Any]) -> HubSettings:
    return HubSettings(
        listen_host=_check_listen(_text(raw, "listen", "127.0.0.1", "hub")),
        port=_check_port(_or_default(raw, "port", DEFAULT_HUB_PORT), "hub.port"),
        hostname=_check_hostname(_text(raw, "hostname", "hub.localhost", "hub"), "hub.hostname"),
        open_timeout=_seconds(raw, "open_timeout", 40, "hub"),
    )


def _parse_tunnel(raw: dict[str, Any]) -> TunnelSettings:
    command = _or_default(raw, "ssh_command", "ssh")
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ConfigError("tunnel.ssh_command: expected a string or a list of strings")

    options = dict(DEFAULT_SSH_OPTIONS)
    for key, value in _mapping(raw.get("ssh_options"), "tunnel.ssh_options").items():
        key = str(key)
        if value is None:  # not set: back to tunhub's default, if it has one
            if key in DEFAULT_SSH_OPTIONS:
                options[key] = DEFAULT_SSH_OPTIONS[key]
            else:
                options.pop(key, None)
            continue
        options[key] = _ssh_option_value(value, f"tunnel.ssh_options.{key}")

    return TunnelSettings(
        ssh_command=tuple(command),
        ssh_options=tuple(options.items()),
        probe_timeout=_seconds(raw, "probe_timeout", 0.8, "tunnel"),
        ready_timeout=_seconds(raw, "ready_timeout", 15, "tunnel"),
        health_timeout=_seconds(raw, "health_timeout", 8, "tunnel"),
        auto_direct_interval=_seconds(raw, "auto_direct_interval", 15, "tunnel"),
    )


def _parse_service(
    service_id: str, body: dict[str, Any], auto_direct_default: bool, warnings: list[str]
) -> Service:
    where = f"service '{service_id}'"
    if not SERVICE_ID_RE.match(service_id):
        raise ConfigError(f"{where}: id must match {SERVICE_ID_RE.pattern}")
    _warn_unknown_keys(body, SERVICE_KEYS, where, warnings)

    service_type = _text(body, "type", "http", where).lower()
    if service_type not in SERVICE_TYPES:
        raise ConfigError(f"{where}: type must be one of {', '.join(SERVICE_TYPES)}")

    if body.get("local_port") is None:
        raise ConfigError(f"{where}: local_port is required")
    local_port = _check_port(body["local_port"], f"{where}.local_port")

    path = _text(body, "path", "/", where)
    if not path.startswith("/"):
        path = "/" + path

    raw_routes = body.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ConfigError(f"{where}: routes must be a non-empty list")
    routes = tuple(
        _parse_route(item, f"{where}.routes[{index}]", warnings) for index, item in enumerate(raw_routes)
    )

    return Service(
        id=service_id,
        name=_text(body, "name", service_id, where),
        type=service_type,
        local_port=local_port,
        hostname=_check_hostname(_text(body, "hostname", f"{service_id}.localhost", where), f"{where}.hostname"),
        path=path,
        autostart=_flag(body, "autostart", False, where),
        health_check=_flag(body, "health_check", service_type != "tcp", where),
        routes=routes,
        description=_text(body, "description", "", where),
        auto_direct=_flag(body, "auto_direct", auto_direct_default, where),
        icon=_text(body, "icon", "", where) or None,
    )


def _parse_route(item: Any, where: str, warnings: list[str]) -> Route:
    if not isinstance(item, dict):
        raise ConfigError(f"{where}: expected a mapping with 'direct' or 'ssh'")
    label = _text(item, "label", "", where)

    if "direct" in item and "ssh" in item:
        # Usually a missing '-': two routes written as one.
        raise ConfigError(
            f"{where}: has both 'direct' and 'ssh'; a route is one or the other. "
            "Did you mean two routes (a '-' before 'ssh')?"
        )
    if "direct" in item:
        _warn_unknown_keys(item, DIRECT_ROUTE_KEYS, f"{where} (direct)", warnings)
        host, port = parse_endpoint(item["direct"], where)
        return Route(kind="direct", target_host=host, target_port=port, label=label)

    if "ssh" in item:
        _warn_unknown_keys(item, SSH_ROUTE_KEYS, f"{where} (ssh)", warnings)
        if "remote" not in item:
            raise ConfigError(f"{where}: ssh route needs 'remote' (host:port seen from the SSH server)")
        host, port = parse_endpoint(item["remote"], where, default_host="127.0.0.1")
        alias = _text(item, "ssh", "", where)
        if not alias:
            raise ConfigError(f"{where}.ssh: needs a host alias from ~/.ssh/config")
        if alias.startswith("-"):
            # ssh would read it as an option (e.g. -oProxyCommand=...).
            raise ConfigError(f"{where}.ssh: a host alias cannot start with '-'")
        return Route(kind="ssh", target_host=host, target_port=port, ssh_host=alias, label=label)

    raise ConfigError(f"{where}: expected 'direct' or 'ssh'")


def _check_required_version(spec: Any) -> None:
    """`requires_tunhub: ">=0.1.5"`: the tunhub versions this config is written for (PEP 440)."""
    version = __version__
    if spec is None:
        return
    if not isinstance(spec, str) or not spec.strip():
        raise ConfigError(f"requires_tunhub: expected a version range such as '>=0.1.5', got {spec!r}")
    try:
        allowed = SpecifierSet(spec)
    except InvalidSpecifier:
        raise ConfigError(f"requires_tunhub: {spec!r} is not a version range such as '>=0.1.5'") from None
    # prereleases: a development build may match too (0.1.5.dev1 matches >=0.1.5.dev0
    # and >=0.1, but not >=0.1.5, which it comes before).
    if not allowed.contains(version, prereleases=True):
        raise ConfigError(f"this config requires tunhub {spec}, but this is tunhub {version}; please upgrade tunhub")


# ---------------------------------------------------------------------------
# Warnings


def _warn_unknown_keys(raw: dict[str, Any], known: tuple[str, ...], where: str, warnings: list[str]) -> None:
    for key in raw:
        key = str(key)
        if key in known:
            continue
        message = f"{where}: unknown key '{key}' (ignored)"
        close = difflib.get_close_matches(key, known, n=1)
        if close:
            message += f"; did you mean '{close[0]}'?"
        warnings.append(message)


def _warn_remote_ip_hostnames(hub: HubSettings, services: dict[str, Service], warnings: list[str]) -> None:
    """An IP address as host name must be this machine, or the browser skips tunhub."""
    users = [("hub.hostname", hub.hostname)]
    users += [(f"service '{s.id}'.hostname", s.hostname) for s in services.values() if s.is_web]
    for where, name in users:
        if _is_ip(name) and not ipaddress.ip_address(name).is_loopback:
            warnings.append(
                f"{where}: {name} is another machine, so the browser would connect there directly and "
                "bypass tunhub; use 127.0.0.1 or a name ending in .localhost"
            )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _shared_file_warnings(shared: dict[str, Any]) -> list[str]:
    """Direct routes to this machine do not belong in the shared file.

    On every machine 127.0.0.1 is that machine itself, and known_macs cannot
    check a loopback target. Lenient: errors are reported by parse_config().
    """
    warnings: list[str] = []
    services = shared.get("services")
    if not isinstance(services, dict):
        return warnings
    for service_id, body in services.items():
        if not isinstance(body, dict) or body.get("enabled") is False or not isinstance(body.get("routes"), list):
            continue
        for index, route in enumerate(body["routes"]):
            if not isinstance(route, dict) or "direct" not in route:
                continue
            try:
                host, port = parse_endpoint(route["direct"], "")
            except ConfigError:
                continue
            if _is_loopback_host(host):
                warnings.append(
                    f"{SHARED_FILE}: service '{service_id}'.routes[{index}] connects directly to "
                    f"{format_endpoint(host, port)}, which is a different machine on every computer; "
                    f"put this route in {LOCAL_FILE} of the machine that runs the service"
                )
    return warnings


def _check_hostname_conflicts(hub: HubSettings, services: dict[str, Service]) -> None:
    """Each service gets its own host name: cookies and saved passwords are per host.

    IP addresses and `localhost` are exempt from the duplicate check: they are
    the workaround for apps that reject `<id>.localhost`, and whoever uses it
    accepts that those services share cookies.
    """
    owners: dict[str, str] = {}
    for service in services.values():
        name = service.hostname
        if name == hub.hostname:
            raise ConfigError(f"service '{service.id}': host name {name} is the dashboard's own host name")
        if name == "localhost" or _is_ip(name):
            continue
        if name in owners:
            raise ConfigError(
                f"service '{service.id}': host name {name} is already used by service '{owners[name]}' "
                "(they would share cookies and saved passwords)"
            )
        owners[name] = service.id


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
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return _mapping(data, f"{path}: top level")  # an empty file is an empty mapping


def _mapping(value: Any, where: str) -> dict[str, Any]:
    """A YAML mapping. Only null (a missing or empty section) counts as empty;
    anything else that is not a mapping, such as false, 0 or [], is an error."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _or_default(raw: dict[str, Any], key: str, default: Any) -> Any:
    """The value of an optional key; null (or a missing key) means the default."""
    value = raw.get(key)
    return default if value is None else value


def _text(raw: dict[str, Any], key: str, default: str, where: str) -> str:
    """Text; null means the default. Numbers are read as text, true/false are not."""
    value = _or_default(raw, key, default)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{where}.{key}: expected text, got {value!r}")
    return str(value)


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _check_hostname(name: str, where: str) -> str:
    """A host name for URLs: an IPv4 address or a valid DNS name (lowercased)."""
    name = name.lower()
    if _is_ip(name):
        if ipaddress.ip_address(name).version == 6:
            raise ConfigError(f"{where}: an IPv6 address cannot be used as a host name here")
        return name
    labels = name.split(".")
    if len(name) > 253 or not all(_DNS_LABEL_RE.match(label) for label in labels):
        raise ConfigError(f"{where}: {name!r} is not a valid host name")
    return name


def _check_listen(host: str) -> str:
    """The dashboard has no login, so it may only listen on this machine."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError(
            f"hub.listen: {host!r} is not allowed; use 127.0.0.1 or ::1 (the dashboard has no "
            "login, so it must not be reachable from other machines)"
        )
    return host


def _seconds(raw: dict[str, Any], key: str, default: float, where: str) -> float:
    """A positive duration in seconds; null means the default."""
    value = _or_default(raw, key, default)
    if isinstance(value, bool):  # bool is an int subclass; reject `true`
        value = None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}.{key}: expected a number of seconds, got {value!r}") from None
    if not 0 < seconds < float("inf"):
        raise ConfigError(f"{where}.{key}: must be a positive number of seconds, got {seconds}")
    return seconds


def _flag(raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
    """A true/false value (null means the default); "false" as text is rejected, not read as true."""
    value = _or_default(raw, key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: expected true or false, got {value!r}")
    return value


def _check_port(value: Any, where: str) -> int:
    """The one rule for ports: an int, or a string of ASCII digits, in 1-65535.

    Not int(): it truncates 8765.7 and overflows on inf. Not str.isdigit():
    it accepts '²' (which int() then rejects) and other non-ASCII digits.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        port = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        port = int(value)
    else:
        raise ConfigError(f"{where}: invalid port {value!r}")
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}: port {port} out of range")
    return port


def _ssh_option_value(value: Any, where: str) -> str:
    # YAML turns `yes` into True; ssh wants yes/no.
    if isinstance(value, bool):
        return "yes" if value else "no"
    if not isinstance(value, (str, int, float)):
        raise ConfigError(f"{where}: expected a single value, got {value!r}")
    return str(value)
