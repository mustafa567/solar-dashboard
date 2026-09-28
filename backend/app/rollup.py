"""Turning power samples into energy buckets.

The gateway only reports instantaneous power, so all kWh figures in this app are
integrated from the stored samples. Two entry points share the same bucketing
rules:

``integrate_readings``
    Exact integration straight from raw samples. Used for the day view and by
    the rollup job. Trapezoidal between consecutive samples, and any interval
    longer than ``max_gap_seconds`` is treated as an outage that contributes no
    energy -- otherwise a service restart would be integrated as hours of flat
    power.

``combine_hourly``
    Sums already-computed hourly rollups into coarser buckets. Used for the
    week / month / year views so those never touch raw samples.

Bucket edges are computed in the configured local timezone, because "today" and
"this month" are local concepts, while every stored timestamp is UTC.
"""

from __future__ import annotations

import calendar
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Literal, Sequence

from .models import (
    Bucket,
    DailyImport,
    EnergyTotals,
    HourlyRollup,
    Reading,
    to_utc,
)

RangeKind = Literal["day", "week", "month", "year"]
BucketKind = Literal["hour", "day", "month"]

#: Which bucket granularity each range renders at.
BUCKET_FOR_RANGE: dict[RangeKind, BucketKind] = {
    "day": "hour",
    "week": "day",
    "month": "day",
    "year": "month",
}


@dataclass(frozen=True, slots=True)
class Window:
    """A local-time window plus the bucket edges that subdivide it."""

    range_kind: RangeKind
    bucket_kind: BucketKind
    start: datetime
    end: datetime
    #: Local-time bucket edges, length = len(buckets) + 1.
    edges: tuple[datetime, ...]
    label: str

    @property
    def previous_anchor(self) -> date:
        """A date inside the preceding window, for prev/next navigation."""
        return (self.start - timedelta(days=1)).date()

    @property
    def next_anchor(self) -> date:
        """A date inside the following window."""
        return self.end.date()


# -- window construction ---------------------------------------------------


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def build_window(range_kind: RangeKind, anchor: date, tz: tzinfo) -> Window:
    """Build the window and bucket edges for a range selector + anchor date.

    Weeks run Monday to Sunday (ISO), which is what ``date.weekday()`` gives.
    """
    if range_kind == "day":
        start = _local_midnight(anchor, tz)
        end = start + timedelta(days=1)
        edges = _hour_edges(start, end)
        label = anchor.isoformat()
    elif range_kind == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        start = _local_midnight(monday, tz)
        end = start + timedelta(days=7)
        edges = _day_edges(start, 7, tz)
        label = f"Week of {monday.isoformat()}"
    elif range_kind == "month":
        start = _local_midnight(anchor.replace(day=1), tz)
        days_in_month = calendar.monthrange(anchor.year, anchor.month)[1]
        end = _local_midnight(
            (anchor.replace(day=1) + timedelta(days=days_in_month)), tz
        )
        edges = _day_edges(start, days_in_month, tz)
        label = start.strftime("%B %Y")
    elif range_kind == "year":
        start = _local_midnight(date(anchor.year, 1, 1), tz)
        end = _local_midnight(date(anchor.year + 1, 1, 1), tz)
        edges = tuple(
            _local_midnight(date(anchor.year, month, 1), tz) for month in range(1, 13)
        ) + (end,)
        label = str(anchor.year)
    else:  # pragma: no cover - guarded by the API layer
        raise ValueError(f"Unknown range: {range_kind!r}")

    return Window(
        range_kind=range_kind,
        bucket_kind=BUCKET_FOR_RANGE[range_kind],
        start=start,
        end=end,
        edges=edges,
        label=label,
    )


