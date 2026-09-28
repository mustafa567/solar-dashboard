"""Tests for poller resilience.

A crashed poller means a permanent hole in the history, so these tests are about
the loop surviving things rather than about happy-path readings.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_settings

from app.config import Settings, mask_serial
from app.models import Reading, utcnow
from app.poller import Poller
from app.pvs_client import GatewayError, GatewayNotConfigured
from app.sqlite_store import SqliteReadingStore


class ScriptedGateway:
    """A gateway whose responses are scripted per call."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls = 0
        self.close_count = 0
        self.serial_number = "ZT999999999999A9999"

    async def read(self) -> Reading:
        self.calls += 1
        result = self.script.pop(0) if self.script else GatewayError("exhausted")
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]

    async def close(self) -> None:
        self.close_count += 1


def a_reading(solar: float = 3.0) -> Reading:
    return Reading.create(utcnow(), solar, 1.0, 1.0 - solar)


@pytest.fixture
async def poller_store(tmp_path):
    store = SqliteReadingStore(tmp_path / "poller.db")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


class TestBackoff:
    def test_backoff_doubles_and_is_capped(self, tmp_path) -> None:
        settings = make_settings(
            tmp_path, poll_backoff_min_seconds=5, poll_backoff_max_seconds=60
        )
        poller = Poller(
            store=SqliteReadingStore(settings.db_path),
            settings=settings,
            client=ScriptedGateway([]),
        )

        delays = []
        for _ in range(8):
            poller.status.consecutive_failures += 1
            delays.append(poller._backoff_delay())

        assert delays[:4] == [5.0, 10.0, 20.0, 40.0]
        assert all(delay == 60.0 for delay in delays[4:])

    def test_a_successful_poll_returns_to_the_normal_interval(
        self, poller_store
    ) -> None:
        settings = make_settings(poller_store.db_path.parent, poll_interval_seconds=45)
        poller = Poller(
            store=poller_store,
            settings=settings,
            client=ScriptedGateway([a_reading()]),
        )
        delay = asyncio.run(poller._poll_once())
        assert delay == 45.0


class TestResilience:
    async def test_the_loop_survives_a_failing_gateway(self, poller_store) -> None:
        settings = make_settings(
            poller_store.db_path.parent,
            poll_interval_seconds=1,
            poll_backoff_min_seconds=1,
            poll_backoff_max_seconds=1,
        )
        gateway = ScriptedGateway(
            [GatewayError("refused"), GatewayError("refused"), a_reading(4.0)]
        )
        poller = Poller(store=poller_store, settings=settings, client=gateway)

        await poller.start()
        # Two failures then a success, each 1s apart.
        await asyncio.sleep(2.6)
        await poller.stop()

        assert gateway.calls >= 3
        assert poller.status.success_count >= 1
        assert poller.status.failure_count >= 2
        # Recovery clears the failure streak and the error message.
        assert poller.status.consecutive_failures == 0
        assert poller.status.last_error is None
        assert (await poller_store.stats()).reading_count >= 1

    async def test_a_storage_failure_does_not_kill_the_loop(
        self, poller_store
    ) -> None:
        settings = make_settings(
            poller_store.db_path.parent,
            poll_interval_seconds=1,
            poll_backoff_min_seconds=1,
            poll_backoff_max_seconds=1,
        )

        class BrokenStore(SqliteReadingStore):
            async def insert_reading(self, reading: Reading) -> None:
                raise RuntimeError("disk full")

        broken = BrokenStore(poller_store.db_path)
        await broken.initialize()
        poller = Poller(
            store=broken,
            settings=settings,
            client=ScriptedGateway([a_reading(), a_reading(), a_reading()]),
        )

        await poller.start()
        await asyncio.sleep(1.6)
        running_mid_outage = poller.status.running
        await poller.stop()
        await broken.close()

        assert running_mid_outage is True
        assert poller.status.failure_count >= 1
        assert "disk full" in poller.status.last_error

    async def test_the_session_is_dropped_after_a_failure(
        self, poller_store
    ) -> None:
        """A failed poll closes the session so the next one re-authenticates."""
        settings = make_settings(poller_store.db_path.parent)
        gateway = ScriptedGateway([GatewayError("session expired")])
        poller = Poller(store=poller_store, settings=settings, client=gateway)

        await poller._poll_once()
        assert gateway.close_count == 1

    async def test_misconfiguration_backs_off_to_the_maximum(
        self, poller_store
    ) -> None:
        """A missing PVS_HOST will not fix itself; do not hammer it."""
        settings = make_settings(
            poller_store.db_path.parent, poll_backoff_max_seconds=300
        )
        gateway = ScriptedGateway([GatewayNotConfigured("PVS_HOST is not set")])
        poller = Poller(store=poller_store, settings=settings, client=gateway)

        delay = await poller._poll_once()
        assert delay == 300.0
        # No session to drop, so it is left alone.
        assert gateway.close_count == 0

    async def test_stop_is_idempotent_and_prompt(self, poller_store) -> None:
        settings = make_settings(poller_store.db_path.parent)
        poller = Poller(
            store=poller_store,
            settings=settings,
            client=ScriptedGateway([a_reading()]),
        )
        await poller.start()
        await poller.stop()
        await poller.stop()
        assert poller.status.running is False

    async def test_latest_reading_is_seeded_from_storage_on_restart(
        self, poller_store
    ) -> None:
        """After a service restart the home view shows the last known value."""
        settings = make_settings(poller_store.db_path.parent)
        stored = Reading.create(utcnow(), 7.5, 2.0, -5.5)
        await poller_store.insert_reading(stored)

        poller = Poller(
            store=poller_store,
            settings=settings,
            client=ScriptedGateway([GatewayError("down")]),
        )
        await poller.start()
        try:
            assert poller.latest_reading is not None
            assert poller.latest_reading.solar_kw == 7.5
        finally:
            await poller.stop()


