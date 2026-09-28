"""The polling loop that builds up history.

The gateway only exposes current readings, so this loop is the only thing that
creates history: it samples every ``POLL_INTERVAL_SECONDS`` and writes each
sample to the store. It must never die -- a crashed poller means a permanent
hole in the data -- so every failure is caught, logged once per outage, and
retried with exponential backoff between ``POLL_BACKOFF_MIN_SECONDS`` and
``POLL_BACKOFF_MAX_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .models import Reading, utcnow
from .pvs_client import GatewayNotConfigured, PVSGatewayClient
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class PollerStatus:
    """Live health of the poller, surfaced on /api/live and /api/status."""

    running: bool = False
    poll_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    last_source: str | None = None
    #: True while the gateway reports a physically impossible house load.
    implausible_readings: bool = False
    next_poll_at: datetime | None = None
    current_interval_seconds: float = 0.0

    @property
    def gateway_reachable(self) -> bool:
        """True once a reading has landed and nothing has failed since."""
        return self.last_success_at is not None and self.consecutive_failures == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "gateway_reachable": self.gateway_reachable,
            "poll_count": self.poll_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": _iso(self.last_success_at),
            "last_failure_at": _iso(self.last_failure_at),
            "last_error": self.last_error,
            "last_source": self.last_source,
            "implausible_readings": self.implausible_readings,
            "next_poll_at": _iso(self.next_poll_at),
            "current_interval_seconds": round(self.current_interval_seconds, 1),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class Poller:
    """Samples the gateway on an interval and persists every reading."""

    store: ReadingStore
    settings: Settings
    client: PVSGatewayClient | None = None
    status: PollerStatus = field(default_factory=PollerStatus)

    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _latest: Reading | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = PVSGatewayClient(self.settings)

    @property
    def latest_reading(self) -> Reading | None:
        """Most recent in-memory reading, avoiding a DB hit on /api/live."""
        return self._latest

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        # Seed from storage so /api/live has something to show immediately
        # after a restart, before the first poll completes.
        try:
            self._latest = await self.store.latest_reading()
        except Exception as err:  # noqa: BLE001 - never block startup on this
            _LOGGER.debug("Could not seed the latest reading from storage: %s", err)
        self._task = asyncio.create_task(self._run(), name="pvs-poller")
        self.status.running = True
        _LOGGER.info(
            "Poller started: every %ss against %s",
            self.settings.poll_interval_seconds,
            self.settings.pvs_host or "<PVS_HOST unset>",
        )

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        if self.client is not None:
            await self.client.close()
        self.status.running = False
        self.status.next_poll_at = None
        _LOGGER.info("Poller stopped")

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        """Poll until stopped. This coroutine must not raise."""
        while not self._stop.is_set():
            delay = await self._poll_once()
            self.status.current_interval_seconds = delay
            self.status.next_poll_at = utcnow() + timedelta(seconds=delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue  # normal path: the wait elapsed, poll again
        self.status.running = False

    async def _poll_once(self) -> float:
        """Take one sample. Returns how many seconds to wait before the next."""
        assert self.client is not None  # nosec - set in __post_init__
        self.status.poll_count += 1
        try:
            reading = await self.client.read()
        except GatewayNotConfigured as err:
            # Misconfiguration will not fix itself; complain slowly rather than
            # hammering a host that does not exist.
            self._record_failure(err)
            return float(self.settings.poll_backoff_max_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the loop must survive anything
            self._record_failure(err)
            # Drop the session so the next attempt re-authenticates cleanly.
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001
                pass
            return self._backoff_delay()

        try:
            await self.store.insert_reading(reading)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a write failure is not fatal
            self._record_failure(err, stage="storage")
            return self._backoff_delay()

        self._latest = reading
        if self.status.consecutive_failures:
            _LOGGER.info(
                "Gateway recovered after %s consecutive failures",
                self.status.consecutive_failures,
            )
        self.status.consecutive_failures = 0
        self.status.success_count += 1
        self.status.last_success_at = reading.timestamp
        self.status.last_error = None
        self.status.last_source = reading.source
        self.status.implausible_readings = getattr(
            self.client, "implausible_readings", False
        )
        _LOGGER.debug(
            "Reading stored: solar=%.3fkW home=%.3fkW grid=%.3fkW (%s, via %s)",
            reading.solar_kw,
            reading.home_kw,
            reading.grid_kw,
            reading.grid_direction,
            reading.source,
        )
        return float(self.settings.poll_interval_seconds)

    def _record_failure(self, err: Exception, stage: str = "gateway") -> None:
        self.status.failure_count += 1
        self.status.consecutive_failures += 1
        self.status.last_failure_at = utcnow()
        self.status.last_error = f"{type(err).__name__}: {err}"
        # Log loudly the first time, then quietly, so a long outage does not
        # fill the service log with thousands of identical lines.
        if self.status.consecutive_failures == 1:
            _LOGGER.warning("Poll failed (%s): %s", stage, self.status.last_error)
        else:
            _LOGGER.debug(
                "Poll still failing (%s, attempt %s): %s",
                stage,
                self.status.consecutive_failures,
                self.status.last_error,
            )

    def _backoff_delay(self) -> float:
        """Exponential backoff, capped, based on consecutive failures."""
        failures = max(1, self.status.consecutive_failures)
        delay = self.settings.poll_backoff_min_seconds * (2 ** (failures - 1))
        return float(
            min(
                delay,
                self.settings.poll_backoff_max_seconds,
            )
        )
