"""Finding the PVS6 when its IP moves.

This gateway is on Wi-Fi with a DHCP lease, so its address changes. A poller
that only knows one hard-coded IP stops collecting the moment that happens, and
missed readings can never be recovered -- so the host is resolved through a
chain of candidates instead.

Order, cheapest and most likely first:

1. The last address that actually worked (remembered on disk across restarts).
2. Whatever ``PVS_HOST`` is set to -- an IP or a hostname.
3. mDNS names the PVS advertises: ``pvs.local``, ``pvs6.local``, ``pvs5.local``.
4. The ARP table, matched on the gateway's own MAC, which is learned
   automatically the first time a connection succeeds.

The permanent fix is a DHCP reservation on the router; this makes the app cope
until that happens, and keeps coping if it never does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

#: Names a PVS advertises over mDNS. Windows resolves .local natively.
MDNS_NAMES = ("pvs.local", "pvs6.local", "pvs5.local")

# Accepts both separated (d4:12:43:a2:c3:f6, d4-12-43-a2-c3-f6) and bare
# (d41243a2c3f6) forms, because this has to re-normalise its own output when a
# cached value is read back from disk.
_MAC_RE = re.compile(
    r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|\b[0-9a-f]{12}\b", re.IGNORECASE
)

# Neighbour tables are printed differently on every platform:
#   Windows  arp -a     "  192.168.4.58   d4-12-43-a2-c3-f6   dynamic"
#   BSD/mac  arp -a     "pvs (192.168.4.58) at d4:12:43:a2:c3:f6 on en0"
#   Linux    ip neigh   "192.168.4.58 dev wlan0 lladdr d4:12:43:a2:c3:f6 REACHABLE"
# Rather than a regex per format, each line is scanned for its first IPv4 and
# first MAC and the two are paired. Header lines carry an IP but no MAC and are
# skipped naturally.
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_MAC_IN_LINE_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")

# Tried in order; the first command that exists and prints something wins.
# `ip` is what Raspberry Pi OS ships -- net-tools (and so `arp`) is not
# installed by default on current Debian.
_NEIGHBOUR_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("ip", "neigh"),
    ("arp", "-a"),
)


def normalise_mac(value: str | None) -> str | None:
    """Lower-case a MAC and strip its separators, so formats compare equal."""
    if not value:
        return None
    match = _MAC_RE.search(value)
    if not match:
        return None
    return re.sub(r"[:-]", "", match.group(0)).lower()


@dataclass
class DiscoveryCache:
    """Remembers where the gateway was last seen, across restarts."""

    path: Path
    host: str | None = None
    mac: str | None = None

    @classmethod
    def load(cls, path: Path) -> DiscoveryCache:
        cache = cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cache.host = data.get("host") or None
            cache.mac = normalise_mac(data.get("mac"))
        except (OSError, ValueError):
            # No cache yet, or it is unreadable. Either way, start clean.
            pass
        return cache

    def save(self, host: str | None = None, mac: str | None = None) -> None:
        if host:
            self.host = host
        if mac:
            self.mac = normalise_mac(mac) or self.mac
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"host": self.host, "mac": self.mac}, indent=2),
                encoding="utf-8",
            )
        except OSError as err:
            _LOGGER.debug("Could not write the discovery cache: %s", err)


def resolve_mdns(names: tuple[str, ...] = MDNS_NAMES) -> list[str]:
    """Resolve the PVS's advertised .local names to IPs, skipping failures."""
    found: list[str] = []
    for name in names:
        try:
            address = socket.gethostbyname(name)
        except OSError:
            continue
        if address not in found:
            _LOGGER.debug("mDNS resolved %s to %s", name, address)
            found.append(address)
    return found


def lookup_arp(mac: str | None) -> str | None:
    """Find the current IP for a known MAC in the local ARP table.

    Only sees hosts the machine has talked to recently, so it is a fallback
    rather than a primary method.
    """
    target = normalise_mac(mac)
    if not target:
        return None

    for command in _NEIGHBOUR_COMMANDS:
        try:
            output = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError) as err:
            # Command missing on this platform; try the next one.
            _LOGGER.debug("%s unavailable: %s", " ".join(command), err)
            continue

        address = _match_neighbour(output, target)
        if address:
            _LOGGER.debug(
                "%s matched %s to %s", " ".join(command), target, address
            )
            return address
    return None


def _match_neighbour(output: str, target: str) -> str | None:
    """Find the IPv4 paired with ``target`` in neighbour-table output."""
    for line in output.splitlines():
        macs = _MAC_IN_LINE_RE.findall(line)
        if not macs or normalise_mac(macs[0]) != target:
            continue
        ips = _IPV4_RE.findall(line)
        if ips:
            return ips[0]
    return None


async def candidate_hosts(
    configured_host: str,
    cache: DiscoveryCache,
) -> list[str]:
    """Build the ordered list of addresses to try, without duplicates."""
    candidates: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(cache.host)
    add(configured_host)

    # Name resolution and the ARP shell-out both block, so keep them off the
    # event loop.
    for address in await asyncio.to_thread(resolve_mdns):
        add(address)
    add(await asyncio.to_thread(lookup_arp, cache.mac))

    # The configured name itself may be a .local that only resolves sometimes;
    # having it earlier in the list is fine, this just guarantees the mDNS
    # names are tried even when PVS_HOST is a stale IP.
    for name in MDNS_NAMES:
        add(name)

    return candidates
