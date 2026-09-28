"""End-to-end tests for the REST API, against a temp database and fake gateway."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

import pytest
from conftest import TEST_DAY, TEST_TZ, build_day_readings, make_settings
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Reading, utcnow
from app.pvs_client import GatewayError
from app.sqlite_store import SqliteReadingStore


class FakeGateway:
    """Stands in for PVSGatewayClient: no network, scriptable failures."""

    def __init__(self, reading: Reading | None = None, error: Exception | None = None):
        self.reading = reading
        self.error = error
        self.read_count = 0
        self.closed = False
        self.serial_number = "ZT999999999999A9999"
        self.last_source = "livedata"

    @property
    def connected(self) -> bool:
        return not self.closed

    async def read(self) -> Reading:
        self.read_count += 1
        if self.error is not None:
            raise self.error
        if self.reading is not None:
            return self.reading
        return Reading.create(utcnow(), 4.2, 1.6, -2.6, source="livedata")

    async def close(self) -> None:
        self.closed = True


def build_client(
    settings: Settings,
    gateway: FakeGateway | None = None,
) -> TestClient:
    app = create_app(settings=settings, store=SqliteReadingStore(settings.db_path))
    app.state.gateway_client = gateway or FakeGateway()
    return TestClient(app)


@pytest.fixture
def seeded_settings(tmp_path):
    """Settings whose database already holds one full day of samples."""
    import asyncio

    settings = make_settings(tmp_path)

    async def seed() -> None:
        store = SqliteReadingStore(settings.db_path)
        await store.initialize()
        await store.insert_readings(build_day_readings(TEST_DAY))
        await store.close()

    asyncio.run(seed())
    return settings


class TestHealthAndLive:
    def test_health_is_ok_without_a_gateway(self, settings: Settings) -> None:
        with build_client(settings) as client:
            body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["poller_running"] is True

    def test_live_reports_empty_state_before_any_reading(
        self, settings: Settings
    ) -> None:
        gateway = FakeGateway(error=GatewayError("gateway unreachable"))
        with build_client(settings, gateway) as client:
            body = client.get("/api/live").json()

        assert body["has_data"] is False
        assert body["solar_kw"] is None
        assert body["gateway_reachable"] is False
        assert body["status_line"] == "Waiting for the first reading from your gateway"

    def test_live_returns_the_polled_reading_with_flows(
        self, settings: Settings
    ) -> None:
        reading = Reading.create(utcnow(), 5.0, 2.0, -3.0)
        with build_client(settings, FakeGateway(reading=reading)) as client:
            body = client.get("/api/live").json()

        assert body["has_data"] is True
        assert body["solar_kw"] == 5.0
        assert body["home_kw"] == 2.0
        assert body["grid_kw"] == -3.0
        assert body["grid_direction"] == "export"
        assert body["flows"] == {
            "solar_to_home_kw": 2.0,
            "solar_to_grid_kw": 3.0,
            "grid_to_home_kw": 0.0,
        }
        assert "exporting" in body["status_line"]
        assert body["stale"] is False

    def test_live_status_line_when_the_grid_carries_the_house(
        self, settings: Settings
    ) -> None:
        night = Reading.create(utcnow(), 0.0, 1.4, 1.4)
        with build_client(settings, FakeGateway(reading=night)) as client:
            body = client.get("/api/live").json()
        assert body["status_line"] == "The grid is powering your home"
        assert body["grid_direction"] == "import"

    def test_live_includes_todays_running_totals(self, settings: Settings) -> None:
        with build_client(settings) as client:
            body = client.get("/api/live").json()
        assert set(body["today"]) >= {
            "solar_kwh",
            "home_kwh",
            "grid_import_kwh",
            "grid_export_kwh",
            "self_consumption_pct",
        }


class TestHistory:
    def test_day_view_has_hourly_buckets_and_sane_totals(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/history", params={"range": "day", "date": TEST_DAY.isoformat()}
            ).json()

        assert body["range"] == "day"
        assert body["bucket"] == "hour"
        assert len(body["points"]) == 24
        assert body["has_data"] is True

        # The synthetic curve is zero before 06:00 and peaks at noon.
        assert body["points"][3]["solar_kwh"] == 0.0
        assert body["points"][12]["solar_kwh"] > body["points"][8]["solar_kwh"]

        totals = body["totals"]
        # 1 kW flat house load over 24h, minus the last uncovered minute.
        assert totals["home_kwh"] == pytest.approx(24.0, abs=0.05)
        assert totals["solar_kwh"] > 40
        assert 0 <= totals["self_consumption_pct"] <= 100
        assert 0 <= totals["self_sufficiency_pct"] <= 100

    def test_day_view_labels_are_local_clock_times(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/history", params={"range": "day", "date": TEST_DAY.isoformat()}
            ).json()
        assert body["points"][0]["label"] == "00:00"
        assert body["points"][13]["label"] == "13:00"

    def test_empty_range_reports_no_data_rather_than_failing(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/history", params={"range": "day", "date": "2020-01-01"}
            ).json()

        assert body["has_data"] is False
        assert len(body["points"]) == 24
        assert body["totals"]["solar_kwh"] == 0.0
        assert body["totals"]["self_consumption_pct"] is None

    def test_month_view_reads_from_the_rollup_table(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            # Week/month/year are served from hourly rollups, so build them.
            rolled = client.post("/api/admin/rollup", params={"full": "true"}).json()
            assert rolled["hours_written"] > 0

            body = client.get(
                "/api/history", params={"range": "month", "date": TEST_DAY.isoformat()}
            ).json()

        assert body["bucket"] == "day"
        assert len(body["points"]) == 30
        assert body["has_data"] is True
        day_point = next(
            p for p in body["points"] if p["label"] == str(TEST_DAY.day)
        )
        assert day_point["solar_kwh"] > 40

    def test_day_and_month_totals_agree_for_the_same_day(
        self, seeded_settings: Settings
    ) -> None:
        """The raw-integration path and the rollup path must not disagree."""
        with build_client(seeded_settings) as client:
            client.post("/api/admin/rollup", params={"full": "true"})
            day = client.get(
                "/api/history", params={"range": "day", "date": TEST_DAY.isoformat()}
            ).json()
            month = client.get(
                "/api/history", params={"range": "month", "date": TEST_DAY.isoformat()}
            ).json()

        day_in_month = next(
            p for p in month["points"] if p["label"] == str(TEST_DAY.day)
        )
        assert day_in_month["solar_kwh"] == pytest.approx(
            day["totals"]["solar_kwh"], rel=0.01
        )
        assert day_in_month["home_kwh"] == pytest.approx(
            day["totals"]["home_kwh"], rel=0.01
        )

    @pytest.mark.parametrize(
        "range_kind,expected_bucket,expected_points",
        [("week", "day", 7), ("month", "day", 30), ("year", "month", 12)],
    )
    def test_every_range_returns_its_bucket_shape(
        self,
        seeded_settings: Settings,
        range_kind: str,
        expected_bucket: str,
        expected_points: int,
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/history",
                params={"range": range_kind, "date": TEST_DAY.isoformat()},
            ).json()
        assert body["bucket"] == expected_bucket
        assert len(body["points"]) == expected_points

    def test_navigation_dates_are_returned(self, seeded_settings: Settings) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/history", params={"range": "day", "date": "2026-09-27"}
            ).json()
        assert body["previous_date"] == "2026-09-26"
        assert body["next_date"] == "2026-09-28"

    def test_history_defaults_to_today(self, settings: Settings) -> None:
        with build_client(settings) as client:
            body = client.get("/api/history").json()
        assert body["date"] == utcnow().astimezone(TEST_TZ).date().isoformat()

    def test_bad_date_is_rejected(self, settings: Settings) -> None:
        with build_client(settings) as client:
            response = client.get(
                "/api/history", params={"range": "day", "date": "27-09-2026"}
            )
        assert response.status_code == 400
        assert "YYYY-MM-DD" in response.json()["detail"]

    def test_bad_range_is_rejected(self, settings: Settings) -> None:
        with build_client(settings) as client:
            response = client.get("/api/history", params={"range": "decade"})
        assert response.status_code == 422


class TestExport:
    def test_json_export_returns_raw_readings(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/export",
                params={
                    "from": TEST_DAY.isoformat(),
                    "to": (TEST_DAY + timedelta(days=1)).isoformat(),
                },
            ).json()

        assert body["count"] == 1440
        first = body["readings"][0]
        assert set(first) == {
            "timestamp",
            "epoch_seconds",
            "solar_kw",
            "home_kw",
            "grid_kw",
            "grid_direction",
            "source",
            # The gateway's own counters travel with the export, so the later
            # Azure SQL load carries the lifetime counters and the scale too.
            "solar_kwh_lifetime",
            "home_kwh_lifetime",
            "grid_net_kwh_lifetime",
            "grid_scale",
        }

    def test_csv_export_is_parseable_and_has_a_header(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            response = client.get(
                "/api/export",
                params={
                    "from": TEST_DAY.isoformat(),
                    "to": (TEST_DAY + timedelta(days=1)).isoformat(),
                    "format": "csv",
                },
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]

        rows = list(csv.reader(io.StringIO(response.text)))
        assert rows[0] == [
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
        assert len(rows) == 1441  # header + one day of samples

    def test_export_accepts_datetimes_and_respects_bounds(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get(
                "/api/export",
                params={
                    "from": f"{TEST_DAY.isoformat()}T00:00:00",
                    "to": f"{TEST_DAY.isoformat()}T01:00:00",
                },
            ).json()
        assert body["count"] == 60

    def test_reversed_range_is_rejected(self, settings: Settings) -> None:
        with build_client(settings) as client:
            response = client.get(
                "/api/export", params={"from": "2026-09-28", "to": "2026-09-27"}
            )
        assert response.status_code == 400
        assert "after" in response.json()["detail"]

    def test_absurdly_long_range_is_rejected(self, settings: Settings) -> None:
        with build_client(settings) as client:
            response = client.get(
                "/api/export", params={"from": "2000-01-01", "to": "2026-01-01"}
            )
        assert response.status_code == 400
        assert "smaller slices" in response.json()["detail"]

    def test_missing_parameters_are_rejected(self, settings: Settings) -> None:
        with build_client(settings) as client:
            assert client.get("/api/export").status_code == 422


class TestStatusAndAdmin:
    def test_status_reports_poller_storage_and_config(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.get("/api/status").json()

        assert body["poller"]["running"] is True
        # The seeded day plus whatever the live poller has written since.
        assert body["storage"]["reading_count"] >= 1440
        assert body["gateway"]["host"] == "192.0.2.10"
        assert body["config"]["poll_interval_seconds"] == 60
        assert {task["name"] for task in body["tasks"]} == {
            "hourly-rollup",
            "daily-backup",
        }
        # The password must never appear anywhere in the status payload.
        assert "1008" not in str(body["config"])

    def test_status_surfaces_the_poller_error_during_an_outage(
        self, settings: Settings
    ) -> None:
        gateway = FakeGateway(error=GatewayError("connection refused"))
        with build_client(settings, gateway) as client:
            body = client.get("/api/status").json()

        assert body["poller"]["gateway_reachable"] is False
        assert body["poller"]["consecutive_failures"] >= 1
        assert "connection refused" in body["poller"]["last_error"]

    def test_manual_backup_writes_a_rotating_file(
        self, seeded_settings: Settings
    ) -> None:
        with build_client(seeded_settings) as client:
            body = client.post("/api/admin/backup").json()

        written = seeded_settings.backup_dir / body["path"].split("\\")[-1].split("/")[-1]
        assert written.is_file()
        assert body["size_bytes"] > 0
        assert written.name.startswith("solar-backup-")

    def test_backup_can_be_disabled(self, tmp_path) -> None:
        settings = make_settings(tmp_path, backup_enabled=False)
        with build_client(settings) as client:
            assert client.post("/api/admin/backup").status_code == 409


class TestFrontendServing:
    def test_missing_build_returns_a_helpful_message(
        self, settings: Settings
    ) -> None:
        with build_client(settings) as client:
            response = client.get("/")
        assert response.status_code == 503
        assert "has not been built" in response.json()["detail"]

    def test_built_index_is_served_for_unknown_routes(
        self, settings: Settings
    ) -> None:
        settings.static_dir.mkdir(parents=True, exist_ok=True)
        (settings.static_dir / "index.html").write_text("<h1>app</h1>", "utf-8")

        with build_client(settings) as client:
            assert client.get("/").text == "<h1>app</h1>"
            # Client-side route: still the app shell, not a 404.
            assert client.get("/analyze").text == "<h1>app</h1>"

    def test_unknown_api_paths_still_404(self, settings: Settings) -> None:
        with build_client(settings) as client:
            response = client.get("/api/nope")
        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown API endpoint"
