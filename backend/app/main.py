"""FastAPI application: REST API plus the built frontend.

One process runs everything -- the poller, the rollup job, the daily backup and
the HTTP API -- which is the point: on Windows that means one service to install
and one thing to watch, rather than four.
"""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, tzinfo
from typing import AsyncIterator, Iterator, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import __version__
from .backup import BackupManager
from .config import Settings, get_settings, mask_serial
from .live import build_live_payload
from .models import Bucket, EnergyTotals, to_utc, utcnow
from .poller import Poller
from .rollup import (
    RangeKind,
    Window,
    build_window,
    combine_hourly,
    integrate_readings,
    merge_daily_imports,
    sum_totals,
)
from .rollup_job import refresh_hourly
from .scheduler import PeriodicTask
from .store import ReadingStore, create_store

_LOGGER = logging.getLogger(__name__)

#: How often the hourly rollup table is refreshed.
ROLLUP_INTERVAL_SECONDS = 300
#: How often a backup is taken. Daily, per the requirement.
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60

#: Guard rail on /api/export so one request cannot try to serialise years of
#: samples into memory at once.
EXPORT_MAX_DAYS = 400

VALID_RANGES: tuple[RangeKind, ...] = ("day", "week", "month", "year")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # pypvs logs the gateway's session cookie at INFO on every login, and that
    # cookie is a live credential. Keep its warnings, drop the rest.
    logging.getLogger("pypvs").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the store, the poller and the background jobs."""
    settings: Settings = app.state.settings
    store: ReadingStore = app.state.store

    await store.initialize()

    # app.state.gateway_client is a seam: tests inject a fake gateway here so
    # nothing in the suite reaches for a real PVS6.
    poller: Poller = Poller(
        store=store,
        settings=settings,
        client=getattr(app.state, "gateway_client", None),
    )
    backups = BackupManager(store=store, settings=settings)

    rollup_task = PeriodicTask(
        name="hourly-rollup",
        interval_seconds=ROLLUP_INTERVAL_SECONDS,
        action=lambda: refresh_hourly(store, settings),
    )
    backup_task = PeriodicTask(
        name="daily-backup",
        interval_seconds=BACKUP_INTERVAL_SECONDS,
        action=backups.run,
    )

    app.state.poller = poller
    app.state.backups = backups
    app.state.rollup_task = rollup_task
    app.state.backup_task = backup_task
    app.state.started_at = utcnow()

    await poller.start()
    await rollup_task.start()
    await backup_task.start()
    _LOGGER.info("Solar dashboard backend ready: %s", settings.describe())

    try:
        yield
    finally:
        await backup_task.stop()
        await rollup_task.stop()
        await poller.stop()
        await store.close()


def create_app(
    settings: Settings | None = None,
    store: ReadingStore | None = None,
) -> FastAPI:
    """Build the app. Injectable settings/store keep it testable."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Solar Dashboard",
        version=__version__,
        description="Local PVS6 monitoring: live readings, history and export.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store or create_store()

    register_api_routes(app)
    mount_frontend(app, settings)
    return app


# -- helpers ---------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> ReadingStore:
    return request.app.state.store


def _poller(request: Request) -> Poller:
    return request.app.state.poller


def _view_timezone(settings: Settings, requested: str | None) -> tuple[tzinfo, str]:
    """The zone to cut days in: the viewer's browser zone, else TIMEZONE.

    "Today" belongs to whoever is looking, and the host PC's clock zone can be
    wrong, so the frontend sends its IANA zone. Anything that does not load
    falls back to the configured zone rather than failing the request.
    """
    name = (requested or "").strip()
    if name and len(name) <= 64:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name), name
        except Exception:  # noqa: BLE001 - unknown or malformed zone name
            _LOGGER.debug("Ignoring unknown tz=%r from the browser", name)
    return settings.timezone, settings.timezone_name or str(settings.timezone)


def _parse_date(value: str | None, tz) -> date:
    """Parse a YYYY-MM-DD anchor, defaulting to today in the local timezone."""
    if not value:
        return utcnow().astimezone(tz).date()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"date must be YYYY-MM-DD, got {value!r}",
        ) from None


def _parse_moment(value: str, field: str, tz) -> datetime:
    """Parse an ISO date or datetime, assuming local time when naive."""
    text = value.strip().replace("Z", "+00:00")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            parsed = None
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field} must be an ISO date (YYYY-MM-DD) or datetime, "
                f"got {value!r}"
            ),
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return to_utc(parsed)


