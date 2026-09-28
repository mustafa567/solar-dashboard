"""SQLite implementation of ReadingStore.

This is the ONLY module in the project that imports ``sqlite3`` or contains
SQL. Keeping it that way is what makes the planned SQLite -> Azure SQL move a
single-file change: write a sibling store with the same ``ReadingStore``
methods and switch ``store.create_store()`` over to it.

Concurrency notes: one connection is shared across the poller task and API
requests, guarded by a lock, and every blocking call is pushed to a worker
thread so the event loop never stalls. WAL mode lets readers proceed while the
poller writes.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .models import DailyImport, EnergyTotals, HourlyRollup, Reading, to_utc
from .store import ReadingStore, StoreStats

_LOGGER = logging.getLogger(__name__)

#: Bumped whenever the schema changes, so a future migration can branch on it.
SCHEMA_VERSION = 4

# Column notes:
#   ts             UTC epoch seconds, primary key (also the range-query index)
#   solar_kw       instantaneous solar production, kW
#   home_kw        instantaneous house consumption, kW
#   grid_kw        signed grid power: > 0 importing, < 0 exporting
#   grid_direction derived label: import / export / idle
#   source         which gateway API the sample came from: livedata / meters
#   *_kwh_lifetime the gateway's own cumulative counters, stored unmodified;
#                  nullable because older rows predate them
#
# hourly_rollup is a derived cache keyed by UTC hour start. It can be dropped
# and rebuilt from readings at any time; see rollup_job.py.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts              INTEGER PRIMARY KEY,
    solar_kw        REAL    NOT NULL,
    home_kw         REAL    NOT NULL,
    grid_kw         REAL    NOT NULL,
    grid_direction  TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'livedata',
    solar_kwh_lifetime      REAL,
    home_kwh_lifetime       REAL,
    grid_net_kwh_lifetime   REAL
);

-- Daily totals imported from SunPower monthly reports, for history from
-- before this app was recording. Separate from `readings` so imported figures
-- are never mistaken for measured ones. home_kwh is nullable: the reports carry
-- impossible (negative) values on some days and those are not imported as fact.
CREATE TABLE IF NOT EXISTS daily_import (
    day          TEXT    PRIMARY KEY,
    solar_kwh    REAL,
    home_kwh     REAL,
    max_ac_kw    REAL,
    source       TEXT    NOT NULL,
    imported_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hourly_rollup (
    hour_ts          INTEGER PRIMARY KEY,
    solar_kwh        REAL    NOT NULL,
    home_kwh         REAL    NOT NULL,
    grid_import_kwh  REAL    NOT NULL,
    grid_export_kwh  REAL    NOT NULL,
    sample_count     INTEGER NOT NULL,
    covered_seconds  REAL    NOT NULL
);
"""

_COLUMNS = (
    "ts, solar_kw, home_kw, grid_kw, grid_direction, source,"
    " solar_kwh_lifetime, home_kwh_lifetime, grid_net_kwh_lifetime"
)

#: Columns added after v1, applied to existing databases on open.
_ADDED_COLUMNS = {
    "solar_kwh_lifetime": "REAL",
    "home_kwh_lifetime": "REAL",
    "grid_net_kwh_lifetime": "REAL",
}

