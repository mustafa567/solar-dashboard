"""Daily database backup with rotation.

A stopgap until the data lives in Azure SQL: once a day, ask the store for a
consistent copy of itself and keep the last ``BACKUP_KEEP_DAYS`` of them. The
copy goes through SQLite's online backup API (see ``sqlite_store.backup_to``),
so it is safe to take while the poller is writing -- unlike a plain file copy,
which can catch a torn WAL.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Settings
from .models import utcnow
from .store import ReadingStore

_LOGGER = logging.getLogger(__name__)

BACKUP_PREFIX = "solar-backup-"
BACKUP_SUFFIX = ".db"
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_BACKUP_PATTERN = re.compile(
    rf"^{re.escape(BACKUP_PREFIX)}(\d{{8}}-\d{{6}}){re.escape(BACKUP_SUFFIX)}$"
)


@dataclass
class BackupResult:
    path: Path
    size_bytes: int
    pruned: list[Path]


class BackupManager:
    """Takes and rotates database snapshots."""

    def __init__(self, store: ReadingStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self.last_result: BackupResult | None = None

    @property
    def directory(self) -> Path:
        return self._settings.backup_dir

    async def run(self) -> BackupResult | None:
        """Take one backup and prune old ones. Returns None if disabled."""
        if not self._settings.backup_enabled:
            _LOGGER.debug("Backups are disabled (BACKUP_ENABLED=false)")
            return None

        destination = self.directory / self._filename(utcnow())
        written = await self._store.backup_to(destination)
        if written is None:
            _LOGGER.debug(
                "The active store does not support file backups; skipping"
            )
            return None

        pruned = self.prune()
        try:
            size = written.stat().st_size
        except OSError:
            size = 0
        result = BackupResult(path=written, size_bytes=size, pruned=pruned)
        self.last_result = result
        _LOGGER.info(
            "Database backed up to %s (%.1f MiB); pruned %s old backup(s)",
            written.name,
            size / (1024 * 1024),
            len(pruned),
        )
        return result

    def prune(self) -> list[Path]:
        """Keep the newest ``BACKUP_KEEP_DAYS`` backups, delete the rest."""
        backups = self.existing()
        keep = max(1, self._settings.backup_keep_days)
        doomed = backups[keep:]
        removed: list[Path] = []
        for path in doomed:
            try:
                path.unlink()
                removed.append(path)
            except OSError as err:
                _LOGGER.warning("Could not delete old backup %s: %s", path, err)
        return removed

    def existing(self) -> list[Path]:
        """Backups in the backup directory, newest first."""
        directory = self.directory
        if not directory.is_dir():
            return []
        matches = [
            path
            for path in directory.iterdir()
            if path.is_file() and _BACKUP_PATTERN.match(path.name)
        ]
        return sorted(matches, key=lambda p: p.name, reverse=True)

    def describe(self) -> dict[str, object]:
        """Backup state for /api/status."""
        backups = self.existing()
        return {
            "enabled": self._settings.backup_enabled,
            "directory": str(self.directory),
            "keep_days": self._settings.backup_keep_days,
            "count": len(backups),
            "latest": backups[0].name if backups else None,
            "last_run_size_bytes": (
                self.last_result.size_bytes if self.last_result else None
            ),
        }

    @staticmethod
    def _filename(moment: datetime) -> str:
        return f"{BACKUP_PREFIX}{moment.strftime(_TIMESTAMP_FORMAT)}{BACKUP_SUFFIX}"