def _bucket_label(bucket: Bucket, window: Window) -> str:
    """Short axis label for a bucket, in local time."""
    if window.bucket_kind == "hour":
        return bucket.start.strftime("%H:%M")
    if window.bucket_kind == "month":
        return bucket.start.strftime("%b")
    if window.range_kind == "week":
        return bucket.start.strftime("%a")
    return str(bucket.start.day)


def _bucket_dict(
    bucket: Bucket, window: Window, imported: bool = False
) -> dict[str, object]:
    totals = bucket.totals
    return {
        "t": bucket.start.isoformat(),
        "label": _bucket_label(bucket, window),
        "solar_kwh": totals.solar_kwh,
        "home_kwh": totals.home_kwh,
        "grid_import_kwh": totals.grid_import_kwh,
        "grid_export_kwh": totals.grid_export_kwh,
        "solar_kw_avg": bucket.average_kw(totals.solar_kwh),
        "home_kw_avg": bucket.average_kw(totals.home_kwh),
        "grid_import_kw_avg": bucket.average_kw(totals.grid_import_kwh),
        "grid_export_kw_avg": bucket.average_kw(totals.grid_export_kwh),
        "sample_count": bucket.sample_count,
        "covered_seconds": bucket.covered_seconds,
        "has_data": (
            bucket.sample_count > 0 or bucket.covered_seconds > 0 or imported
        ),
        # True when the figures come from an imported SunPower monthly
        # report rather than from this app's own readings.
        "imported": imported,
    }


def _totals_dict(
    totals: EnergyTotals, grid_known: bool = True
) -> dict[str, object]:
    """Serialise period totals.

    ``grid_known`` is False when every figure in the period came from a monthly
    report. Those give solar and household totals but no directional split, so
    grid import/export are *unknown* rather than zero -- and the percentages
    derived from them would read a meaningless 100%. Reporting None makes the
    UI show a dash instead of a confident wrong number.
    """
    return {
        "solar_kwh": totals.solar_kwh,
        "home_kwh": totals.home_kwh,
        "grid_known": grid_known,
        "grid_import_kwh": totals.grid_import_kwh if grid_known else None,
        "grid_export_kwh": totals.grid_export_kwh if grid_known else None,
        "self_consumption_pct": (
            totals.self_consumption_pct if grid_known else None
        ),
        "self_sufficiency_pct": (
            totals.self_sufficiency_pct if grid_known else None
        ),
    }


async def _day_buckets(
    store: ReadingStore, settings: Settings, window: Window
) -> list[Bucket]:
    """Hourly buckets integrated straight from raw samples (exact)."""
    readings = await store.query_range(window.start, window.end)
    lead = await store.reading_before(window.start)
    if lead is not None:
        readings = [lead, *readings]
    return integrate_readings(readings, window.edges, settings.max_sample_gap_seconds)


async def _rolled_buckets(
    store: ReadingStore, window: Window
) -> list[tuple[Bucket, bool]]:
    """Coarser buckets from the hourly rollups, backfilled from imports.

    Imported daily totals only fill buckets the poller never covered, so
    measured data always wins where it exists.
    """
    rollups = await store.query_hourly(window.start, window.end)
    buckets = combine_hourly(rollups, window.edges)
    imports = await store.query_daily_imports(
        window.start.date(), window.end.date()
    )
    return merge_daily_imports(buckets, imports)


async def _totals_for_local_day(
    store: ReadingStore, settings: Settings, moment: datetime, tz: tzinfo
) -> EnergyTotals:
    """Energy so far today, for the home view's summary row."""
    local_day = moment.astimezone(tz).date()
    window = build_window("day", local_day, tz)
    return sum_totals(await _day_buckets(store, settings, window))


# -- routes ----------------------------------------------------------------


