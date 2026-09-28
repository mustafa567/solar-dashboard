"""Storage abstraction for solar readings.

Everything in the app talks to a ``ReadingStore``. The only implementation
today is ``SqliteReadingStore`` in ``sqlite_store.py``, which is the *single*
module in this project containing SQL or the ``sqlite3`` import. Migrating to
Azure SQL later means adding one more subclass here and pointing the factory at
it -- no other module needs to change.

The interface is async so a genuinely async backend (aioodbc, asyncpg, ...) can
implement it directly; the SQLite implementation runs its blocking calls in a
worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .models import DailyImport, HourlyRollup, Reading


@dataclass(frozen=True, slots=True)
class StoreStats:
    """Cheap summary of what the store currently holds, for /api/status."""

    reading_count: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    size_bytes: int | None = None


class ReadingStore(ABC):
    """Persistence contract for time-series solar readings."""

    @abstractmethod
    async def initialize(self) -> None:
        """Open connections and create the schema if it does not exist."""

    @abstractmethod
    async def close(self) -> None:
        """Release any resources held by the store."""

    @abstractmethod
    async def insert_reading(self, reading: Reading) -> None:
        """Persist one reading. Re-inserting the same timestamp replaces it."""

    @abstractmethod
    async def query_range(
        self,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Reading]:
        """Return readings with ``start <= timestamp < end``, oldest first."""

    @abstractmethod
    async def latest_reading(self) -> Reading | None:
        """Return the most recent reading, or None when the store is empty."""

    @abstractmethod
    async def reading_before(self, moment: datetime) -> Reading | None:
        """Return the newest reading strictly before ``moment``.

        Rollups use this to pick up the sample that precedes a window, so
        energy in the first partial interval is not silently dropped.
        """

    # -- hourly rollups ----------------------------------------------------
    #
    # Raw samples stay the source of truth; these rows are a derived cache so
    # week / month / year views never have to load a year of samples. They can
    # be deleted and rebuilt from `readings` at any time.

    @abstractmethod
    async def upsert_hourly(self, rollups: list[HourlyRollup]) -> int:
        """Insert or replace hourly rollup rows, keyed by hour start."""

    @abstractmethod
    async def query_hourly(
        self, start: datetime, end: datetime
    ) -> list[HourlyRollup]:
        """Return hourly rollups with ``start <= hour_start < end``."""

    @abstractmethod
    async def latest_hourly_hour(self) -> datetime | None:
        """Return the most recent rolled-up hour, or None if there are none."""

    # -- imported history --------------------------------------------------
    #
    # Daily totals from SunPower's monthly reports, covering the period before
    # this app was recording. Kept in their own table so they are never confused
    # with measured readings.

    @abstractmethod
    async def upsert_daily_imports(self, rows: list[DailyImport]) -> int:
        """Insert or replace imported daily totals, keyed by local date."""

    @abstractmethod
    async def query_daily_imports(
        self, start: date, end: date
    ) -> list[DailyImport]:
        """Return imported days with ``start <= day < end``."""

    @abstractmethod
    async def stats(self) -> StoreStats:
        """Return a summary of stored data."""

    async def backup_to(self, destination: Path) -> Path | None:
        """Write a consistent copy of the data to ``destination``.

        Returns the path written, or None for backends where file-level backup
        does not apply (a managed database handles its own backups).
        """
        return None


def create_store() -> ReadingStore:
    """Build the configured store.

    This is the single seam to change when moving to Azure SQL: return an
    ``AzureSqlReadingStore`` here (selected by a config value) and the poller,
    the rollups and the API all keep working unchanged.
    """
    from .config import get_settings
    from .sqlite_store import SqliteReadingStore

    settings = get_settings()
    return SqliteReadingStore(settings.db_path)
