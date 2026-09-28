"""Keeps the hourly rollup table in step with the raw readings.

Raw samples remain the source of truth. This job integrates them into hourly
kWh rows so the week / month / year views read a few hundred rows instead of a
year's worth of samples (a year at 60s polling is roughly 525,000 rows).

It is incremental and idempotent: each run recomputes the most recent few hours
plus anything newer, so a partially-complete hour is corrected once it fills in,
and the whole table can be deleted and rebuilt from scratch at any time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import Settings
from .models import HourlyRollup, utcnow
from .rollup import hour_edges_between, integrate_readings
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)

#: Recompute this many already-rolled-up hours each run. Covers the hour that
#: was still partial last time plus a margin for late writes.
REFRESH_OVERLAP_HOURS = 3

#: Hours processed per database round trip when backfilling.
CHUNK_HOURS = 24 * 7


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


async def refresh_hourly(
    store: ReadingStore,
    settings: Settings,
    full: bool = False,
) -> int:
    """Recompute hourly rollups. Returns the number of hour rows written.

    Pass ``full=True`` to rebuild every hour from the first stored reading --
    useful after changing ``MAX_SAMPLE_GAP_SECONDS`` or importing history.
    """
    stats = await store.stats()
    if stats.first_timestamp is None or stats.last_timestamp is None:
        return 0

    first_hour = floor_hour(stats.first_timestamp)
    latest_rolled = None if full else await store.latest_hourly_hour()
    if latest_rolled is None:
        start = first_hour
    else:
        start = max(
            first_hour, latest_rolled - timedelta(hours=REFRESH_OVERLAP_HOURS)
        )

    # Include the hour in progress so "this month" totals stay current; it gets
    # recomputed on the next run once it is complete.
    end = floor_hour(max(stats.last_timestamp, utcnow())) + timedelta(hours=1)
    if start >= end:
        return 0

    written = 0
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(hours=CHUNK_HOURS), end)
        written += await _refresh_chunk(store, settings, cursor, chunk_end)
        cursor = chunk_end

    if written:
        _LOGGER.debug(
            "Hourly rollup refreshed: %s hour(s) from %s to %s",
            written,
            start.isoformat(),
            end.isoformat(),
        )
    return written


async def _refresh_chunk(
    store: ReadingStore,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> int:
    edges = hour_edges_between(start, end)
    readings = await store.query_range(start, end)

    # Pull in the sample just before the window so the first hour's opening
    # interval is integrated rather than dropped.
    lead = await store.reading_before(start)
    if lead is not None:
        readings = [lead, *readings]

    if not readings:
        return 0

    buckets = integrate_readings(readings, edges, settings.max_sample_gap_seconds)
    rows = [
        HourlyRollup(
            hour_start=bucket.start,
            totals=bucket.totals,
            sample_count=bucket.sample_count,
            covered_seconds=bucket.covered_seconds,
        )
        for bucket in buckets
        # Skip hours the poller never observed, so the table stays sparse across
        # outages and a missing row reads as "no data" rather than "zero kWh".
        if bucket.sample_count > 0 or bucket.covered_seconds > 0
    ]
    return await store.upsert_hourly(rows)
