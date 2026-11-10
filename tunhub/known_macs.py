"""known_macs: the next-hop MAC of each direct target, like ~/.ssh/known_hosts.

Format: one ``<ip> <mac>`` per line; ``#`` starts a comment; several lines for
one IP mean several trusted MACs (e.g. a machine with wired and Wi-Fi NICs).

tunhub learns on first use (ssh's ``StrictHostKeyChecking accept-new``) by
appending a line. Only ``tunhub forget <ip>`` rewrites the file: it removes
the lines of that IP, like ``ssh-keygen -R``, and keeps everything else
(comments, manual edits, lines it does not understand). The file is re-read
when it changes on disk.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .nexthop import next_hop_mac, normalize_mac

FILE_NAME = "known_macs"
HEADER = (
    "# tunhub known_macs: next-hop MAC per direct target, like ~/.ssh/known_hosts.\n"
    "# tunhub appends a line the first time a direct route connects. If a MAC\n"
    "# changes legitimately (new NIC, other router), remove its line or run\n"
    "# `tunhub forget <ip>`; several lines for one IP are all trusted.\n"
)


@dataclass(frozen=True)
class Entry:
    mac: str
    line: int  # 1-based, for messages that point at the file


def _canonical_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def parse(text: str) -> dict[str, list[Entry]]:
    """Entries by IP. Malformed lines are skipped, like ssh does."""
    entries: dict[str, list[Entry]] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        fields = raw.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        ip, mac = _canonical_ip(fields[0]), normalize_mac(fields[1])
        if ip is None or mac is None:
            continue
        entries.setdefault(ip, []).append(Entry(mac, number))
    return entries


class KnownMacs:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, list[Entry]] = {}
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size) of what we read

    def lookup(self, ip: str) -> list[Entry]:
        self._reload_if_changed()
        return list(self._entries.get(_canonical_ip(ip) or ip, []))

    def add(self, ip: str, mac: str) -> None:
        """Append ``ip mac``; creates the file (with a header) if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            if new_file:
                handle.write(HEADER)
            elif self.path.stat().st_size and not self._ends_with_newline():
                handle.write("\n")
            handle.write(f"{_canonical_ip(ip) or ip} {mac}\n")
        self._stamp = None  # force a re-read so line numbers are right

    def forget(self, ip: str) -> int:
        """Remove every line whose first field is ``ip``; returns how many were removed.

        Raises ValueError if ``ip`` is not an IP address. Lines tunhub does not
        understand (comments, ``@`` markers of later versions, typos) are kept.
        """
        target = _canonical_ip(ip)
        if target is None:
            raise ValueError(f"not an IP address: {ip!r}")
        if not self.path.exists():
            return 0
        kept, removed = [], 0
        for raw in self.path.read_text(encoding="utf-8").splitlines(keepends=True):
            fields = raw.split("#", 1)[0].split()
            if fields and _canonical_ip(fields[0]) == target:
                removed += 1
            else:
                kept.append(raw)
        if removed:
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text("".join(kept), encoding="utf-8", newline="")
            os.replace(temporary, self.path)
            self._stamp = None
        return removed

    def _ends_with_newline(self) -> bool:
        with open(self.path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"

    def _reload_if_changed(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._entries, self._stamp = {}, None
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._stamp:
            self._entries = parse(self.path.read_text(encoding="utf-8"))
            self._stamp = stamp


# ---------------------------------------------------------------------------
# The check itself


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    note: str | None = None  # worth logging: first use, cannot verify, mismatch


class MacGuard:
    """Decides whether a direct target IP is still the machine it was before.

    The goal is to avoid *accidental* misconnections (another network that
    reuses the same private address), not to resist deliberate spoofing.
    """

    def __init__(self, known: KnownMacs, lookup: Callable[[str], str | None] = next_hop_mac,
                 skip_loopback: bool = True) -> None:
        self.known = known
        self.lookup = lookup
        self.skip_loopback = skip_loopback

    async def check(self, ip: str) -> Verdict:
        if self.skip_loopback and ipaddress.ip_address(ip).is_loopback:
            return Verdict(True)  # e.g. local.<machine>.yaml on the server itself
        mac = await asyncio.to_thread(self.lookup, ip)
        try:
            entries = self.known.lookup(ip)
        except (OSError, ValueError) as exc:  # unreadable, or not UTF-8
            # Without the records the target cannot be confirmed: skip direct.
            return Verdict(False, f"cannot read {self.known.path} ({exc}); direct route to {ip} skipped")
        where = ", ".join(f"{entry.mac} (line {entry.line})" for entry in entries)

        if mac is None:
            if entries:
                return Verdict(False, f"cannot verify {ip}: its next-hop MAC is unknown, "
                                      f"but {FILE_NAME} expects {where}")
            return Verdict(True, f"cannot verify {ip} (no next-hop MAC, e.g. through a VPN); using it anyway")
        if not entries:
            try:
                self.known.add(ip, mac)
            except OSError as exc:
                # First use is trusted anyway (accept-new); failing to record
                # it only means the next connection counts as first use again.
                return Verdict(True, f"could not record {ip} ({mac}) in {self.known.path} ({exc}); "
                                     "using it without a record")
            return Verdict(True, f"Permanently added {ip} ({mac}) to {FILE_NAME}")
        if any(entry.mac == mac for entry in entries):
            return Verdict(True)
        return Verdict(False, f"next-hop MAC of {ip} is {mac}, but {FILE_NAME} expects {where}. "
                              f"Another machine may use this address on this network, so the direct "
                              f"route is skipped. If the change is expected, run `tunhub forget {ip}`")