class TestSerialMasking:
    @pytest.mark.parametrize(
        "serial,expected",
        [
            ("ZT999999999999A9999", "ZT999999999999*****"),
            ("ABCDE", "*****"),
            ("ABC", "***"),
            ("", ""),
            (None, None),
        ],
    )
    def test_the_password_characters_are_never_exposed(
        self, serial, expected
    ) -> None:
        assert mask_serial(serial) == expected

    def test_describe_masks_the_serial(self, settings: Settings) -> None:
        described = settings.describe()
        assert described["pvs_serial"] == "ZT999999999999*****"
        assert settings.pvs_password == "A9999"
        assert settings.pvs_password not in str(described)


class TestReadingNormalisation:
    """Guards for the miscalibrated net/consumption channel on this gateway.

    The PVS6 computes site_load_p as pv_p + net_p, so an over-reading net CT
    drives the reported house load negative. Real values seen from the gateway:
    pv_p 5.90, net_p -10.40, site_load_p -4.50 -- while SunStrong Connect showed
    "4.5 kW HOME USAGE", i.e. the magnitude of the same impossible number.
    """

    def test_negative_house_load_is_flagged_and_shown_as_magnitude(self) -> None:
        from app.pvs_client import _normalise

        solar, home, grid, implausible = _normalise(5.90, -4.50, -10.40, 1.0)

        assert implausible is True
        # Matches what the official app puts on screen.
        assert home == pytest.approx(4.50)
        assert solar == pytest.approx(5.90)
        assert grid == pytest.approx(-10.40)

    def test_grid_scale_corrects_the_net_channel_and_rederives_the_load(
        self,
    ) -> None:
        from app.pvs_client import _normalise

        solar, home, grid, implausible = _normalise(5.90, -4.50, -10.40, 0.5)

        assert grid == pytest.approx(-5.20)
        # Re-derived rather than reusing the gateway's own site_load_p, which
        # was computed from the uncorrected net power.
        assert home == pytest.approx(0.70)
        assert implausible is False

    def test_a_normal_reading_passes_through_untouched(self) -> None:
        from app.pvs_client import _normalise

        solar, home, grid, implausible = _normalise(6.0, 1.5, -4.5, 1.0)

        assert (solar, home, grid) == (6.0, 1.5, -4.5)
        assert implausible is False

    def test_meter_noise_just_below_zero_is_not_flagged(self) -> None:
        from app.pvs_client import _normalise

        _, home, _, implausible = _normalise(0.0, -0.01, -0.01, 1.0)

        assert implausible is False
        assert home == pytest.approx(-0.01)

    def test_lifetime_counters_survive_a_round_trip(self, poller_store) -> None:
        """Counters are the trustworthy record, so they must persist exactly."""
        import asyncio

        async def check() -> None:
            stored = Reading.create(
                utcnow(),
                5.9,
                4.5,
                -10.4,
                solar_kwh_lifetime=59567.3789,
                home_kwh_lifetime=77817.7813,
                grid_net_kwh_lifetime=18250.4004,
            )
            await poller_store.insert_reading(stored)
            got = await poller_store.latest_reading()
            assert got.solar_kwh_lifetime == pytest.approx(59567.3789)
            assert got.home_kwh_lifetime == pytest.approx(77817.7813)
            assert got.grid_net_kwh_lifetime == pytest.approx(18250.4004)

        asyncio.run(check())