_INSERT_SQL = (
    f"INSERT OR REPLACE INTO readings ({_COLUMNS})"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_HOURLY_COLUMNS = (
    "hour_ts, solar_kwh, home_kwh, grid_import_kwh, grid_export_kwh,"
    " sample_count, covered_seconds"
)

_HOURLY_INSERT_SQL = (
    f"INSERT OR REPLACE INTO hourly_rollup ({_HOURLY_COLUMNS})"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _to_epoch(value: datetime) -> int:
    return int(to_utc(value).timestamp())


def _from_epoch(value: int) -> datetime:
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _row_to_reading(row: sqlite3.Row) -> Reading:
    return Reading(
        timestamp=_from_epoch(row["ts"]),
        solar_kw=row["solar_kw"],
        home_kw=row["home_kw"],
        grid_kw=row["grid_kw"],
        grid_direction=row["grid_direction"],
        source=row["source"],
        solar_kwh_lifetime=row["solar_kwh_lifetime"],
        home_kwh_lifetime=row["home_kwh_lifetime"],
        grid_net_kwh_lifetime=row["grid_net_kwh_lifetime"],
    )


def _row_to_hourly(row: sqlite3.Row) -> HourlyRollup:
    return HourlyRollup(
        hour_start=_from_epoch(row["hour_ts"]),
        totals=EnergyTotals(
            solar_kwh=row["solar_kwh"],
            home_kwh=row["home_kwh"],
            grid_import_kwh=row["grid_import_kwh"],
            grid_export_kwh=row["grid_export_kwh"],
        ),
        sample_count=row["sample_count"],
        covered_seconds=row["covered_seconds"],
    )


def _reading_params(reading: Reading) -> tuple[object, ...]:
    return (
        _to_epoch(reading.timestamp),
        reading.solar_kw,
        reading.home_kw,
        reading.grid_kw,
        reading.grid_direction,
        reading.source,
        reading.solar_kwh_lifetime,
        reading.home_kwh_lifetime,
        reading.grid_net_kwh_lifetime,
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema.

    Only additive so far: CREATE TABLE IF NOT EXISTS leaves an older `readings`
    table alone, so any column added after v1 is applied here instead. Existing
    rows keep NULL for the new counters, which is exactly right -- those
    readings were taken before the gateway's counters were recorded.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(readings)")}
    for column, column_type in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {column} {column_type}")
            _LOGGER.info("Schema migration: added readings.%s", column)


class SqliteReadingStore(ReadingStore):
    """Stores readings in a local SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # -- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,  # autocommit; no long-lived transactions
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn = conn
        _LOGGER.info(
            "SQLite store ready at %s (schema v%s)", self._db_path, SCHEMA_VERSION
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise RuntimeError("SqliteReadingStore.initialize() has not been called")
        return conn

    # -- writes ------------------------------------------------------------

    async def insert_reading(self, reading: Reading) -> None:
        await asyncio.to_thread(self._insert_sync, reading)

    def _insert_sync(self, reading: Reading) -> None:
        conn = self._require_conn()
        with self._lock:
            conn.execute(_INSERT_SQL, _reading_params(reading))

    async def insert_readings(self, readings: list[Reading]) -> int:
        """Bulk insert, used by tests and any future backfill/import tooling."""
        return await asyncio.to_thread(self._insert_many_sync, readings)

    def _insert_many_sync(self, readings: list[Reading]) -> int:
        if not readings:
            return 0
        conn = self._require_conn()
        rows = [_reading_params(r) for r in readings]
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(_INSERT_SQL, rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    # -- reads -------------------------------------------------------------

    async def query_range(
        self,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Reading]:
        return await asyncio.to_thread(self._query_range_sync, start, end, limit)

    def _query_range_sync(
        self,
        start: datetime,
        end: datetime,
        limit: int | None,
    ) -> list[Reading]:
        conn = self._require_conn()
        sql = (
            f"SELECT {_COLUMNS} FROM readings"
            " WHERE ts >= ? AND ts < ? ORDER BY ts ASC"
        )
        params: list[object] = [_to_epoch(start), _to_epoch(end)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_reading(row) for row in rows]

    async def latest_reading(self) -> Reading | None:
        return await asyncio.to_thread(self._latest_sync)

    def _latest_sync(self) -> Reading | None:
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return _row_to_reading(row) if row else None

    async def reading_before(self, moment: datetime) -> Reading | None:
        return await asyncio.to_thread(self._reading_before_sync, moment)

    def _reading_before_sync(self, moment: datetime) -> Reading | None:
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM readings WHERE ts < ?"
                " ORDER BY ts DESC LIMIT 1",
                (_to_epoch(moment),),
            ).fetchone()
        return _row_to_reading(row) if row else None

    async def stats(self) -> StoreStats:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> StoreStats:
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
                " FROM readings"
            ).fetchone()
        try:
            size: int | None = self._db_path.stat().st_size
        except OSError:
            size = None
        return StoreStats(
            reading_count=row["n"] or 0,
            first_timestamp=_from_epoch(row["first_ts"]) if row["first_ts"] else None,
            last_timestamp=_from_epoch(row["last_ts"]) if row["last_ts"] else None,
            size_bytes=size,
        )

    # -- hourly rollups ----------------------------------------------------

    async def upsert_hourly(self, rollups: list[HourlyRollup]) -> int:
        return await asyncio.to_thread(self._upsert_hourly_sync, rollups)

    def _upsert_hourly_sync(self, rollups: list[HourlyRollup]) -> int:
        if not rollups:
            return 0
        conn = self._require_conn()
        rows = [
            (
                _to_epoch(r.hour_start),
                r.totals.solar_kwh,
                r.totals.home_kwh,
                r.totals.grid_import_kwh,
                r.totals.grid_export_kwh,
                r.sample_count,
                r.covered_seconds,
            )
            for r in rollups
        ]
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(_HOURLY_INSERT_SQL, rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    async def query_hourly(
        self, start: datetime, end: datetime
    ) -> list[HourlyRollup]:
        return await asyncio.to_thread(self._query_hourly_sync, start, end)

    def _query_hourly_sync(
        self, start: datetime, end: datetime
    ) -> list[HourlyRollup]:
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                f"SELECT {_HOURLY_COLUMNS} FROM hourly_rollup"
                " WHERE hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts ASC",
                (_to_epoch(start), _to_epoch(end)),
            ).fetchall()
        return [_row_to_hourly(row) for row in rows]

    async def latest_hourly_hour(self) -> datetime | None:
        return await asyncio.to_thread(self._latest_hourly_sync)

    def _latest_hourly_sync(self) -> datetime | None:
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT MAX(hour_ts) AS hour_ts FROM hourly_rollup"
            ).fetchone()
        return _from_epoch(row["hour_ts"]) if row and row["hour_ts"] else None

    # -- imported history --------------------------------------------------

    async def upsert_daily_imports(self, rows: list[DailyImport]) -> int:
        return await asyncio.to_thread(self._upsert_daily_sync, rows)

    def _upsert_daily_sync(self, rows: list[DailyImport]) -> int:
        if not rows:
            return 0
        conn = self._require_conn()
        now = int(datetime.now(timezone.utc).timestamp())
        params = [
            (
                row.day.isoformat(),
                row.solar_kwh,
                row.home_kwh,
                row.max_ac_kw,
                row.source,
                now,
            )
            for row in rows
        ]
        with self._lock:
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO daily_import"
                    " (day, solar_kwh, home_kwh, max_ac_kw, source, imported_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    params,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(params)

    async def query_daily_imports(
        self, start: date, end: date
    ) -> list[DailyImport]:
        return await asyncio.to_thread(self._query_daily_sync, start, end)

    def _query_daily_sync(self, start: date, end: date) -> list[DailyImport]:
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT day, solar_kwh, home_kwh, max_ac_kw, source"
                " FROM daily_import WHERE day >= ? AND day < ? ORDER BY day ASC",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [
            DailyImport(
                day=date.fromisoformat(row["day"]),
                solar_kwh=row["solar_kwh"],
                home_kwh=row["home_kwh"],
                max_ac_kw=row["max_ac_kw"],
                source=row["source"],
            )
            for row in rows
        ]

    # -- backup ------------------------------------------------------------

    async def backup_to(self, destination: Path) -> Path | None:
        return await asyncio.to_thread(self._backup_sync, Path(destination))

    def _backup_sync(self, destination: Path) -> Path:
        """Use SQLite's online backup API, which is WAL- and writer-safe."""
        conn = self._require_conn()
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            with self._lock:
                conn.backup(target)
        finally:
            target.close()
        return destination


__all__ = ["SqliteReadingStore", "SCHEMA_VERSION"]
