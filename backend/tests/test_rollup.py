"""Tests for the power-to-energy integration, which every kWh figure rests on."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import TEST_TZ

from app.models import EnergyTotals, HourlyRollup, Reading
from app.rollup import (
    build_window,
    combine_hourly,
    hour_edges_between,
    integrate_readings,
    sum_totals,
)


def constant_readings(
    start: datetime,
    count: int,
    interval_seconds: int = 60,
    solar_kw: float = 2.0,
    home_kw: float = 1.0,
    grid_kw: float = -1.0,
) -> list[Reading]:
    return [
        Reading.create(
            start + timedelta(seconds=interval_seconds * i),
            solar_kw,
            home_kw,
            grid_kw,
        )
        for i in range(count)
    ]


class TestIntegration:
    def test_constant_power_integrates_to_exact_energy(self) -> None:
        """2 kW held for a full hour is 2 kWh, not 1.97 or 2.03."""
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        # 61 samples spans exactly 60 minutes of intervals.
        readings = constant_readings(window.start, count=61)
        buckets = integrate_readings(readings, window.edges, 300)

        assert buckets[0].totals.solar_kwh == pytest.approx(2.0)
        assert buckets[0].totals.home_kwh == pytest.approx(1.0)
        assert buckets[0].totals.grid_export_kwh == pytest.approx(1.0)
        assert buckets[0].totals.grid_import_kwh == pytest.approx(0.0)
        assert buckets[0].covered_seconds == pytest.approx(3600.0)
        assert buckets[0].sample_count == 60  # the 61st lands in the next hour

    def test_energy_is_split_across_bucket_boundaries(self) -> None:
        """An interval straddling an edge contributes to both buckets."""
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        # One 60-minute interval centred on the 01:00 boundary.
        left = Reading.create(window.start + timedelta(minutes=30), 2.0, 0.0, -2.0)
        right = Reading.create(window.start + timedelta(minutes=90), 2.0, 0.0, -2.0)
        buckets = integrate_readings([left, right], window.edges, max_gap_seconds=3600)

        assert buckets[0].totals.solar_kwh == pytest.approx(1.0)
        assert buckets[1].totals.solar_kwh == pytest.approx(1.0)
        assert buckets[0].covered_seconds == pytest.approx(1800.0)
        assert buckets[1].covered_seconds == pytest.approx(1800.0)

    def test_gap_longer_than_max_contributes_no_energy(self) -> None:
        """An outage must not be integrated as hours of flat power."""
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        before = Reading.create(window.start + timedelta(minutes=5), 5.0, 1.0, -4.0)
        after = Reading.create(window.start + timedelta(hours=6), 5.0, 1.0, -4.0)
        buckets = integrate_readings([before, after], window.edges, max_gap_seconds=300)

        totals = sum_totals(buckets)
        assert totals.solar_kwh == 0.0
        assert totals.home_kwh == 0.0
        assert sum(b.covered_seconds for b in buckets) == 0.0
        # The samples themselves are still counted where they landed.
        assert buckets[0].sample_count == 1
        assert buckets[6].sample_count == 1

    def test_trapezoid_uses_the_mean_of_both_endpoints(self) -> None:
        """Ramping 0 -> 4 kW over an hour is 2 kWh."""
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        readings = [
            Reading.create(window.start, 0.0, 0.0, 0.0),
            Reading.create(window.start + timedelta(hours=1), 4.0, 0.0, -4.0),
        ]
        buckets = integrate_readings(readings, window.edges, max_gap_seconds=3600)
        assert buckets[0].totals.solar_kwh == pytest.approx(2.0)

    def test_import_and_export_do_not_cancel_across_zero(self) -> None:
        """An interval crossing zero yields both import and export energy."""
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        readings = [
            Reading.create(window.start, 0.0, 2.0, 2.0),  # importing 2 kW
            Reading.create(window.start + timedelta(hours=1), 4.0, 2.0, -2.0),
        ]
        buckets = integrate_readings(readings, window.edges, max_gap_seconds=3600)
        # Rectified trapezoid: each side averages 1 kW over the hour.
        assert buckets[0].totals.grid_import_kwh == pytest.approx(1.0)
        assert buckets[0].totals.grid_export_kwh == pytest.approx(1.0)

    def test_lead_sample_completes_the_first_bucket(self) -> None:
        """The sample before the window is what makes hour 0 whole.

        Without it, the minute between midnight and the first stored sample is
        uncovered and its energy is lost.
        """
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        lead = Reading.create(window.start - timedelta(minutes=1), 2.0, 0.0, -2.0)
        inside = constant_readings(
            window.start + timedelta(minutes=1), count=2, solar_kw=2.0, home_kw=0.0
        )

        without = integrate_readings(inside, window.edges, 300)
        with_lead = integrate_readings([lead, *inside], window.edges, 300)

        # Only the in-window half of the straddling interval is counted.
        assert without[0].covered_seconds == pytest.approx(60.0)
        assert with_lead[0].covered_seconds == pytest.approx(120.0)
        # Bucket totals are stored rounded to 4 decimal places.
        assert with_lead[0].totals.solar_kwh == pytest.approx(
            2.0 * 120 / 3600, abs=1e-4
        )

    def test_empty_input_yields_empty_buckets_not_an_error(self) -> None:
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        buckets = integrate_readings([], window.edges, 300)
        assert len(buckets) == 24
        assert all(b.sample_count == 0 for b in buckets)
        assert sum_totals(buckets).solar_kwh == 0.0

    def test_duplicate_timestamps_are_ignored(self) -> None:
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        same = [
            Reading.create(window.start, 3.0, 1.0, -2.0),
            Reading.create(window.start, 3.0, 1.0, -2.0),
        ]
        buckets = integrate_readings(same, window.edges, 300)
        assert buckets[0].totals.solar_kwh == 0.0


class TestWindows:
    @pytest.mark.parametrize(
        "kind,anchor,expected_buckets",
        [
            ("day", date(2026, 9, 27), 24),
            ("week", date(2026, 9, 27), 7),
            ("month", date(2026, 9, 27), 30),
            ("month", date(2028, 2, 10), 29),  # leap February
            ("year", date(2026, 5, 5), 12),
        ],
    )
    def test_bucket_counts(self, kind, anchor, expected_buckets) -> None:
        window = build_window(kind, anchor, TEST_TZ)
        assert len(window.edges) - 1 == expected_buckets

    def test_week_starts_on_monday(self) -> None:
        # 2026-09-27 is a Sunday; its ISO week starts Monday the 21st.
        window = build_window("week", date(2026, 9, 27), TEST_TZ)
        assert window.start.date() == date(2026, 9, 21)
        assert window.end.date() == date(2026, 9, 28)

    def test_navigation_anchors_land_in_adjacent_windows(self) -> None:
        window = build_window("month", date(2026, 1, 15), TEST_TZ)
        assert window.previous_anchor == date(2025, 12, 31)
        assert window.next_anchor == date(2026, 2, 1)

    def test_dst_spring_forward_day_has_23_hours(self) -> None:
        """A real IANA zone, so the DST-aware edge walk is actually exercised."""
        zone = pytest.importorskip("zoneinfo").ZoneInfo("America/Los_Angeles")
        window = build_window("day", date(2026, 3, 8), zone)
        assert len(window.edges) - 1 == 23

    def test_dst_fall_back_day_has_25_hours(self) -> None:
        zone = pytest.importorskip("zoneinfo").ZoneInfo("America/Los_Angeles")
        window = build_window("day", date(2026, 11, 1), zone)
        assert len(window.edges) - 1 == 25


class TestCombineHourly:
    def test_hours_sum_into_days(self) -> None:
        window = build_window("week", date(2026, 9, 21), TEST_TZ)
        # 3 kWh of solar every hour of the first day.
        rollups = [
            HourlyRollup(
                hour_start=window.start + timedelta(hours=hour),
                totals=EnergyTotals(3.0, 1.0, 0.25, 2.25),
                sample_count=60,
                covered_seconds=3600.0,
            )
            for hour in range(24)
        ]
        buckets = combine_hourly(rollups, window.edges)

        assert buckets[0].totals.solar_kwh == pytest.approx(72.0)
        assert buckets[0].sample_count == 1440
        assert all(b.totals.solar_kwh == 0.0 for b in buckets[1:])

    def test_hours_outside_the_window_are_dropped(self) -> None:
        window = build_window("day", date(2026, 9, 27), TEST_TZ)
        outside = HourlyRollup(
            hour_start=window.start - timedelta(hours=5),
            totals=EnergyTotals(9.0, 9.0, 9.0, 9.0),
            sample_count=60,
            covered_seconds=3600.0,
        )
        buckets = combine_hourly([outside], window.edges)
        assert sum_totals(buckets).solar_kwh == 0.0

    def test_missing_hours_read_as_zero_not_as_an_error(self) -> None:
        window = build_window("month", date(2026, 9, 1), TEST_TZ)
        buckets = combine_hourly([], window.edges)
        assert len(buckets) == 30
        assert all(b.sample_count == 0 for b in buckets)


class TestHourEdges:
    def test_edges_align_down_to_the_hour(self) -> None:
        start = datetime(2026, 9, 27, 10, 37, 12, tzinfo=timezone.utc)
        end = datetime(2026, 9, 27, 13, 5, tzinfo=timezone.utc)
        edges = hour_edges_between(start, end)
        assert edges[0] == datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
        assert edges[-1] == datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)
        assert len(edges) == 5


class TestTotals:
    def test_self_consumption_and_sufficiency(self) -> None:
        totals = EnergyTotals(
            solar_kwh=30.0, home_kwh=20.0, grid_import_kwh=4.0, grid_export_kwh=14.0
        )
        assert totals.self_consumption_pct == pytest.approx(53.3)
        assert totals.self_sufficiency_pct == pytest.approx(80.0)

    def test_percentages_are_none_without_a_denominator(self) -> None:
        empty = EnergyTotals()
        assert empty.self_consumption_pct is None
        assert empty.self_sufficiency_pct is None