def register_api_routes(app: FastAPI) -> None:
    @app.get("/api/live")
    async def get_live(
        request: Request,
        tz: str | None = Query(
            None, description="Viewer's IANA timezone; defaults to TIMEZONE."
        ),
    ) -> JSONResponse:
        """Current snapshot: power now, flow directions and today's totals."""
        settings = _settings(request)
        zone, _ = _view_timezone(settings, tz)
        store = _store(request)
        poller = _poller(request)

        reading = poller.latest_reading
        if reading is None:
            reading = await store.latest_reading()

        today: EnergyTotals | None = None
        try:
            today = await _totals_for_local_day(store, settings, utcnow(), zone)
        except Exception as err:  # noqa: BLE001 - live view must still render
            _LOGGER.debug("Could not compute today's totals: %s", err)

        payload = build_live_payload(
            reading=reading,
            gateway_reachable=poller.status.gateway_reachable,
            poll_interval_seconds=settings.poll_interval_seconds,
            today=today,
            implausible=poller.status.implausible_readings,
        )
        return JSONResponse(payload)

    @app.get("/api/history")
    async def get_history(
        request: Request,
        range: Literal["day", "week", "month", "year"] = Query(
            "day", description="Aggregation window"
        ),
        date_param: str | None = Query(
            None,
            alias="date",
            description="Any date inside the window, YYYY-MM-DD. Defaults to today.",
        ),
        tz: str | None = Query(
            None, description="Viewer's IANA timezone; defaults to TIMEZONE."
        ),
    ) -> JSONResponse:
        """Aggregated series for a day, week, month or year."""
        settings = _settings(request)
        zone, zone_name = _view_timezone(settings, tz)
        store = _store(request)

        if range not in VALID_RANGES:
            raise HTTPException(
                status_code=400,
                detail=f"range must be one of {', '.join(VALID_RANGES)}",
            )

        anchor = _parse_date(date_param, zone)
        window = build_window(range, anchor, zone)

        if range == "day":
            pairs = [
                (bucket, False)
                for bucket in await _day_buckets(store, settings, window)
            ]
        else:
            pairs = await _rolled_buckets(store, window)

        totals = sum_totals([bucket for bucket, _ in pairs])
        points = [
            _bucket_dict(bucket, window, imported) for bucket, imported in pairs
        ]
        has_data = any(point["has_data"] for point in points)
        # Grid flow is only complete when NOTHING in the period came from a
        # monthly report. A year with four imported months and one measured
        # one would otherwise report September's grid total as the year's.
        grid_known = not any(
            point["imported"] for point in points
        )

        # The reports are daily totals, so the hourly day view can never be
        # filled from them -- but it can still say what the day added up to.
        imported_day_total: dict[str, float | None] | None = None
        if range == "day" and not has_data:
            same_day = await store.query_daily_imports(
                anchor, anchor + timedelta(days=1)
            )
            if same_day:
                row = same_day[0]
                imported_day_total = {
                    "solar_kwh": row.solar_kwh,
                    "home_kwh": row.home_kwh,
                    "source": row.source,
                }

        return JSONResponse(
            {
                "range": range,
                "bucket": window.bucket_kind,
                "date": anchor.isoformat(),
                "label": window.label,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "timezone": zone_name,
                "previous_date": window.previous_anchor.isoformat(),
                "next_date": window.next_anchor.isoformat(),
                "is_current_period": window.start
                <= utcnow().astimezone(zone)
                < window.end,
                "has_data": has_data,
                "points": points,
                "totals": _totals_dict(totals, grid_known),
                "imported_points": sum(
                    1 for point in points if point["imported"]
                ),
                "imported_day_total": imported_day_total,
            }
        )

    @app.get("/api/export")
    async def get_export(
        request: Request,
        from_param: str = Query(
            ..., alias="from", description="ISO date or datetime, inclusive"
        ),
        to_param: str = Query(
            ..., alias="to", description="ISO date or datetime, exclusive"
        ),
        format: Literal["json", "csv"] = Query(
            "json", description="Output format"
        ),
        tz: str | None = Query(
            None, description="Zone for naive from/to; defaults to TIMEZONE."
        ),
    ):
        """Dump raw readings, for the eventual one-time load into Azure SQL."""
        settings = _settings(request)
        store = _store(request)
        zone, _ = _view_timezone(settings, tz)

        start = _parse_moment(from_param, "from", zone)
        end = _parse_moment(to_param, "to", zone)
        if end <= start:
            raise HTTPException(status_code=400, detail="'to' must be after 'from'")
        if end - start > timedelta(days=EXPORT_MAX_DAYS):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"range is longer than {EXPORT_MAX_DAYS} days; export in "
                    "smaller slices"
                ),
            )

        readings = await store.query_range(start, end)

        if format == "csv":
            filename = (
                f"solar-readings-{start.date().isoformat()}-to-"
                f"{end.date().isoformat()}.csv"
            )

            def rows() -> Iterator[str]:
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                writer.writerow(
                    [
                        "timestamp_utc",
                        "epoch_seconds",
                        "solar_kw",
                        "home_kw",
                        "grid_kw",
                        "grid_direction",
                        "source",
                        "solar_kwh_lifetime",
                        "home_kwh_lifetime",
                        "grid_net_kwh_lifetime",
                        "grid_scale",
                    ]
                )
                for reading in readings:
                    writer.writerow(
                        [
                            reading.timestamp.isoformat(),
                            int(reading.timestamp.timestamp()),
                            reading.solar_kw,
                            reading.home_kw,
                            reading.grid_kw,
                            reading.grid_direction,
                            reading.source,
                            reading.solar_kwh_lifetime,
                            reading.home_kwh_lifetime,
                            reading.grid_net_kwh_lifetime,
                            reading.grid_scale,
                        ]
                    )
                    if buffer.tell() > 64 * 1024:
                        yield buffer.getvalue()
                        buffer.seek(0)
                        buffer.truncate(0)
                yield buffer.getvalue()

            return StreamingResponse(
                rows(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        return JSONResponse(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "count": len(readings),
                "readings": [
                    {
                        "timestamp": reading.timestamp.isoformat(),
                        "epoch_seconds": int(reading.timestamp.timestamp()),
                        "solar_kw": reading.solar_kw,
                        "home_kw": reading.home_kw,
                        "grid_kw": reading.grid_kw,
                        "grid_direction": reading.grid_direction,
                        "source": reading.source,
                        "solar_kwh_lifetime": reading.solar_kwh_lifetime,
                        "home_kwh_lifetime": reading.home_kwh_lifetime,
                        "grid_net_kwh_lifetime": reading.grid_net_kwh_lifetime,
                        "grid_scale": reading.grid_scale,
                    }
                    for reading in readings
                ],
            }
        )

    @app.get("/api/status")
    async def get_status(request: Request) -> JSONResponse:
        """Service health: poller, jobs, storage and configuration."""
        settings = _settings(request)
        store = _store(request)
        poller = _poller(request)
        state = request.app.state

        stats = await store.stats()
        return JSONResponse(
            {
                "version": __version__,
                "started_at": state.started_at.isoformat(),
                "server_time": utcnow().isoformat(),
                "poller": poller.status.as_dict(),
                "gateway": {
                    "host": settings.pvs_host,
                    # Masked: the last 5 characters are the API password.
                    "serial_number": mask_serial(
                        poller.client.serial_number if poller.client else None
                    ),
                    "last_source": poller.status.last_source,
                },
                "storage": {
                    "reading_count": stats.reading_count,
                    "first_timestamp": (
                        stats.first_timestamp.isoformat()
                        if stats.first_timestamp
                        else None
                    ),
                    "last_timestamp": (
                        stats.last_timestamp.isoformat()
                        if stats.last_timestamp
                        else None
                    ),
                    "size_bytes": stats.size_bytes,
                    "latest_rollup_hour": (
                        h.isoformat()
                        if (h := await store.latest_hourly_hour())
                        else None
                    ),
                },
                "tasks": [
                    state.rollup_task.as_dict(),
                    state.backup_task.as_dict(),
                ],
                "backups": state.backups.describe(),
                "config": settings.describe(),
            }
        )

    @app.post("/api/admin/rollup")
    async def post_rollup(
        request: Request,
        full: bool = Query(False, description="Rebuild every hour from scratch"),
    ) -> JSONResponse:
        """Force a rollup refresh. Handy after importing or editing history."""
        settings = _settings(request)
        store = _store(request)
        written = await refresh_hourly(store, settings, full=full)
        return JSONResponse({"hours_written": written, "full": full})

    @app.post("/api/admin/backup")
    async def post_backup(request: Request) -> JSONResponse:
        """Take a backup immediately rather than waiting for the daily run."""
        result = await request.app.state.backups.run()
        if result is None:
            raise HTTPException(
                status_code=409,
                detail="Backups are disabled or unsupported by the active store",
            )
        return JSONResponse(
            {"path": str(result.path), "size_bytes": result.size_bytes}
        )

    @app.get("/api/health")
    async def get_health(request: Request) -> JSONResponse:
        """Cheap liveness probe that never touches the gateway."""
        return JSONResponse(
            {
                "status": "ok",
                "poller_running": _poller(request).status.running,
                "server_time": utcnow().isoformat(),
            }
        )


# -- static frontend -------------------------------------------------------


def mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built frontend from the same process as the API.

    Registered as a catch-all rather than a StaticFiles mount so unknown paths
    fall back to index.html (client-side routing) while /api/* keeps 404ing as
    JSON.
    """
    static_dir = settings.static_dir
    index_file = static_dir / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Unknown API endpoint")

        if not index_file.is_file():
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "The frontend has not been built yet. Run `npm install && "
                        "npm run build` in frontend/, or use the Vite dev server "
                        "on port 5173."
                    ),
                    "expected_at": str(static_dir),
                },
            )

        candidate = (static_dir / full_path).resolve() if full_path else index_file
        try:
            candidate.relative_to(static_dir.resolve())
        except ValueError:
            # Path traversal attempt; fall back to the app shell.
            candidate = index_file
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file)


app = create_app()
