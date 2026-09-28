"""Prove a database copy is complete and undamaged.

Used when moving the service to another machine. The reading history cannot be
re-fetched from the gateway, so "it looked like it copied" is not good enough --
run this on the source, run it on the target, and compare the fingerprints.

    # on the old machine, after stopping the service
    .venv/Scripts/python.exe backend/scripts/verify_db.py data/solar.db

    # on the new machine, after copying
    python3 backend/scripts/verify_db.py data/solar.db

Identical "fingerprint" lines mean every row arrived. It reports integrity,
schema version, row counts, the date span and per-table checksums.

Exit code is 0 when the database is healthy, 1 when it is not -- so it can gate
a scripted migration.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

TABLES = ("readings", "hourly_rollup", "daily_import")


def human(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def checksum(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    """Row count and a content hash, computed the same way on every platform.

    Hashing the rows rather than the file: a file hash would differ between two
    perfectly good copies (page layout, WAL state, vacuum), while this compares
    what the data actually is.
    """
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
        count += 1
        # repr of a tuple of plain SQLite types is stable across versions.
        digest.update(repr(tuple(row)).encode("utf-8"))
    return count, digest.hexdigest()[:16]


def verify(path: Path) -> int:
    if not path.is_file():
        print(f"No database at {path}", file=sys.stderr)
        return 1

    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"Database : {path}")
    print(f"Size     : {size_mb:.1f} MiB")

    # Read-only so this can never be the thing that damages the file.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    healthy = True

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"Integrity: {integrity}")
        if integrity != "ok":
            healthy = False

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        print(f"Schema   : v{version}")

        present = table_names(conn)
        missing = [t for t in TABLES if t not in present]
        if missing:
            healthy = False
            print(f"Missing tables: {', '.join(missing)}")
            if "readings" in missing:
                # Almost always this: someone copied solar.db on its own while
                # the service was running, leaving every recent write behind in
                # the -wal sidecar.
                print(
                    "\n  This looks like a copy taken without its -wal file.\n"
                    "  SQLite in WAL mode keeps recent writes in <name>.db-wal;\n"
                    "  the .db on its own can be nearly empty.\n"
                    "\n"
                    "  Fix: stop the service (which checkpoints the WAL into the\n"
                    "  .db) and copy again, or copy a snapshot from data/backups/\n"
                    "  instead -- those are written through SQLite's online\n"
                    "  backup API and are always self-contained."
                )

        if "readings" in present:
            span = conn.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
            if span and span[0]:
                print(f"Readings : {human(span[0])}  ->  {human(span[1])}")

        if "daily_import" in present:
            days = conn.execute(
                "SELECT MIN(day), MAX(day) FROM daily_import"
            ).fetchone()
            if days and days[0]:
                print(f"Imported : {days[0]}  ->  {days[1]}")

        print("\nFingerprint (must match on both machines):")
        parts = []
        for table in TABLES:
            if table not in present:
                continue
            count, digest = checksum(conn, table)
            print(f"  {table:14} {count:>8} rows   {digest}")
            parts.append(f"{table}:{count}:{digest}")

        overall = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
        print(f"  {'COMBINED':14} {'':>8}        {overall}")
    finally:
        conn.close()

    print("\nOK" if healthy else "\nPROBLEMS FOUND", file=sys.stderr if not healthy else sys.stdout)
    return 0 if healthy else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default="data/solar.db",
        help="Database file (default: data/solar.db)",
    )
    args = parser.parse_args()
    return verify(Path(args.path))


if __name__ == "__main__":
    raise SystemExit(main())
