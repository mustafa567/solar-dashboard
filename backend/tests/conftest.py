"""Shared test fixtures.

Every test runs against a temporary SQLite file and a fake gateway, so nothing
here touches the real PVS6 or the real database.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.models import Reading  # noqa: E402
from app.sqlite_store import SqliteReadingStore  # noqa: E402

#: A fixed whole-hour offset keeps expected kWh values easy to reason about.
TEST_TZ = timezone(timedelta(hours=-7))

#: The seeded day is deliberately in the past: the fake poller writes a live
#: sample dated *today*, and a seeded day of "today" would then hold 1441
#: readings instead of 1440. A Sunday in a 30-day month keeps the week and
#: month bucket counts stable.
TEST_DAY = date(2026, 6, 14)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """Settings pointed at a temp directory, with no real gateway."""
    defaults: dict[str, object] = {
        "pvs_host": "192.0.2.10",  # RFC 5737 test address: never routable
        "pvs_serial": "ZT999999999999A9999",
        "poll_interval_seconds": 60,
        "poll_backoff_min_seconds": 5,
        "poll_backoff_max_seconds": 300,
        "request_timeout_seconds": 5,
        "meter_consumption_mode": "net",
        "grid_scale": 1.0,
        "db_path": tmp_path / "solar.db",
        "discovery_cache_path": tmp_path / "gateway-location.json",
        "max_sample_gap_seconds": 300,
        "backup_enabled": True,
        "backup_dir": tmp_path / "backups",
        "backup_keep_days": 3,
        "api_host": "127.0.0.1",
        "api_port": 8000,
        "static_dir": tmp_path / "dist",
        "log_level": "WARNING",
        "timezone_name": "",
        "timezone": TEST_TZ,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def solar_curve_kw(local_moment: datetime) -> float:
    """A simple bell-shaped production curve: zero at night, 6 kW at noon."""
    hour = local_moment.hour + local_moment.minute / 60.0
    if hour < 6 or hour > 18:
        return 0.0
    # Peaks at 12:00, tapering to zero at 06:00 and 18:00.
    return round(6.0 * (1 - ((hour - 12) / 6.0) ** 2), 4)


def build_day_readings(
    day: date,
    tz: timezone = TEST_TZ,
    interval_seconds: int = 60,
    home_kw: float = 1.0,
) -> list[Reading]:
    """One local day of synthetic samples with a flat house load."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    readings: list[Reading] = []
    cursor = start
    while cursor < end:
        solar = solar_curve_kw(cursor)
        readings.append(
            Reading.create(
                timestamp=cursor,
                solar_kw=solar,
                home_kw=home_kw,
                # grid = home - solar; positive means importing.
                grid_kw=round(home_kw - solar, 4),
                source="livedata",
            )
        )
        cursor += timedelta(seconds=interval_seconds)
    return readings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def store(settings: Settings):
    reading_store = SqliteReadingStore(settings.db_path)
    await reading_store.initialize()
    try:
        yield reading_store
    finally:
        await reading_store.close()
