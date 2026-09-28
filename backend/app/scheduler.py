"""A minimal periodic background task.

Used for the rollup refresh and the daily database backup. Deliberately not a
scheduler library: the app needs "run this coroutine every N seconds, survive
its exceptions, and stop promptly on shutdown" and nothing more.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from .models import utcnow

_LOGGER = logging.getLogger(__name__)


class PeriodicTask:
    """Runs a coroutine on an interval until stopped."""

    def __init__(
        self,
        name: str,
        interval_seconds: float,
        action: Callable[[], Awaitable[object]],
        run_immediately: bool = True,
    ) -> None:
        self.name = name
        self.interval_seconds = float(interval_seconds)
        self._action = action
        self._run_immediately = run_immediately
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None
        self.run_count = 0
        self.failure_count = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name=self.name)
        _LOGGER.info("Task %s started (every %ss)", self.name, self.interval_seconds)

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        _LOGGER.info("Task %s stopped", self.name)

    async def run_now(self) -> None:
        """Run the action once, outside the schedule. Exceptions propagate."""
        await self._action()
        self.last_run_at = utcnow()
        self.run_count += 1

    async def _loop(self) -> None:
        if not self._run_immediately:
            if await self._sleep():
                return
        while not self._stop.is_set():
            try:
                await self._action()
                self.last_run_at = utcnow()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - a task failure is not fatal
                self.failure_count += 1
                self.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.warning("Task %s failed: %s", self.name, self.last_error)
            self.run_count += 1
            if await self._sleep():
                return

    async def _sleep(self) -> bool:
        """Wait for the interval. Returns True if we were asked to stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
        except asyncio.TimeoutError:
            return False
        return True

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
        }
