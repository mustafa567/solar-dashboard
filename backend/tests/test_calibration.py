"""GRID_SCALE correction of live readings, stored history and report imports.

Real values from this gateway are used throughout: pv_p 5.11, net_p -8.31,
which the dashboard showed as "3.20 kW home, 8.31 kW exporting". At the
measured scale of 0.5 that is 0.955 kW home and 4.155 kW exporting.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.calibration import (
    corrected_daily_home,
    normalise,
    rescale_daily_import,
    rescale_reading,
)
from app.models import DailyImport, Reading
from app.sqlite_store import SqliteReadingStore

MOMENT = datetime(2026, 9, 28, 21, 13, tzinfo=timezone.utc)


def _as_stored(solar: float, net: float, scale: float) -> Reading:
    """A reading exactly as the live client would have stored it."""
    solar, home, grid, _ = normalise(solar, solar + net, net, scale)
    return Reading.create(MOMENT, solar, home, grid, grid_scale=scale)


class TestRescaleReading:
    def test_uncorrected_history_is_corrected_exactly(self) -> None:
        stored = _as_stored(5.11, -8.31, 1.0)
        assert stored.home_kw == pytest.approx(3.20)  # the impossible magnitude

        fixed = rescale_reading(stored, 0.5)

        assert fixed.grid_kw == pytest.approx(-4.155)
        # Re-derived from solar + grid, not from the stored magnitude.
        assert fixed.home_kw == pytest.approx(0.955)
        assert fixed.solar_kw == pytest.approx(5.11)
        assert fixed.grid_direction == "export"
        assert fixed.grid_scale == 0.5

    def test_matches_what_the_live_client_would_have_stored(self) -> None:
        assert rescale_reading(_as_stored(5.11, -8.31, 1.0), 0.5) == _as_stored(
            5.11, -8.31, 0.5
        )

    def test_rescaling_is_reversible(self) -> None:
        original = _as_stored(2.70, 2.82, 1.0)
        there_and_back = rescale_reading(rescale_reading(original, 0.5), 1.0)
        assert there_and_back.grid_kw == pytest.approx(original.grid_kw)
        assert there_and_back.home_kw == pytest.approx(original.home_kw)

    def test_a_reading_already_at_the_target_is_returned_unchanged(self) -> None:
        stored = _as_stored(5.11, -8.31, 0.5)
        assert rescale_reading(stored, 0.5) is stored

    def test_direction_can_flip_to_idle(self) -> None:
        # 0.03 kW import is above the idle threshold; halved, it is below it.
        stored = _as_stored(0.0, 0.03, 1.0)
        assert stored.grid_direction == "import"
        assert rescale_reading(stored, 0.5).grid_direction == "idle"

    def test_lifetime_counters_are_left_alone(self) -> None:
        stored = Reading.create(
            MOMENT, 5.11, 3.20, -8.31,
            solar_kwh_lifetime=59590.5, home_kwh_lifetime=77819.3,
            grid_net_kwh_lifetime=18228.8,
        )
        fixed = rescale_reading(stored, 0.5)
        assert (
            fixed.solar_kwh_lifetime,
            fixed.home_kwh_lifetime,
            fixed.grid_net_kwh_lifetime,
        ) == (59590.5, 77819.3, 18228.8)


class TestCorrectedDailyHome:
    def test_a_negative_printed_day_becomes_usable(self) -> None:
        # Produced 40, "used" (19.42) -> net was -59.42 at 2x -> -29.71.
        assert corrected_daily_home(40.0, -19.42, 0.5) == pytest.approx(10.29)

    def test_a_positive_day_is_corrected_too(self) -> None:
        assert corrected_daily_home(30.0, 60.0, 0.5) == pytest.approx(45.0)

    def test_unscaled_is_the_printed_value(self) -> None:
        assert corrected_daily_home(30.0, 60.0, 1.0) == 60.0

    def test_still_impossible_after_correction_is_unknown(self) -> None:
        assert corrected_daily_home(10.0, -19.42, 0.5) is None
        assert corrected_daily_home(40.0, -19.42, 1.0) is None

    def test_missing_inputs_are_unknown_not_zero(self) -> None:
        assert corrected_daily_home(40.0, None, 0.5) is None
        # Without production the net part cannot be separated out to correct.
        assert corrected_daily_home(None, 60.0, 0.5) is None

    def test_rescale_daily_import_uses_the_printed_value(self) -> None:
        row = DailyImport(
            date(2026, 3, 2), 43.47, None, 6.31,
            home_kwh_reported=-5.0, grid_scale=1.0,
        )
        fixed = rescale_daily_import(row, 0.5)
        assert fixed.home_kwh == pytest.approx(19.235)
        assert fixed.home_kwh_reported == -5.0
        assert fixed.grid_scale == 0.5


class TestStorage:
    async def test_grid_scale_round_trips(self, store) -> None:
        await store.insert_reading(_as_stored(5.11, -8.31, 0.5))
        got = await store.latest_reading()
        assert got is not None
        assert got.grid_scale == 0.5

        await store.upsert_daily_imports(
            [DailyImport(date(2026, 3, 2), 43.47, 19.235, 6.31, home_kwh_reported=-5.0, grid_scale=0.5)]
        )
        (row,) = await store.query_daily_imports(date(2026, 3, 1), date(2026, 4, 1))
        assert (row.home_kwh_reported, row.grid_scale) == (-5.0, 0.5)

    async def test_a_v4_database_migrates_without_losing_anything(
        self, tmp_path
    ) -> None:
        path = tmp_path / "v4.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE readings (
                ts INTEGER PRIMARY KEY, solar_kw REAL NOT NULL,
                home_kw REAL NOT NULL, grid_kw REAL NOT NULL,
                grid_direction TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'livedata',
                solar_kwh_lifetime REAL, home_kwh_lifetime REAL,
                grid_net_kwh_lifetime REAL);
            CREATE TABLE daily_import (
                day TEXT PRIMARY KEY, solar_kwh REAL, home_kwh REAL,
                max_ac_kw REAL, source TEXT NOT NULL,
                imported_at INTEGER NOT NULL);
            INSERT INTO readings VALUES
                (1790630000, 5.11, 3.2, -8.31, 'export', 'livedata', 1, 2, 3);
            INSERT INTO daily_import VALUES
                ('2026-03-01', 40.0, 60.0, 6.0, 'sunpower-monthly-report', 0),
                ('2026-03-02', 43.47, NULL, 6.31, 'sunpower-monthly-report', 0);
            PRAGMA user_version = 4;
            """
        )
        conn.close()

        store = SqliteReadingStore(path)
        await store.initialize()
        try:
            reading = await store.latest_reading()
            assert reading is not None
            assert reading.grid_scale == 1.0  # every pre-v5 row was uncorrected
            assert reading.grid_kw == -8.31

            rows = await store.query_daily_imports(date(2026, 3, 1), date(2026, 4, 1))
            # The printed value is recovered where it was kept...
            assert rows[0].home_kwh_reported == 60.0
            # ...and stays unknown where v4 had already dropped it.
            assert rows[1].home_kwh_reported is None
            assert all(row.grid_scale == 1.0 for row in rows)
        finally:
            await store.close()
