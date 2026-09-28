"""Re-express stored history under the configured GRID_SCALE.

GRID_SCALE corrects the gateway's miscalibrated net/consumption channel (see
AGENTS.md rule 9), but changing it in .env only affects readings taken from then
on. This brings everything already stored into line:

* ``readings`` -- every row records the scale it was stored under, so the raw
  net power is recovered exactly (``grid_kw / grid_scale``) and re-corrected.
  House load is re-derived from ``solar + grid``, as the gateway does.
* ``daily_import`` -- house use is re-derived from the value the SunPower report
  printed. Days imported before v5 whose printed value was negative were
  dropped at the time; re-run ``import_reports.py`` on the PDFs to recover them.
* ``hourly_rollup`` -- rebuilt from scratch, since it is derived from readings.

A consistent snapshot is written to BACKUP_DIR first and is never pruned by the
daily rotation. Rows already at the target scale are left alone, so it is safe
to run again -- including after ``import_readings.py`` merges an export taken
from an instance that was still at the old scale.

Stop the service first so the poller is not writing while history is rewritten:

    .venv/Scripts/python.exe backend/scripts/recalibrate.py --dry-run
    .venv/Scripts/python.exe backend/scripts/recalibrate.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.calibration import rescale_daily_import, rescale_reading  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Reading  # noqa: E402
from app.rollup import integrate_readings  # noqa: E402
from app.rollup_job import refresh_hourly  # noqa: E402
from app.sqlite_store import SqliteReadingStore  # noqa: E402

BACKUP_NAME = "pre-recalibrate-{stamp}.db"

#: Readings are rewritten a week at a time, so memory stays flat on a year of
#: history.
CHUNK = timedelta(days=7)


def _daily_summary(
    readings: list[Reading], tz, max_gap_seconds: float
) -> dict[date, tuple[float, float, float, float]]:
    """(solar, home, import, export) kWh per local day, for the before/after."""
    if not readings:
        return {}
    first = readings[0].timestamp.astimezone(tz).date()
    last = readings[-1].timestamp.astimezone(tz).date()
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    edges = [datetime.combine(day, time(), tz) for day in days]
    edges.append(datetime.combine(last + timedelta(days=1), time(), tz))
    buckets = integrate_readings(readings, edges, max_gap_seconds)
    return {
        day: (
            b.totals.solar_kwh,
            b.totals.home_kwh,
            b.totals.grid_import_kwh,
            b.totals.grid_export_kwh,
        )
        for day, b in zip(days, buckets)
        if b.sample_count
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=None, help="Database to correct (default: DB_PATH)"
    )
    parser.add_argument(
        "--grid-scale",
        type=float,
        default=None,
        help="Target scale (default: GRID_SCALE from .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything",
    )
    args = parser.parse_args()

    settings = get_settings()
    target = args.grid_scale if args.grid_scale is not None else settings.grid_scale
    db_path = Path(args.db) if args.db else settings.db_path
    if not db_path.is_file():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    store = SqliteReadingStore(db_path)
    await store.initialize()
    try:
        stats = await store.stats()
        readings: list[Reading] = []
        if stats.first_timestamp is not None and stats.last_timestamp is not None:
            cursor = stats.first_timestamp
            end = stats.last_timestamp + timedelta(seconds=1)
            while cursor < end:
                readings.extend(await store.query_range(cursor, min(cursor + CHUNK, end)))
                cursor += CHUNK

        imports = await store.query_daily_imports(date.min, date.max)

        corrected = [rescale_reading(r, target) for r in readings]
        changed = [new for old, new in zip(readings, corrected) if new is not old]
        new_imports = [rescale_daily_import(row, target) for row in imports]
        changed_imports = [
            new for old, new in zip(imports, new_imports) if old.grid_scale != target
        ]

        print(f"Database   {db_path}")
        print(f"Target     GRID_SCALE {target}")
        scales = Counter(r.grid_scale for r in readings)
        print(
            f"Readings   {len(readings):,} stored "
            f"({', '.join(f'{n:,} at {s}' for s, n in sorted(scales.items())) or 'none'})"
            f"; {len(changed):,} to rewrite"
        )
        unknown_before = sum(1 for row in imports if row.home_kwh is None)
        unknown_after = sum(1 for row in new_imports if row.home_kwh is None)
        unrecoverable = sum(1 for row in imports if row.home_kwh_reported is None)
        print(
            f"Imports    {len(imports)} days; {len(changed_imports)} to rewrite; "
            f"usage unknown on {unknown_before} -> {unknown_after} "
            f"({unrecoverable} have no printed value -- re-import the PDFs)"
        )

        tz = settings.timezone
        before = _daily_summary(readings, tz, settings.max_sample_gap_seconds)
        after = _daily_summary(corrected, tz, settings.max_sample_gap_seconds)
        if before:
            print("\nPer day (kWh)          solar |   home before -> after |"
                  " import before -> after | export before -> after")
            for day in sorted(before):
                s0, h0, i0, e0 = before[day]
                _, h1, i1, e1 = after[day]
                print(
                    f"  {day}  {s0:12.2f} | {h0:13.2f} -> {h1:5.2f} |"
                    f" {i0:15.2f} -> {i1:5.2f} | {e0:15.2f} -> {e1:5.2f}"
                )

        if args.dry_run:
            print("\nDry run: nothing written.")
            return 0
        if not changed and not changed_imports:
            print("\nAlready at the target scale; nothing to do.")
            return 0

        backup_dir = settings.backup_dir
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = await store.backup_to(backup_dir / BACKUP_NAME.format(stamp=stamp))
        print(f"\nBackup     {backup}")

        await store.insert_readings(changed)
        await store.upsert_daily_imports(changed_imports)
        hours = await refresh_hourly(store, settings, full=True)
        print(
            f"Wrote      {len(changed):,} readings, {len(changed_imports)} imported "
            f"days, rebuilt {hours} rollup hours"
        )
        if target != settings.grid_scale:
            print(
                f"\nNOTE: .env still has GRID_SCALE={settings.grid_scale}. Set it to "
                f"{target} before restarting, or new readings will not match."
            )
        return 0
    finally:
        await store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
