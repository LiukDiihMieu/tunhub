"""Find the MAC address of the next hop toward an address, and a network fingerprint.

Used to notice that a direct route reaches a different machine than before
(see known_macs.py). The next hop is the target itself when it is on the same
subnet, otherwise the router in front of it.

Everything here is best effort: any failure returns None ("unknown"), never
raises. Lookups are blocking; call them from a worker thread.

- Linux:   `ip -j route get` and `ip -j neigh show` (iproute2, JSON output)
- macOS:   `route -n get`, `arp -n` and `ifconfig`
- Windows: GetBestRoute and SendARP from iphlpapi.dll, through ctypes
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass

COMMAND_TIMEOUT = 2.0
# Any public address works: it only selects the route the machine uses by default.
DEFAULT_ROUTE_PROBE = "1.1.1.1"


@dataclass(frozen=True)
class NetworkFingerprint:
    """Identifies the network this machine is on (see the design notes in README).

    Deliberately not the interface name or the exact local IP: those change
    without the network changing (Wi-Fi to Ethernet, a new DHCP lease).
    """

    gateway_ip: str
    gateway_mac: str
    local_subnet: str


def normalize_mac(text: str) -> str | None:
    """'A:B:1:22:3:44' or 'a-b-01-22-03-44' -> 'aa:bb:01:22:03:44'; None if invalid."""
    parts = re.split(r"[:-]", text.strip())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9A-Fa-f]{1,2}", part) for part in parts):
        return None
    mac = ":".join(part.lower().zfill(2) for part in parts)
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    return mac


def next_hop_mac(ip: str) -> str | None:
    """MAC of the next hop toward ``ip`` (an IPv4 address), or None if unknown."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_next_hop_mac(ip)
        if sys.platform == "darwin":
            return _macos_next_hop_mac(ip)
        if sys.platform == "win32":
            return _windows_next_hop_mac(ip)
    except Exception:  # noqa: BLE001 - best effort by design
        return None
    return None


