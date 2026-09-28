"""Domain types shared by the poller, the storage layer and the API.

These are plain dataclasses on purpose: the storage layer speaks in terms of
them, so swapping SQLite for Azure SQL later means writing a new store that
produces the same ``Reading`` objects and nothing else has to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

# Below this many kW the grid is treated as idle rather than importing or
# exporting. Meters jitter around zero, and a flapping arrow reads as a bug.
GRID_IDLE_THRESHOLD_KW = 0.02

GridDirection = Literal["import", "export", "idle"]

#: Where a reading came from, recorded so /api/status can show which path of
#: the gateway's local API is actually working.
ReadingSource = Literal["livedata", "meters"]


def grid_direction_for(grid_kw: float) -> GridDirection:
    """Classify signed grid power (positive = importing) into a direction."""
    if grid_kw > GRID_IDLE_THRESHOLD_KW:
        return "import"
    if grid_kw < -GRID_IDLE_THRESHOLD_KW:
        return "export"
    return "idle"


@dataclass(frozen=True, slots=True)
class Reading:
    """One sampled snapshot of the site's power flows.

    All power values are instantaneous kW. ``grid_kw`` is signed the same way
    the PVS6 reports ``/sys/livedata/net_p``: positive means importing from the
    grid, negative means exporting to it. ``grid_direction`` is derived from
    ``grid_kw`` and stored alongside it so consumers never have to re-derive
    the sign convention.
    """

    timestamp: datetime
    solar_kw: float
    home_kw: float
    grid_kw: float
    grid_direction: GridDirection
    source: ReadingSource = "livedata"

    # Lifetime kWh counters straight from the gateway, stored unmodified.
    #
    # These are recorded because they are independently verifiable against
    # SunPower's own monthly report, whereas the instantaneous power channel
    # can be miscalibrated. Energy computed from the difference between two
    # counters is exact regardless of what the power readings say. History
    # cannot be re-fetched from the gateway, so they are captured from the
    # start even though the rollups do not require them yet.
    solar_kwh_lifetime: float | None = None
    home_kwh_lifetime: float | None = None
    grid_net_kwh_lifetime: float | None = None

    @classmethod
    def create(
        cls,
        timestamp: datetime,
        solar_kw: float,
        home_kw: float,
        grid_kw: float,
        source: ReadingSource = "livedata",
        solar_kwh_lifetime: float | None = None,
        home_kwh_lifetime: float | None = None,
        grid_net_kwh_lifetime: float | None = None,
    ) -> Reading:
        """Build a reading, normalising the timestamp and deriving direction."""
        return cls(
            timestamp=to_utc(timestamp),
            solar_kw=round(float(solar_kw), 4),
            home_kw=round(float(home_kw), 4),
            grid_kw=round(float(grid_kw), 4),
            grid_direction=grid_direction_for(float(grid_kw)),
            source=source,
            solar_kwh_lifetime=_opt_round(solar_kwh_lifetime),
            home_kwh_lifetime=_opt_round(home_kwh_lifetime),
            grid_net_kwh_lifetime=_opt_round(grid_net_kwh_lifetime),
        )

    @property
    def grid_import_kw(self) -> float:
        """Grid power flowing into the home (0 when exporting)."""
        return max(0.0, self.grid_kw)

    @property
    def grid_export_kw(self) -> float:
        """Grid power flowing out to the utility (0 when importing)."""
        return max(0.0, -self.grid_kw)


@dataclass(frozen=True, slots=True)
class EnergyTotals:
    """Energy totals over some window, in kWh."""

    solar_kwh: float = 0.0
    home_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0

    @property
    def self_consumption_pct(self) -> float | None:
        """Share of generated solar consumed on site rather than exported."""
        if self.solar_kwh <= 0:
            return None
        used_on_site = max(0.0, self.solar_kwh - self.grid_export_kwh)
        return round(min(100.0, used_on_site / self.solar_kwh * 100.0), 1)

    @property
    def self_sufficiency_pct(self) -> float | None:
        """Share of household consumption met without importing from the grid."""
        if self.home_kwh <= 0:
            return None
        from_solar = max(0.0, self.home_kwh - self.grid_import_kwh)
        return round(min(100.0, from_solar / self.home_kwh * 100.0), 1)



@dataclass(frozen=True, slots=True)
class HourlyRollup:
    """Pre-aggregated energy for one UTC hour.

    Persisted by the rollup job so week / month / year views never have to load
    a year of raw samples. ``covered_seconds`` records how much of the hour the
    poller actually observed, which is what lets the UI tell a genuinely idle
    hour apart from an hour the service was down for.
    """

    hour_start: datetime
    totals: EnergyTotals
    sample_count: int
    covered_seconds: float

    @property
    def hour_end(self) -> datetime:
        return self.hour_start + timedelta(hours=1)

@dataclass(frozen=True, slots=True)
class DailyImport:
    """One day of totals imported from a SunPower monthly performance report.

    Used to give the dashboard history from before the poller existed. These
    are daily figures only -- there is no intraday detail in the reports -- so
    they can fill day/month buckets but never the hourly day view.

    ``home_kwh`` is nullable on purpose: the reports carry the same
    miscalibrated consumption channel the gateway does, and print impossible
    values (negative, in accounting parentheses) on some days. Those are stored
    as None rather than imported as fact.
    """

    day: date
    solar_kwh: float | None
    home_kwh: float | None
    max_ac_kw: float | None = None
    source: str = "sunpower-monthly-report"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One aggregated time bucket (an hour, a day or a month)."""

    start: datetime
    end: datetime
    totals: EnergyTotals
    sample_count: int
    #: Seconds of the bucket actually covered by samples. A short span against
    #: a long bucket means the poller was down for part of it.
    covered_seconds: float

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def average_kw(self, kwh: float) -> float | None:
        """Convert a kWh total in this bucket back to an average kW over it."""
        if self.covered_seconds <= 0:
            return None
        return round(kwh * 3600.0 / self.covered_seconds, 3)


def _opt_round(value: float | None, places: int = 4) -> float | None:
    """Round a counter if present, leaving None alone."""
    return None if value is None else round(float(value), places)


def to_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime (naive input assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)
