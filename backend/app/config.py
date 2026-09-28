"""Configuration loading.

Values come from the process environment, with a ``.env`` file in the project
root used as a fallback for anything not already set. The ``.env`` parser here
is deliberately tiny so the backend does not need python-dotenv.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from functools import lru_cache
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# backend/app/config.py -> backend/app -> backend -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file without overriding real env vars."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        _LOGGER.debug("No .env file at %s; relying on the environment", env_path)
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # Strip matching quotes; otherwise drop a trailing inline comment.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw))
    except ValueError:
        _LOGGER.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        _LOGGER.warning("%s=%s is below the minimum %s; clamping", name, value, minimum)
        return minimum
    return value


def _float(
    name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOGGER.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        _LOGGER.warning(
            "%s=%s is outside %s..%s; using %s", name, value, minimum, maximum, default
        )
        return default
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSEY:
        return False
    _LOGGER.warning("%s=%r is not a boolean; using %s", name, raw, default)
    return default


def _resolve(path_value: str) -> Path:
    """Resolve a configured path, treating relative paths as project-relative."""
    path = Path(path_value.strip().strip('"').strip("'")).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _resolve_timezone(name: str) -> tzinfo:
    """Return the tzinfo for an IANA name, falling back to the system zone."""
    system_zone = datetime.now().astimezone().tzinfo
    if not name:
        return system_zone
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as err:  # noqa: BLE001 - any failure means fall back
        _LOGGER.warning(
            "TIMEZONE=%r could not be loaded (%s); using the system timezone. "
            "On Windows, `pip install tzdata` provides the IANA database.",
            name,
            err,
        )
        return system_zone


def mask_serial(serial: str | None) -> str | None:
    """Hide the last 5 characters of a PVS serial.

    Those five characters *are* the gateway's local API password, so the full
    serial must never appear in an API response or a log line.
    """
    if not serial:
        return serial
    if len(serial) <= 5:
        return "*" * len(serial)
    return serial[:-5] + "*" * 5


@dataclass(frozen=True)
class Settings:
    """Immutable view of the app's configuration."""

    pvs_host: str
    pvs_serial: str
    poll_interval_seconds: int
    poll_backoff_min_seconds: int
    poll_backoff_max_seconds: int
    request_timeout_seconds: int
    meter_consumption_mode: str
    grid_scale: float
    db_path: Path
    discovery_cache_path: Path
    max_sample_gap_seconds: int
    backup_enabled: bool
    backup_dir: Path
    backup_keep_days: int
    api_host: str
    api_port: int
    static_dir: Path
    log_level: str
    timezone_name: str
    timezone: tzinfo = field(compare=False)

    @property
    def pvs_password(self) -> str:
        """Local API password: the last 5 characters of the PVS serial number."""
        return self.pvs_serial[-5:] if self.pvs_serial else ""

    def describe(self) -> dict[str, object]:
        """Config summary safe to log or expose on /api/status (no password)."""
        return {
            "pvs_host": self.pvs_host,
            "pvs_serial": mask_serial(self.pvs_serial),
            "poll_interval_seconds": self.poll_interval_seconds,
            "meter_consumption_mode": self.meter_consumption_mode,
            "grid_scale": self.grid_scale,
            "db_path": str(self.db_path),
            "max_sample_gap_seconds": self.max_sample_gap_seconds,
            "backup_enabled": self.backup_enabled,
            "backup_dir": str(self.backup_dir),
            "backup_keep_days": self.backup_keep_days,
            "static_dir": str(self.static_dir),
            "timezone": self.timezone_name or str(self.timezone),
        }


def build_settings() -> Settings:
    """Read the environment (and .env) into a Settings object."""
    load_dotenv()

    backoff_min = _int("POLL_BACKOFF_MIN_SECONDS", 5, minimum=1)
    backoff_max = _int("POLL_BACKOFF_MAX_SECONDS", 300, minimum=backoff_min)

    mode = _str("METER_CONSUMPTION_MODE", "net").strip().lower()
    if mode not in {"net", "load"}:
        _LOGGER.warning("METER_CONSUMPTION_MODE=%r is not net|load; using 'net'", mode)
        mode = "net"

    timezone_name = _str("TIMEZONE", "").strip()

    return Settings(
        pvs_host=_str("PVS_HOST", "").strip(),
        pvs_serial=_str("PVS_SN", "").strip(),
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60, minimum=5),
        poll_backoff_min_seconds=backoff_min,
        poll_backoff_max_seconds=backoff_max,
        request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 30, minimum=5),
        meter_consumption_mode=mode,
        grid_scale=_float("GRID_SCALE", 1.0, 0.05, 20.0),
        db_path=_resolve(_str("DB_PATH", "data/solar.db")),
        discovery_cache_path=_resolve(
            _str("DISCOVERY_CACHE_PATH", "data/gateway-location.json")
        ),
        max_sample_gap_seconds=_int("MAX_SAMPLE_GAP_SECONDS", 300, minimum=1),
        backup_enabled=_bool("BACKUP_ENABLED", True),
        backup_dir=_resolve(_str("BACKUP_DIR", "data/backups")),
        backup_keep_days=_int("BACKUP_KEEP_DAYS", 14, minimum=1),
        api_host=_str("API_HOST", "0.0.0.0"),
        api_port=_int("API_PORT", 8000, minimum=1),
        static_dir=_resolve(_str("STATIC_DIR", "frontend/dist")),
        log_level=_str("LOG_LEVEL", "INFO").strip().upper(),
        timezone_name=timezone_name,
        timezone=_resolve_timezone(timezone_name),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings, so every module sees the same configuration."""
    return build_settings()