def _hour_edges(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """Hour edges across a local day.

    Built by walking UTC instants so a DST transition yields 23 or 25 buckets
    instead of silently double-counting an hour.
    """
    edges = [start]
    cursor_utc = to_utc(start)
    end_utc = to_utc(end)
    while cursor_utc < end_utc:
        cursor_utc += timedelta(hours=1)
        edges.append(min(cursor_utc, end_utc).astimezone(start.tzinfo))
    return tuple(edges)


def _day_edges(start: datetime, count: int, tz: tzinfo) -> tuple[datetime, ...]:
    """Local midnight edges for ``count`` consecutive days."""
    first_day = start.date()
    edges = [
        _local_midnight(first_day + timedelta(days=offset), tz)
        for offset in range(count + 1)
    ]
    return tuple(edges)


# -- integration from raw samples ------------------------------------------


@dataclass
class _Accumulator:
    """Mutable per-bucket running total."""

    solar_kwh: float = 0.0
    home_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    sample_count: int = 0
    covered_seconds: float = 0.0

    def totals(self) -> EnergyTotals:
        return EnergyTotals(
            solar_kwh=round(self.solar_kwh, 4),
            home_kwh=round(self.home_kwh, 4),
            grid_import_kwh=round(self.grid_import_kwh, 4),
            grid_export_kwh=round(self.grid_export_kwh, 4),
        )


def integrate_readings(
    readings: Sequence[Reading],
    edges: Sequence[datetime],
    max_gap_seconds: float,
) -> list[Bucket]:
    """Integrate power samples into the buckets defined by ``edges``.

    ``readings`` must be sorted oldest-first and may extend outside the window;
    only the overlapping portion of each interval is counted, so passing the
    sample immediately before the window makes the first bucket complete.
    """
    if len(edges) < 2:
        return []

    edge_epochs = [to_utc(edge).timestamp() for edge in edges]
    accumulators = [_Accumulator() for _ in range(len(edges) - 1)]
    window_start, window_end = edge_epochs[0], edge_epochs[-1]

    previous: Reading | None = None
    for reading in readings:
        moment = to_utc(reading.timestamp).timestamp()

        # Count the sample itself against the bucket it lands in.
        if window_start <= moment < window_end:
            index = bisect_right(edge_epochs, moment) - 1
            if 0 <= index < len(accumulators):
                accumulators[index].sample_count += 1

        if previous is not None:
            _spread_interval(
                previous,
                reading,
                edge_epochs,
                accumulators,
                max_gap_seconds,
            )
        previous = reading

    return [
        Bucket(
            start=edges[index],
            end=edges[index + 1],
            totals=acc.totals(),
            sample_count=acc.sample_count,
            covered_seconds=round(acc.covered_seconds, 3),
        )
        for index, acc in enumerate(accumulators)
    ]


def _spread_interval(
    left: Reading,
    right: Reading,
    edge_epochs: Sequence[float],
    accumulators: list[_Accumulator],
    max_gap_seconds: float,
) -> None:
    """Allocate one sample interval's energy across the buckets it overlaps."""
    start = to_utc(left.timestamp).timestamp()
    end = to_utc(right.timestamp).timestamp()
    span = end - start
    if span <= 0 or span > max_gap_seconds:
        # Zero/negative span is a duplicate; an over-long span is an outage.
        return

    # Trapezoidal mean power for each channel. Import and export are averaged
    # from their rectified endpoints so an interval that crosses zero still
    # splits sensibly instead of cancelling out.
    mean_solar = (left.solar_kw + right.solar_kw) / 2.0
    mean_home = (left.home_kw + right.home_kw) / 2.0
    mean_import = (left.grid_import_kw + right.grid_import_kw) / 2.0
    mean_export = (left.grid_export_kw + right.grid_export_kw) / 2.0

    first = max(0, bisect_right(edge_epochs, start) - 1)
    for index in range(first, len(accumulators)):
        bucket_start = edge_epochs[index]
        bucket_end = edge_epochs[index + 1]
        if bucket_start >= end:
            break
        overlap = min(end, bucket_end) - max(start, bucket_start)
        if overlap <= 0:
            continue
        hours = overlap / 3600.0
        acc = accumulators[index]
        acc.solar_kwh += mean_solar * hours
        acc.home_kwh += mean_home * hours
        acc.grid_import_kwh += mean_import * hours
        acc.grid_export_kwh += mean_export * hours
        acc.covered_seconds += overlap


# -- aggregation from stored hourly rollups --------------------------------


def combine_hourly(
    rollups: Sequence[HourlyRollup],
    edges: Sequence[datetime],
) -> list[Bucket]:
    """Sum hourly rollups into the coarser buckets defined by ``edges``.

    An hour is attributed to the bucket containing its midpoint. Every timezone
    with a whole-hour UTC offset -- all of North America included -- has hour
    boundaries that line up exactly with local day boundaries, so this is exact
    there; in half-hour-offset zones a bucket edge can misplace a single hour.
    """
    if len(edges) < 2:
        return []

    edge_epochs = [to_utc(edge).timestamp() for edge in edges]
    accumulators = [_Accumulator() for _ in range(len(edges) - 1)]

    for rollup in rollups:
        midpoint = to_utc(rollup.hour_start).timestamp() + 1800.0
        if midpoint < edge_epochs[0] or midpoint >= edge_epochs[-1]:
            continue
        index = bisect_right(edge_epochs, midpoint) - 1
        if not 0 <= index < len(accumulators):
            continue
        acc = accumulators[index]
        acc.solar_kwh += rollup.totals.solar_kwh
        acc.home_kwh += rollup.totals.home_kwh
        acc.grid_import_kwh += rollup.totals.grid_import_kwh
        acc.grid_export_kwh += rollup.totals.grid_export_kwh
        acc.sample_count += rollup.sample_count
        acc.covered_seconds += rollup.covered_seconds

    return [
        Bucket(
            start=edges[index],
            end=edges[index + 1],
            totals=acc.totals(),
            sample_count=acc.sample_count,
            covered_seconds=round(acc.covered_seconds, 3),
        )
        for index, acc in enumerate(accumulators)
    ]


def sum_totals(buckets: Sequence[Bucket]) -> EnergyTotals:
    """Add up bucket totals into a single period total."""
    return EnergyTotals(
        solar_kwh=round(sum(b.totals.solar_kwh for b in buckets), 3),
        home_kwh=round(sum(b.totals.home_kwh for b in buckets), 3),
        grid_import_kwh=round(sum(b.totals.grid_import_kwh for b in buckets), 3),
        grid_export_kwh=round(sum(b.totals.grid_export_kwh for b in buckets), 3),
    )


def hour_edges_between(start: datetime, end: datetime) -> list[datetime]:
    """UTC hour edges covering [start, end), aligned down to the hour."""
    cursor = to_utc(start).replace(minute=0, second=0, microsecond=0)
    limit = to_utc(end)
    edges = []
    while cursor < limit:
        edges.append(cursor)
        cursor += timedelta(hours=1)
    edges.append(cursor)
    return edges


def merge_daily_imports(
    buckets: list[Bucket],
    imports: Sequence[DailyImport],
) -> list[tuple[Bucket, bool]]:
    """Fill buckets the poller never covered with imported daily totals.

    Returns each bucket paired with whether it came from an import, so the API
    can label it and the UI can say where a number came from.

    Measured data always wins: a bucket with any real coverage is left alone,
    because a day the poller partly saw is still its own record. Imported days
    only fill genuine holes -- which, for this site, is everything before the
    poller existed.

    Imported home usage may be None (the reports print impossible values on some
    days); those buckets get solar only, and the missing consumption stays
    missing rather than being invented as zero.
    """
    if not imports:
        return [(bucket, False) for bucket in buckets]

    by_day: dict[date, DailyImport] = {row.day: row for row in imports}
    merged: list[tuple[Bucket, bool]] = []

    for bucket in buckets:
        if bucket.covered_seconds > 0 or bucket.sample_count > 0:
            merged.append((bucket, False))
            continue

        # A bucket spanning more than a day (a month bucket in the year view)
        # takes every imported day that starts inside it.
        days = [
            row
            for day, row in by_day.items()
            if bucket.start.date() <= day < bucket.end.date()
        ]
        if not days:
            merged.append((bucket, False))
            continue

        solar = sum(row.solar_kwh or 0.0 for row in days)
        home_values = [row.home_kwh for row in days if row.home_kwh is not None]
        home = sum(home_values) if home_values else 0.0

        # The reports give totals, not a directional split, so grid import and
        # export are left at zero rather than guessed at.
        merged.append(
            (
                Bucket(
                    start=bucket.start,
                    end=bucket.end,
                    totals=EnergyTotals(
                        solar_kwh=round(solar, 3),
                        home_kwh=round(home, 3),
                        grid_import_kwh=0.0,
                        grid_export_kwh=0.0,
                    ),
                    sample_count=0,
                    covered_seconds=0.0,
                ),
                True,
            )
        )

    return merged