def network_fingerprint() -> NetworkFingerprint | None:
    """Fingerprint of the current default network, or None if it cannot be determined."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_fingerprint()
        if sys.platform == "darwin":
            return _macos_fingerprint()
        if sys.platform == "win32":
            return _windows_fingerprint()
    except Exception:  # noqa: BLE001 - best effort by design
        return None
    return None


def _run(*argv: str) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _subnet(local_ip: str, prefix: int | str) -> str:
    return str(ipaddress.ip_interface(f"{local_ip}/{prefix}").network)


# ---------------------------------------------------------------------- Linux


def parse_ip_route_get(output: str) -> tuple[str, str, str | None] | None:
    """`ip -j route get X` -> (next hop, device, preferred source address)."""
    routes = json.loads(output)
    if not routes:
        return None
    route = routes[0]
    if route.get("type") in ("local", "unreachable", "blackhole", "prohibit"):
        return None
    return route.get("gateway") or route["dst"], route["dev"], route.get("prefsrc")


def parse_ip_neigh(output: str) -> str | None:
    """`ip -j neigh show X dev D` -> the MAC of a usable entry."""
    for entry in json.loads(output):
        if "FAILED" in entry.get("state", []) or "INCOMPLETE" in entry.get("state", []):
            continue
        mac = normalize_mac(entry.get("lladdr", ""))
        if mac:
            return mac
    return None


def parse_ip_addr_subnet(output: str, local_ip: str) -> str | None:
    """`ip -j -4 addr show dev D` -> the subnet that contains ``local_ip``."""
    for link in json.loads(output):
        for info in link.get("addr_info", []):
            if info.get("family") == "inet" and info.get("local") == local_ip:
                return _subnet(local_ip, info["prefixlen"])
    return None


def _linux_neighbour_mac(next_hop: str, dev: str) -> str | None:
    output = _run("ip", "-j", "neigh", "show", next_hop, "dev", dev)
    return parse_ip_neigh(output) if output else None


def _linux_next_hop_mac(ip: str) -> str | None:
    output = _run("ip", "-j", "route", "get", ip)
    route = parse_ip_route_get(output) if output else None
    if route is None:
        return None
    next_hop, dev, _ = route
    return _linux_neighbour_mac(next_hop, dev)


def _linux_fingerprint() -> NetworkFingerprint | None:
    output = _run("ip", "-j", "route", "get", DEFAULT_ROUTE_PROBE)
    route = parse_ip_route_get(output) if output else None
    if route is None:
        return None
    gateway, dev, local_ip = route
    if gateway == DEFAULT_ROUTE_PROBE or local_ip is None:
        return None  # no gateway (e.g. a point-to-point VPN)
    mac = _linux_neighbour_mac(gateway, dev)
    addr = _run("ip", "-j", "-4", "addr", "show", "dev", dev)
    subnet = parse_ip_addr_subnet(addr, local_ip) if addr else None
    if mac is None or subnet is None:
        return None
    return NetworkFingerprint(gateway_ip=gateway, gateway_mac=mac, local_subnet=subnet)


# ---------------------------------------------------------------------- macOS


def parse_macos_route_get(output: str) -> tuple[str | None, str | None]:
    """`route -n get X` -> (gateway or None when on-link, interface)."""
    fields = dict(re.findall(r"^\s*(\w+):\s*(\S+)", output, re.MULTILINE))
    gateway = fields.get("gateway")
    if gateway is not None:
        try:
            ipaddress.ip_address(gateway)
        except ValueError:
            gateway = None  # e.g. 'link#4': on-link
    return gateway, fields.get("interface")


def parse_macos_arp(output: str) -> str | None:
    """`arp -n X` -> MAC, e.g. '? (192.168.1.1) at a:b:c:d:e:f on en0 ifscope [ethernet]'."""
    match = re.search(r"\bat\s+([0-9A-Fa-f:]+)\s", output)
    return normalize_mac(match.group(1)) if match else None


def parse_macos_ifconfig_subnet(output: str, local_ip: str | None = None) -> str | None:
    """`ifconfig IF` -> subnet of its (matching) IPv4 address; netmask is hex there."""
    for ip, mask in re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9A-Fa-f]+)", output):
        if local_ip is None or ip == local_ip:
            prefix = bin(int(mask, 16)).count("1")
            return _subnet(ip, prefix)
    return None


def _macos_next_hop_mac(ip: str) -> str | None:
    output = _run("route", "-n", "get", ip)
    if output is None:
        return None
    gateway, _ = parse_macos_route_get(output)
    arp = _run("arp", "-n", gateway or ip)
    return parse_macos_arp(arp) if arp else None


def _macos_fingerprint() -> NetworkFingerprint | None:
    output = _run("route", "-n", "get", "default")
    if output is None:
        return None
    gateway, interface = parse_macos_route_get(output)
    if gateway is None or interface is None:
        return None
    arp = _run("arp", "-n", gateway)
    mac = parse_macos_arp(arp) if arp else None
    ifconfig = _run("ifconfig", interface)
    subnet = parse_macos_ifconfig_subnet(ifconfig) if ifconfig else None
    if mac is None or subnet is None:
        return None
    return NetworkFingerprint(gateway_ip=gateway, gateway_mac=mac, local_subnet=subnet)


# -------------------------------------------------------------------- Windows

MIB_IPROUTE_TYPE_DIRECT = 3  # the destination is on the local subnet


def _ipv4_dword(ip: str) -> int:
    # IPv4 addresses in iphlpapi are DWORDs holding the bytes in network order.
    return struct.unpack("<I", socket.inet_aton(ip))[0]


def _dword_ipv4(value: int) -> str:
    return socket.inet_ntoa(struct.pack("<I", value))


def _windows_best_route(ip: str):
    import ctypes
    from ctypes import wintypes

    class MibIpForwardRow(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD)
            for name in (
                "dwForwardDest", "dwForwardMask", "dwForwardPolicy", "dwForwardNextHop",
                "dwForwardIfIndex", "dwForwardType", "dwForwardProto", "dwForwardAge",
                "dwForwardNextHopAS", "dwForwardMetric1", "dwForwardMetric2",
                "dwForwardMetric3", "dwForwardMetric4", "dwForwardMetric5",
            )
        ]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    row = MibIpForwardRow()
    if iphlpapi.GetBestRoute(wintypes.DWORD(_ipv4_dword(ip)), wintypes.DWORD(0), ctypes.byref(row)) != 0:
        return None
    return row


def _windows_arp(ip: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    iphlpapi = ctypes.WinDLL("iphlpapi")
    buffer = (ctypes.c_ubyte * 8)()
    length = wintypes.ULONG(8)
    if iphlpapi.SendARP(wintypes.DWORD(_ipv4_dword(ip)), wintypes.DWORD(0), buffer, ctypes.byref(length)) != 0:
        return None
    return normalize_mac(":".join(f"{byte:02x}" for byte in bytes(buffer)[: length.value]))


def _windows_next_hop_mac(ip: str) -> str | None:
    row = _windows_best_route(ip)
    if row is None:
        return None
    on_link = row.dwForwardType == MIB_IPROUTE_TYPE_DIRECT or row.dwForwardNextHop == 0
    return _windows_arp(ip if on_link else _dword_ipv4(row.dwForwardNextHop))


def _windows_fingerprint() -> NetworkFingerprint | None:
    row = _windows_best_route(DEFAULT_ROUTE_PROBE)
    if row is None or row.dwForwardType == MIB_IPROUTE_TYPE_DIRECT or row.dwForwardNextHop == 0:
        return None
    gateway = _dword_ipv4(row.dwForwardNextHop)
    mac = _windows_arp(gateway)
    # The best route to the gateway is the on-link route of the local subnet.
    subnet_row = _windows_best_route(gateway)
    if mac is None or subnet_row is None:
        return None
    network = _dword_ipv4(subnet_row.dwForwardDest)
    prefix = bin(subnet_row.dwForwardMask).count("1")
    return NetworkFingerprint(gateway_ip=gateway, gateway_mac=mac, local_subnet=_subnet(network, prefix))
