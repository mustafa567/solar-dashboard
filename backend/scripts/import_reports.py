"""Import daily history from SunPower "Residential Monthly Performance Report" PDFs.

The PVS6 has no historical endpoint, so anything from before this app started
polling can only come from these reports. They carry one row per day:

    Day | Energy Produced (kWh) | Energy Used (kWh) | Max AC Power Produced (kW)

Usage: point it at the PDFs (needs pypdf, in requirements-dev.txt).

    .venv/Scripts/python.exe backend/scripts/import_reports.py ~/Downloads/Resi*.pdf
    .venv/Scripts/python.exe backend/scripts/import_reports.py --dry-run reports/*.pdf

A note on the "Energy Used" column
----------------------------------
It is genuinely household use, not net grid flow: the daily values sum exactly
to the "Household use (kWh)" figure on page 1 of each report, in every month
checked. But on heavy-export days it comes out NEGATIVE, printed in accounting
parentheses like "(19.42)".

That is the same miscalibrated consumption CT the live gateway shows -- SunPower
derives household use as production plus net grid, so an overstated export drags
it below zero. The printed value is kept as ``home_kwh_reported`` and the usable
figure is corrected with GRID_SCALE exactly as live power is: ``produced +
(used - produced) * GRID_SCALE``. A day that is still negative after that is
stored as NULL rather than as fact. Production is unaffected.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import re
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.calibration import corrected_daily_home  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import DailyImport  # noqa: E402
from app.sqlite_store import SqliteReadingStore  # noqa: E402

SOURCE = "sunpower-monthly-report"

# Values may be plain (26.18), comma-grouped (1,203.96), parenthesised for
# negative ((19.42)), or absent -- which the reports write as "N/A" or as a
# bare hyphen (seen in the Max AC Power column on days with no production).
_NUMBER = r"\(?-?[\d,]+\.\d+\)?|N/A|-{1,2}"
_ROW = re.compile(
    r"([A-Z][a-z]{2} \d{2}, \d{4})\s*\n"
    rf"({_NUMBER})\s*\n({_NUMBER})\s*\n({_NUMBER})"
)


def parse_number(raw: str) -> float | None:
    """Parse one table cell, honouring accounting parentheses as negative."""
    text = raw.strip().replace(",", "")
    if text.upper() in {"N/A", "", "-", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def parse_report(path: Path, grid_scale: float = 1.0) -> list[DailyImport]:
    """Extract the daily table from one report PDF."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency guidance
        raise SystemExit(
            "pypdf is needed to read the reports. Install it with:\n"
            "    .venv/Scripts/python.exe -m pip install -r "
            "backend/requirements-dev.txt"
        ) from None

    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)

    rows: list[DailyImport] = []
    for day_text, produced, used, max_ac in _ROW.findall(text):
        solar_kwh = parse_number(produced)
        reported = parse_number(used)
        rows.append(
            DailyImport(
                day=datetime.strptime(day_text, "%b %d, %Y").date(),
                solar_kwh=solar_kwh,
                # None when still impossible after correction, so the charts
                # show a gap rather than a fabricated number.
                home_kwh=corrected_daily_home(solar_kwh, reported, grid_scale),
                max_ac_kw=parse_number(max_ac),
                source=SOURCE,
                home_kwh_reported=reported,
                grid_scale=grid_scale,
            )
        )
    return rows


def summarise(rows: list[DailyImport]) -> str:
    months: dict[str, list[DailyImport]] = {}
    for row in rows:
        months.setdefault(row.day.strftime("%Y-%m"), []).append(row)

    lines = []
    for month in sorted(months):
        days = months[month]
        solar = sum(d.solar_kwh or 0.0 for d in days)
        usable = [d for d in days if d.home_kwh is not None]
        home = sum(d.home_kwh or 0.0 for d in usable)
        dropped = len(days) - len(usable)
        note = (
            f", {dropped} day(s) with usage still impossible after correction"
            if dropped
            else ""
        )
        lines.append(
            f"  {month}: {len(days):2} days | solar {solar:8.2f} kWh | "
            f"home {home:8.2f} kWh{note}"
        )
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Report PDFs (globs allowed)")
    parser.add_argument(
        "--db", default=None, help="Database to write (default: DB_PATH from .env)"
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only import days on or after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and summarise without writing anything",
    )
    parser.add_argument(
        "--grid-scale",
        type=float,
        default=None,
        help="Correction for the usage column (default: GRID_SCALE from .env)",
    )
    args = parser.parse_args()
    settings = get_settings()
    grid_scale = args.grid_scale if args.grid_scale is not None else settings.grid_scale

    files: list[Path] = []
    for pattern in args.paths:
        matches = [Path(p) for p in glob.glob(pattern)]
        files.extend(matches or ([Path(pattern)] if Path(pattern).is_file() else []))
    files = sorted({f.resolve() for f in files})
    if not files:
        print("No report files matched.", file=sys.stderr)
        return 1

    by_day: dict[date, DailyImport] = {}
    for path in files:
        rows = parse_report(path, grid_scale)
        print(f"{path.name}: {len(rows)} day rows")
        for row in rows:
            # Later files win on overlap, which is what you want when a month is
            # re-issued.
            by_day[row.day] = row

    since = date.fromisoformat(args.since) if args.since else None
    if since:
        dropped = sum(1 for day in by_day if day < since)
        by_day = {day: row for day, row in by_day.items() if day >= since}
        if dropped:
            print(f"Skipped {dropped} day(s) before {since}")

    rows = [by_day[day] for day in sorted(by_day)]
    if not rows:
        print("Parsed no day rows -- is this the right report format?", file=sys.stderr)
        return 1

    print(f"\n{len(rows)} days, {rows[0].day} to {rows[-1].day} (GRID_SCALE {grid_scale})")
    print(summarise(rows))

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    db_path = Path(args.db) if args.db else settings.db_path
    store = SqliteReadingStore(db_path)
    await store.initialize()
    written = await store.upsert_daily_imports(rows)
    await store.close()

    print(f"\nWrote {written} days into {db_path}")
    print(
        "These fill the week/month/year views for dates the poller never saw. "
        "Measured readings always take precedence."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
