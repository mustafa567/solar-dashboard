"""Collect everything the repo does NOT carry, ready to copy to another machine.

A `git clone` gives you the code. It deliberately does not give you your
readings, your gateway credentials, or the built frontend. This gathers exactly
those into one folder, with the database taken through SQLite's online backup
API so it is safe to run while the poller is still going -- no stopping the
service, and no risk of copying a 4 KB `.db` while 2.5 MB of readings sit
unflushed in the `-wal` sidecar.

    .venv/Scripts/python.exe backend/scripts/make_bundle.py
    .venv/Scripts/python.exe backend/scripts/make_bundle.py --include-dist

Copy the resulting folder to the new machine and follow RESTORE.txt inside it.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.sqlite_store import SqliteReadingStore  # noqa: E402

RESTORE_NOTES = """\
Restoring this bundle on the new machine
========================================

The repo is already cloned there. From the root of that clone:

1. Put these files in place

       copy .env                  <clone>\\.env
       copy data\\solar.db         <clone>\\data\\solar.db
       (and frontend\\dist\\ too, if this bundle has it)

   Create the data folder first if it does not exist.

2. Create the environment

       python -m venv .venv
       .venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt

   Linux/macOS: python3 -m venv .venv && .venv/bin/python -m pip install -r backend/requirements.txt

3. Prove the database arrived intact

       .venv\\Scripts\\python.exe backend\\scripts\\verify_db.py data\\solar.db

   The COMBINED fingerprint must match the one in FINGERPRINT.txt in this
   bundle. If it does not, copy the database again -- do not carry on.

4. If this bundle has no frontend\\dist, build it (needs Node 18+)

       cd frontend && npm install && npm run build && cd ..

5. Start it

       backend\\scripts\\run-backend.bat

   Then open http://localhost:8000 and check /api/status shows the poller
   running and success_count climbing.

6. Only once that is working, stop the service on the old machine so two
   pollers are not writing to two separate databases.

Notes
-----
* .env holds your PVS serial, whose last 5 characters are the gateway's local
  API password. Treat this bundle like a password: do not email it or put it in
  cloud storage you would not put a password in. Delete it once restored.
* The database in this bundle is a point-in-time snapshot. Anything the old
  machine records after you made it will not be here, so keep the changeover
  short.
"""


def consolidate(path: Path) -> None:
    """Fold the copy into a single file with no -wal / -shm sidecars.

    The backup inherits WAL mode, so simply opening the copy creates sidecars
    again -- and a bundle where the data is split across three files is exactly
    the trap this script exists to avoid. Switching the journal to DELETE
    checkpoints everything into the .db and removes them. The app puts it back
    into WAL mode on first open, so nothing is lost by doing this.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()

    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        sidecar.unlink(missing_ok=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=None,
        help="Where to write the bundle (default: alongside the project)",
    )
    parser.add_argument(
        "--include-dist",
        action="store_true",
        help="Include the built frontend, so the new machine does not need Node",
    )
    args = parser.parse_args()

    settings = get_settings()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(args.out) if args.out else PROJECT_ROOT.parent
    bundle = base / f"solar-dashboard-bundle-{stamp}"
    (bundle / "data").mkdir(parents=True, exist_ok=True)

    print(f"Building bundle in {bundle}\n")

    # 1. The database, via the online backup API so the WAL is folded in.
    store = SqliteReadingStore(settings.db_path)
    await store.initialize()
    try:
        target = bundle / "data" / "solar.db"
        await store.backup_to(target)
        stats = await store.stats()
    finally:
        await store.close()

    consolidate(target)

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"  data/solar.db          {size_mb:6.1f} MiB  "
          f"({stats.reading_count:,} readings, single file)")

    # 2. The config, which carries the gateway credentials.
    env_source = PROJECT_ROOT / ".env"
    if env_source.is_file():
        shutil.copy2(env_source, bundle / ".env")
        print("  .env                            copied  (contains your PVS_SN)")
    else:
        print("  .env                          MISSING  - copy it by hand", file=sys.stderr)

    # 3. The built frontend, optional -- it spares the new machine needing Node.
    if args.include_dist:
        dist = PROJECT_ROOT / "frontend" / "dist"
        if dist.is_dir():
            shutil.copytree(dist, bundle / "frontend" / "dist", dirs_exist_ok=True)
            total = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file())
            print(f"  frontend/dist          {total / 1024:6.0f} KiB  copied")
        else:
            print("  frontend/dist                 MISSING  - run npm run build",
                  file=sys.stderr)

    # 4. The fingerprint, so the copy can be proved complete on arrival.
    sys.argv = ["verify_db.py", str(target)]
    from verify_db import verify  # noqa: E402 - imported late, same folder

    print("\n--- fingerprint of the bundled database ---")
    import contextlib
    import io

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        verify(target)
    report = captured.getvalue()
    print(report)
    (bundle / "FINGERPRINT.txt").write_text(report, encoding="utf-8")

    (bundle / "RESTORE.txt").write_text(RESTORE_NOTES, encoding="utf-8")

    print(f"Done. Copy this folder to the new machine:\n\n    {bundle}\n")
    print("It contains your gateway password (.env) -- move it the way you would")
    print("move a password, and delete it once restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
