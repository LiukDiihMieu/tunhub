"""Parsing of next-hop / ARP lookups, with output samples from each OS."""

import re
import sys

import pytest

from tunhub import nexthop
from tunhub.nexthop import (
    NetworkFingerprint,
    normalize_mac,
    parse_ip_addr_subnet,
    parse_ip_neigh,
    parse_ip_route_get,
    parse_macos_arp,
    parse_macos_ifconfig_subnet,
    parse_macos_route_get,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("AA:BB:CC:DD:EE:01", "aa:bb:cc:dd:ee:01"),
        ("a:b:c:1:2:3", "0a:0b:0c:01:02:03"),  # macOS drops leading zeros
        ("aa-bb-cc-dd-ee-01", "aa:bb:cc:dd:ee:01"),  # Windows style
        ("00:00:00:00:00:00", None),
        ("ff:ff:ff:ff:ff:ff", None),
        ("aa:bb:cc:dd:ee", None),
        ("(incomplete)", None),
        ("", None),
    ],
)
def test_normalize_mac(text, expected):
    assert normalize_mac(text) == expected


# ----------------------------------------------------------------- Linux

def test_ip_route_get_on_link_target_is_its_own_next_hop():
    out = '[{"dst":"192.168.1.10","dev":"eno1","prefsrc":"192.168.1.20","flags":[],"uid":1000,"cache":[]}]'
    assert parse_ip_route_get(out) == ("192.168.1.10", "eno1", "192.168.1.20")


def test_ip_route_get_routed_target_goes_through_gateway():
    out = '[{"dst":"1.1.1.1","gateway":"192.168.1.1","dev":"wlan0","prefsrc":"192.168.1.20","flags":[],"uid":1000,"cache":[]}]'
    assert parse_ip_route_get(out) == ("192.168.1.1", "wlan0", "192.168.1.20")


def test_ip_route_get_local_or_unreachable_has_no_next_hop():
    local = '[{"type":"local","dst":"127.0.0.1","dev":"lo","table":"local","prefsrc":"127.0.0.1"}]'
    assert parse_ip_route_get(local) is None
    assert parse_ip_route_get("[]") is None


@pytest.mark.parametrize(
    "out, expected",
    [
        ('[{"dst":"192.168.1.1","lladdr":"AA:BB:CC:DD:EE:01","state":["REACHABLE"]}]', "aa:bb:cc:dd:ee:01"),
        ('[{"dst":"192.168.1.1","lladdr":"aa:bb:cc:dd:ee:01","state":["STALE"]}]', "aa:bb:cc:dd:ee:01"),
        ('[{"dst":"192.168.1.1","state":["FAILED"]}]', None),
        ('[{"dst":"192.168.1.1","lladdr":"aa:bb:cc:dd:ee:01","state":["INCOMPLETE"]}]', None),
        ("[]", None),
    ],
)
def test_ip_neigh(out, expected):
    assert parse_ip_neigh(out) == expected


def test_ip_addr_subnet_picks_the_matching_address():
    out = (
        '[{"ifname":"eno1","addr_info":['
        '{"family":"inet","local":"10.0.0.5","prefixlen":8},'
        '{"family":"inet","local":"192.168.1.20","prefixlen":24}]}]'
    )
    assert parse_ip_addr_subnet(out, "192.168.1.20") == "192.168.1.0/24"
    assert parse_ip_addr_subnet(out, "172.16.0.1") is None


# ----------------------------------------------------------------- macOS

MACOS_ROUTE_DEFAULT = """\
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""

MACOS_ROUTE_ON_LINK = """\
   route to: 192.168.1.10
destination: 192.168.1.0
       mask: 255.255.255.0
  interface: en0
      flags: <UP,DONE,CLONING,STATIC>
"""

MACOS_ROUTE_LINK_GATEWAY = """\
   route to: 192.168.1.10
destination: 192.168.1.10
    gateway: link#14
  interface: en0
"""


def test_macos_route_get():
    assert parse_macos_route_get(MACOS_ROUTE_DEFAULT) == ("192.168.1.1", "en0")
    assert parse_macos_route_get(MACOS_ROUTE_ON_LINK) == (None, "en0")
    assert parse_macos_route_get(MACOS_ROUTE_LINK_GATEWAY) == (None, "en0")


@pytest.mark.parametrize(
    "out, expected",
    [
        ("? (192.168.1.1) at a:b:c:d:e:1 on en0 ifscope [ethernet]\n", "0a:0b:0c:0d:0e:01"),
        ("? (192.168.1.10) at aa:bb:cc:dd:ee:10 on en0 ifscope permanent [ethernet]\n", "aa:bb:cc:dd:ee:10"),
        ("? (192.168.1.99) at (incomplete) on en0 ifscope [ethernet]\n", None),
        ("192.168.1.99 (192.168.1.99) -- no entry\n", None),
    ],
)
def test_macos_arp(out, expected):
    assert parse_macos_arp(out) == expected


def test_macos_ifconfig_subnet():
    out = (
        "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
        "\tinet6 fe80::1%en0 prefixlen 64 secured scopeid 0xe\n"
        "\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255\n"
    )
    assert parse_macos_ifconfig_subnet(out) == "192.168.1.0/24"


# ----------------------------------------------------------- live smoke

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def test_live_lookups_never_raise_and_return_valid_values():
    # Runs against the real network of whatever machine runs the tests (on CI:
    # Linux, macOS and Windows runners). Best effort: None is fine.
    fingerprint = nexthop.network_fingerprint()
    assert fingerprint is None or isinstance(fingerprint, NetworkFingerprint)
    if fingerprint is not None:
        assert MAC_RE.match(fingerprint.gateway_mac)
        assert fingerprint.local_subnet.count("/") == 1
        gateway_mac = nexthop.next_hop_mac(fingerprint.gateway_ip)
        assert gateway_mac is None or MAC_RE.match(gateway_mac)
    assert nexthop.next_hop_mac("127.0.0.1") is None
    assert nexthop.next_hop_mac("not-an-ip") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes lookups")
def test_windows_lookup_of_the_gateway():
    fingerprint = nexthop.network_fingerprint()
    print("fingerprint:", fingerprint)  # shown in CI logs for diagnosis
    if fingerprint is not None:
        assert nexthop.next_hop_mac("1.1.1.1") == fingerprint.gateway_mac
