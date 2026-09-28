"""Tests for importing daily history from SunPower monthly reports.

The reports are the only source of history from before the poller existed, and
they carry the same miscalibrated consumption channel the live gateway does --
printing negative household use on heavy-export days. Importing those as fact
would put impossible numbers into the charts permanently.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import TEST_TZ

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.models import DailyImport  # noqa: E402
from app.rollup import build_window, combine_hourly, merge_daily_imports, sum_totals  # noqa: E402
from import_reports import parse_number  # noqa: E402


class TestParseNumber:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("26.18", 26.18),
            ("1,203.96", 1203.96),
            # Accounting parentheses mean negative in these reports.
            ("(19.42)", -19.42),
            ("(1,203.96)", -1203.96),
            ("N/A", None),
            ("", None),
            ("  43.04  ", 43.04),
            # Max AC Power is a bare hyphen on days with no production. Missing
            # this dropped three whole days from the 2026 import.
            ("-", None),
            ("--", None),
        ],
    )
    def test_report_cell_formats(self, raw, expected) -> None:
        assert parse_number(raw) == expected

    def test_a_row_with_a_hyphen_cell_still_parses(self) -> None:
        """Regression: `Mar 22, 2026 | 0.01 | 8.86 | -` must not be skipped."""
        from import_reports import _ROW

        text = "\n".join(
            [
                "Mar 22, 2026",
                "0.01",
                "8.86",
                "-",
                "Mar 23, 2026",
                "64.16",
                "11.80",
                "6.53",
                "",
            ]
        )
        rows = _ROW.findall(text)
        assert len(rows) == 2
        assert rows[0][0] == "Mar 22, 2026"
        assert parse_number(rows[0][3]) is None


class TestMergeDailyImports:
    def _empty_month(self, anchor: date):
        window = build_window("month", anchor, TEST_TZ)
        return window, combine_hourly([], window.edges)

    def test_imports_fill_days_the_poller_never_saw(self) -> None:
        window, buckets = self._empty_month(date(2026, 8, 15))
        imports = [
            DailyImport(date(2026, 8, day), 30.0, 60.0) for day in range(1, 32)
        ]

        merged = merge_daily_imports(buckets, imports)

        assert all(imported for _, imported in merged)
        totals = sum_totals([bucket for bucket, _ in merged])
        assert totals.solar_kwh == pytest.approx(31 * 30.0)
        assert totals.home_kwh == pytest.approx(31 * 60.0)

    def test_measured_data_always_wins(self) -> None:
        """A day the poller saw keeps its own record, import or not."""
        window = build_window("month", date(2026, 8, 15), TEST_TZ)
        from app.models import EnergyTotals, HourlyRollup

        # One real hour on 2026-08-02.
        measured = [
            HourlyRollup(
                hour_start=window.edges[1].astimezone(),
                totals=EnergyTotals(5.0, 2.0, 0.5, 1.0),
                sample_count=60,
                covered_seconds=3600.0,
            )
        ]
        buckets = combine_hourly(measured, window.edges)
        imports = [
            DailyImport(date(2026, 8, day), 99.0, 99.0) for day in range(1, 32)
        ]

        merged = merge_daily_imports(buckets, imports)
        measured_buckets = [b for b, imported in merged if not imported]

        # The day with real coverage was not overwritten by the 99.0 import.
        assert any(b.totals.solar_kwh == pytest.approx(5.0) for b in measured_buckets)

    def test_unknown_usage_is_not_counted_as_zero(self) -> None:
        """Days whose report usage was impossible contribute solar only."""
        window, buckets = self._empty_month(date(2026, 5, 15))
        imports = [
            DailyImport(date(2026, 5, day), 40.0, None if day <= 20 else 50.0)
            for day in range(1, 32)
        ]

        merged = merge_daily_imports(buckets, imports)
        totals = sum_totals([bucket for bucket, _ in merged])

        assert totals.solar_kwh == pytest.approx(31 * 40.0)
        # Only the 11 days with a usable figure.
        assert totals.home_kwh == pytest.approx(11 * 50.0)

    def test_grid_split_is_left_at_zero_not_invented(self) -> None:
        """The reports give totals, not a directional split."""
        window, buckets = self._empty_month(date(2026, 8, 15))
        imports = [DailyImport(date(2026, 8, 1), 30.0, 60.0)]

        merged = merge_daily_imports(buckets, imports)
        totals = sum_totals([bucket for bucket, _ in merged])

        assert totals.grid_import_kwh == 0.0
        assert totals.grid_export_kwh == 0.0

    def test_a_month_bucket_gathers_all_its_days(self) -> None:
        """In the year view one bucket is a whole month of imported days."""
        window = build_window("year", date(2026, 6, 1), TEST_TZ)
        buckets = combine_hourly([], window.edges)
        imports = [
            DailyImport(date(2026, 8, day), 10.0, 20.0) for day in range(1, 32)
        ]

        merged = merge_daily_imports(buckets, imports)
        august = merged[7]  # index 7 == August

        assert august[1] is True
        assert august[0].totals.solar_kwh == pytest.approx(310.0)

    def test_no_imports_changes_nothing(self) -> None:
        window, buckets = self._empty_month(date(2026, 8, 15))
        merged = merge_daily_imports(buckets, [])
        assert [b for b, _ in merged] == buckets
        assert not any(imported for _, imported in merged)


class TestImportedHistoryApi:
    async def test_imported_days_surface_through_the_store(self, store) -> None:
        rows = [
            DailyImport(date(2026, 8, day), 30.0, 60.0, 6.5) for day in range(1, 32)
        ]
        assert await store.upsert_daily_imports(rows) == 31

        got = await store.query_daily_imports(date(2026, 8, 1), date(2026, 9, 1))
        assert len(got) == 31
        assert got[0].source == "sunpower-monthly-report"

        # Re-importing the same month replaces rather than duplicates.
        await store.upsert_daily_imports(rows[:5])
        assert len(await store.query_daily_imports(date(2026, 8, 1), date(2026, 9, 1))) == 31

    async def test_the_range_is_half_open(self, store) -> None:
        await store.upsert_daily_imports(
            [
                DailyImport(date(2026, 8, 31), 1.0, 2.0),
                DailyImport(date(2026, 9, 1), 3.0, 4.0),
            ]
        )
        got = await store.query_daily_imports(date(2026, 8, 1), date(2026, 9, 1))
        assert [row.day for row in got] == [date(2026, 8, 31)]
