"""Merge raw readings exported from another instance into this database.

Its reason to exist is the changeover gap: when the service moves machines, the
old one keeps recording between the snapshot and the moment it is stopped, and
those minutes are missing from the new database. Export them from the old
instance and merge them here.

    # on the OLD machine -- everything from the snapshot onwards
    curl -o gap.json "http://localhost:8000/api/export?from=2026-09-28&to=2026-09-29"

    # on the NEW machine, after copying gap.json across
    .venv/Scripts/python.exe backend/scripts/import_readings.py gap.json

Accepts either format /api/export produces, JSON or CSV.

Merging is safe to repeat. Readings are keyed by timestamp and written with
INSERT OR REPLACE, so re-importing an overlapping window changes nothing --
which means you can export a generous range rather than trying to calculate the
exact gap.

The hourly rollup is refreshed afterwards, because week/month/year views read
that cache rather than the raw rows; without it the merged readings would be
stored but invisible in those charts.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.models import Reading  # noqa: E402
from app.rollup_job import refresh_hourly  # noqa: E402
from app.sqlite_store import SqliteReadingStore  # noqa: E402


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _reading_from(record: dict[str, object]) -> Reading | None:
    """Build a Reading from one exported row, tolerating missing optionals."""
    stamp = record.get("timestamp") or record.get("timestamp_utc")
    if not stamp:
        epoch = record.get("epoch_seconds")
        if epoch in (None, ""):
            return None
        moment = datetime.fromtimestamp(int(float(epoch)), tz=timezone.utc)  # type: ignore[arg-type]
    else:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))

    solar = _float_or_none(record.get("solar_kw"))
    home = _float_or_none(record.get("home_kw"))
    grid = _float_or_none(record.get("grid_kw"))
    if solar is None or home is None or grid is None:
        return None

    source = str(record.get("source") or "livedata")
    return Reading.create(
        timestamp=moment,
        solar_kw=solar,
        home_kw=home,
        grid_kw=grid,
        source=source,  # type: ignore[arg-type]
        solar_kwh_lifetime=_float_or_none(record.get("solar_kwh_lifetime")),
        home_kwh_lifetime=_float_or_none(record.get("home_kwh_lifetime")),
        grid_net_kwh_lifetime=_float_or_none(record.get("grid_net_kwh_lifetime")),
        # Exports from before the column existed were all stored at 1.0.
        grid_scale=_float_or_none(record.get("grid_scale")) or 1.0,
    )


def load(path: Path) -> list[Reading]:
    """Read an /api/export file, either format."""
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()

    if stripped.startswith("{") or stripped.startswith("["):
        payload = json.loads(text)
        records = payload["readings"] if isinstance(payload, dict) else payload
    else:
        records = list(csv.DictReader(text.splitlines()))

    readings = []
    skipped = 0
    for record in records:
        reading = _reading_from(record)
        if reading is None:
            skipped += 1
            continue
        readings.append(reading)

    if skipped:
        print(f"Skipped {skipped} row(s) missing required fields", file=sys.stderr)
    readings.sort(key=lambda r: r.timestamp)
    return readings


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Export file from /api/export (JSON or CSV)")
    parser.add_argument(
        "--db", default=None, help="Database to merge into (default: DB_PATH)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be merged without writing",
    )
    args = parser.parse_args()

    source = Path(args.path)
    if not source.is_file():
        print(f"No such file: {source}", file=sys.stderr)
        return 1

    readings = load(source)
    if not readings:
        print("Nothing to import -- the file held no usable readings.", file=sys.stderr)
        return 1

    print(f"{source.name}: {len(readings):,} readings")
    print(f"  span {readings[0].timestamp.isoformat()}")
    print(f"    -> {readings[-1].timestamp.isoformat()}")

    settings = get_settings()
    db_path = Path(args.db) if args.db else settings.db_path
    store = SqliteReadingStore(db_path)
    await store.initialize()
    try:
        before = await store.stats()
        print(f"\n{db_path}")
        print(f"  before: {before.reading_count:,} readings")

        # How many of these are genuinely new, so the report is about the gap
        # rather than the size of the file.
        # query_range's end is exclusive, so reach past the final reading or it
        # is reported as new on every run.
        existing = await store.query_range(
            readings[0].timestamp,
            readings[-1].timestamp + timedelta(seconds=1),
        )
        known = {r.timestamp.replace(microsecond=0) for r in existing}
        fresh = [
            r for r in readings if r.timestamp.replace(microsecond=0) not in known
        ]
        print(f"  new in this file: {len(fresh):,}")

        if args.dry_run:
            print("\nDry run: nothing written.")
            return 0

        await store.insert_readings(readings)
        after = await store.stats()
        print(f"  after : {after.reading_count:,} readings "
              f"(+{after.reading_count - before.reading_count:,})")

        print("\nRefreshing hourly rollups so the merged data shows in the charts...")
        hours = await refresh_hourly(store, settings, full=False)
        print(f"  {hours} hour(s) recomputed")

        print(f"\nCoverage now {after.first_timestamp} -> {after.last_timestamp}")
    finally:
        await store.close()

    print("\nDone. Restart the service (or it will pick this up on its next poll).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
