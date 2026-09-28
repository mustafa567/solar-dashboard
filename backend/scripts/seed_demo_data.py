"""Fill a database with plausible synthetic readings.

For working on the UI without a gateway, and for checking that the history views
behave at realistic data volumes. It writes to a *separate* database file by
default so it can never touch real history.

    # 70 days of 60-second samples into data/demo.db
    .venv/Scripts/python.exe backend/scripts/seed_demo_data.py

    # then point the backend at it
    set DB_PATH=data/demo.db
    .venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend

The shape is deliberately not a clean sine wave: there are cloudy days, evening
cooking peaks and appliance spikes, so the charts exercise the same edge cases
real data does (export at midday, import after dark, partial days).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.models import Reading  # noqa: E402
from app.rollup_job import refresh_hourly  # noqa: E402
from app.sqlite_store import SqliteReadingStore  # noqa: E402

SUNRISE_HOUR = 6.4
SUNSET_HOUR = 19.1
PEAK_KW = 7.2
BASELINE_HOME_KW = 0.38


def solar_kw(local: datetime, cloudiness: float) -> float:
    """A bell-shaped production curve, scaled by the day's cloudiness."""
    hour = local.hour + local.minute / 60 + local.second / 3600
    if not SUNRISE_HOUR < hour < SUNSET_HOUR:
        return 0.0
    span = SUNSET_HOUR - SUNRISE_HOUR
    phase = (hour - SUNRISE_HOUR) / span
    # sin gives a smoother shoulder than a parabola.
    base = math.sin(phase * math.pi) ** 1.35 * PEAK_KW

    # Seasonal amplitude: a little less in winter months.
    seasonal = 0.82 + 0.18 * math.cos((local.timetuple().tm_yday - 172) / 365 * 2 * math.pi)
    # Passing clouds: brief dips on partly-cloudy days.
    flicker = 1.0
    if cloudiness > 0.25:
        flicker = 1.0 - cloudiness * (0.5 + 0.5 * math.sin(hour * 11.3 + local.day))
    return max(0.0, round(base * seasonal * (1 - cloudiness * 0.55) * flicker, 3))


def home_kw(local: datetime, rng: random.Random) -> float:
    """Baseline load plus a morning and an evening peak, plus appliance noise."""
    hour = local.hour + local.minute / 60
    load = BASELINE_HOME_KW
    # Morning routine.
    load += 1.5 * math.exp(-(((hour - 7.2) / 1.1) ** 2))
    # Evening cooking / lighting, the biggest daily peak.
    load += 2.6 * math.exp(-(((hour - 19.0) / 1.7) ** 2))
    # Daytime idle draw (fridge, standby, a little HVAC in the afternoon).
    load += 0.5 * math.exp(-(((hour - 14.5) / 3.4) ** 2))
    # Occasional appliance: kettle, dryer, EV top-up.
    if rng.random() < 0.012:
        load += rng.uniform(1.2, 3.8)
    return max(0.05, round(load + rng.gauss(0, 0.05), 3))


def generate(
    start_day: date,
    days: int,
    interval_seconds: int,
    tz,
    seed: int,
) -> list[Reading]:
    rng = random.Random(seed)
    readings: list[Reading] = []
    now = datetime.now(tz)

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        # Most days are clear; a few are overcast.
        cloudiness = max(0.0, min(0.85, rng.betavariate(1.6, 4.0)))
        cursor = datetime(day.year, day.month, day.day, tzinfo=tz)
        day_end = cursor + timedelta(days=1)
        while cursor < day_end:
            # Never fabricate readings in the future.
            if cursor > now:
                break
            solar = solar_kw(cursor, cloudiness)
            home = home_kw(cursor, rng)
            readings.append(
                Reading.create(
                    timestamp=cursor,
                    solar_kw=solar,
                    home_kw=home,
                    grid_kw=round(home - solar, 3),
                    source="livedata",
                )
            )
            cursor += timedelta(seconds=interval_seconds)
    return readings


async def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "demo.db"),
        help="Database file to write (default: data/demo.db)",
    )
    parser.add_argument("--days", type=int, default=70, help="Days of history")
    parser.add_argument(
        "--interval", type=int, default=60, help="Seconds between samples"
    )
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write even if the target file already has readings",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    store = SqliteReadingStore(db_path)
    await store.initialize()

    existing = await store.stats()
    if existing.reading_count and not args.force:
        print(
            f"{db_path} already holds {existing.reading_count} readings. "
            "Pass --force to add to it anyway.",
            file=sys.stderr,
        )
        await store.close()
        return 1

    today = datetime.now(settings.timezone).date()
    start_day = today - timedelta(days=args.days - 1)

    print(f"Generating {args.days} days ending {today} at {args.interval}s intervals...")
    readings = generate(
        start_day, args.days, args.interval, settings.timezone, args.seed
    )
    print(f"Writing {len(readings):,} readings to {db_path}...")
    await store.insert_readings(readings)

    print("Building hourly rollups...")
    hours = await refresh_hourly(store, settings, full=True)

    stats = await store.stats()
    print(
        f"Done. {stats.reading_count:,} readings, {hours:,} hourly rows, "
        f"{(stats.size_bytes or 0) / 1_048_576:.1f} MiB\n"
        f"  first: {stats.first_timestamp}\n"
        f"  last:  {stats.last_timestamp}\n\n"
        f"Run the backend against it with:  set DB_PATH={db_path}"
    )
    await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
