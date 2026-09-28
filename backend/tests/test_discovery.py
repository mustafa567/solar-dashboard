"""Tests for finding the gateway when its DHCP lease moves it.

This PVS6 is on Wi-Fi, so its address changes. A poller that cannot follow it
silently stops recording, and those readings are gone for good -- which is why
the resolution chain is worth testing rather than trusting.
"""

from __future__ import annotations

import json

import pytest
from conftest import make_settings

from app import discovery
from app.discovery import (
    DiscoveryCache,
    candidate_hosts,
    lookup_arp,
    normalise_mac,
    resolve_mdns,
)

WINDOWS_ARP = """
Interface: 192.168.4.35 --- 0x11
  Internet Address      Physical Address      Type
  192.168.4.1           b0-39-56-1f-2a-01     dynamic
  192.168.4.58          d4-12-43-a2-c3-f6     dynamic
  192.168.4.255         ff-ff-ff-ff-ff-ff     static
"""

LINUX_ARP = """
router (192.168.4.1) at b0:39:56:1f:2a:01 [ether] on eth0
pvs (192.168.4.58) at d4:12:43:a2:c3:f6 [ether] on eth0
"""

# Raspberry Pi OS has no net-tools, so `ip neigh` is what actually runs there.
IP_NEIGH = """
192.168.4.1 dev wlan0 lladdr b0:39:56:1f:2a:01 REACHABLE
192.168.4.58 dev wlan0 lladdr d4:12:43:a2:c3:f6 STALE
"""


class TestMacNormalisation:
    @pytest.mark.parametrize(
        "value",
        ["d4:12:43:a2:c3:f6", "D4-12-43-A2-C3-F6", "  d4-12-43-a2-c3-f6  "],
    )
    def test_separators_and_case_do_not_matter(self, value: str) -> None:
        assert normalise_mac(value) == "d41243a2c3f6"

    @pytest.mark.parametrize("value", [None, "", "not-a-mac", "12:34"])
    def test_rubbish_is_rejected(self, value) -> None:
        assert normalise_mac(value) is None


class TestArpLookup:
    @pytest.mark.parametrize("output", [WINDOWS_ARP, LINUX_ARP, IP_NEIGH])
    def test_finds_the_ip_for_a_known_mac(self, monkeypatch, output) -> None:
        monkeypatch.setattr(
            discovery.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"stdout": output})(),
        )
        assert lookup_arp("D4-12-43-A2-C3-F6") == "192.168.4.58"

    def test_unknown_mac_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            discovery.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"stdout": WINDOWS_ARP})(),
        )
        assert lookup_arp("00:00:00:00:00:01") is None

    def test_a_failing_arp_command_is_not_fatal(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise OSError("arp not found")

        monkeypatch.setattr(discovery.subprocess, "run", boom)
        assert lookup_arp("d4:12:43:a2:c3:f6") is None


    def test_it_falls_through_to_the_next_command(self, monkeypatch) -> None:
        """`ip` is absent on Windows and `arp` on a stock Pi; try both."""
        calls: list[tuple[str, ...]] = []

        def fake_run(command, **kwargs):
            calls.append(tuple(command))
            if command[0] == "ip":
                raise FileNotFoundError("no ip here")
            return type("R", (), {"stdout": WINDOWS_ARP})()

        monkeypatch.setattr(discovery.subprocess, "run", fake_run)
        assert lookup_arp("d4:12:43:a2:c3:f6") == "192.168.4.58"
        assert calls == [("ip", "neigh"), ("arp", "-a")]

    def test_a_header_line_with_no_mac_is_skipped(self, monkeypatch) -> None:
        """Windows prints `Interface: 192.168.4.35 --- 0x11` above the table."""
        monkeypatch.setattr(
            discovery.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"stdout": WINDOWS_ARP})(),
        )
        # The interface line's IP must not be returned for any MAC.
        assert lookup_arp("b0:39:56:1f:2a:01") == "192.168.4.1"


class TestMdns:
    def test_unresolvable_names_are_skipped(self, monkeypatch) -> None:
        def resolver(name):
            if name == "pvs6.local":
                return "192.168.4.58"
            raise OSError("NXDOMAIN")

        monkeypatch.setattr(discovery.socket, "gethostbyname", resolver)
        assert resolve_mdns() == ["192.168.4.58"]

    def test_duplicates_are_collapsed(self, monkeypatch) -> None:
        monkeypatch.setattr(
            discovery.socket, "gethostbyname", lambda name: "192.168.4.58"
        )
        assert resolve_mdns() == ["192.168.4.58"]


class TestCache:
    def test_round_trips_host_and_mac(self, tmp_path) -> None:
        path = tmp_path / "gateway-location.json"
        DiscoveryCache(path=path).save(host="192.168.4.58", mac="D4-12-43-A2-C3-F6")

        reloaded = DiscoveryCache.load(path)
        assert reloaded.host == "192.168.4.58"
        assert reloaded.mac == "d41243a2c3f6"

    def test_a_missing_or_corrupt_cache_starts_clean(self, tmp_path) -> None:
        missing = DiscoveryCache.load(tmp_path / "nope.json")
        assert missing.host is None and missing.mac is None

        corrupt = tmp_path / "bad.json"
        corrupt.write_text("{not json", encoding="utf-8")
        assert DiscoveryCache.load(corrupt).host is None

    def test_saving_is_not_fatal_when_the_path_is_unwritable(
        self, tmp_path, monkeypatch
    ) -> None:
        cache = DiscoveryCache(path=tmp_path / "x.json")
        monkeypatch.setattr(
            discovery.Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError)
        )
        cache.save(host="192.168.4.58")  # must not raise


class TestCandidateOrder:
    async def test_last_known_good_is_tried_before_the_configured_host(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(discovery, "resolve_mdns", lambda *a, **k: [])
        monkeypatch.setattr(discovery, "lookup_arp", lambda mac: None)

        cache = DiscoveryCache(path=tmp_path / "c.json", host="192.168.4.58")
        hosts = await candidate_hosts("192.168.4.67", cache)

        assert hosts[0] == "192.168.4.58"
        assert hosts[1] == "192.168.4.67"

    async def test_every_method_contributes_and_nothing_repeats(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(discovery, "resolve_mdns", lambda *a, **k: ["192.168.4.58"])
        monkeypatch.setattr(discovery, "lookup_arp", lambda mac: "192.168.4.77")

        cache = DiscoveryCache(
            path=tmp_path / "c.json", host="192.168.4.58", mac="d41243a2c3f6"
        )
        hosts = await candidate_hosts("192.168.4.67", cache)

        assert len(hosts) == len(set(hosts))
        assert "192.168.4.58" in hosts  # cache + mDNS, listed once
        assert "192.168.4.67" in hosts  # configured
        assert "192.168.4.77" in hosts  # ARP
        # The advertised names are always tried, even if PVS_HOST is a stale IP.
        assert "pvs.local" in hosts

    async def test_the_mdns_names_are_present_with_no_other_signal(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(discovery, "resolve_mdns", lambda *a, **k: [])
        monkeypatch.setattr(discovery, "lookup_arp", lambda mac: None)

        hosts = await candidate_hosts("", DiscoveryCache(path=tmp_path / "c.json"))
        assert hosts == list(discovery.MDNS_NAMES)
