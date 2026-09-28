"""Thin wrapper around pypvs that yields a single normalised ``Reading``.

Two paths into the gateway's local API are supported, tried in this order:

1. ``/sys/livedata/*`` -- the cheapest read. One ``match=`` query returns
   production, site load and net grid power already in kW. This is what the
   SunStrong app's "right now" screen is built on.
2. ``/sys/devices/meter/*`` -- the built-in revenue-grade meters
   (``p3phsumKw``). Used when livedata reads back empty, which happens on
   firmware where the livedata block is only populated once the telemetry
   subsystem has been enabled.

Field names and units were taken from the pypvs repo's ``doc/LocalAPI.md`` and
``doc/varserver-variables-public-pvs6.csv``:

    /sys/livedata/pv_p          Production Power (kW)
    /sys/livedata/site_load_p   Site Load Power (kW)
    /sys/livedata/net_p         Net Consumption Power (kW), >0 = importing
    /sys/devices/meter/N/p3phsumKw   Sum of 3-phase power (kW)

A note on trusting these values
-------------------------------
Some installations report a net/consumption channel that is miscalibrated (for
example 100 A CTs configured as 200 A, which reads exactly 2x high). The
symptom is a *negative* ``site_load_p``: the gateway computes site load as
``pv_p + net_p``, so an over-large export drives it below zero.

The SunStrong Connect app displays the magnitude of ``site_load_p`` and does not
flag this, so its "home usage" figure looks reasonable while the three numbers
on screen do not balance. This client mirrors that display so the dashboard
agrees with the official app, but marks such a reading ``implausible`` so the
problem is visible rather than hidden. ``GRID_SCALE`` is the knob that actually
corrects it once the true CT ratio is known.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from pypvs.const import VARS_MATCH_METERS
from pypvs.models.livedata import PVSLiveData
from pypvs.models.meter import PVSMeter
from pypvs.pvs import PVS

from .config import Settings
from .discovery import DiscoveryCache, candidate_hosts
from .models import Reading, ReadingSource, utcnow

_LOGGER = logging.getLogger(__name__)

VARS_MATCH_LIVEDATA = "/sys/livedata"

#: Meter serial numbers end in 'p' for the production meter and 'c' for the
#: consumption meter on a PVS6 (e.g. PVS6M20460000p / PVS6M20460000c).
_PRODUCTION_SUFFIX = "p"
_CONSUMPTION_SUFFIX = "c"

#: House load below this (kW) is physically impossible and means the net
#: channel is miscalibrated. Small negatives are just meter noise near zero.
_IMPLAUSIBLE_HOME_KW = -0.05


class GatewayError(RuntimeError):
    """Raised when a usable reading could not be obtained from the gateway."""


class GatewayNotConfigured(GatewayError):
    """Raised when PVS_HOST / PVS_SN are missing from the configuration."""


def _explain(err: BaseException) -> str:
    """Flatten an exception chain into one diagnosable line.

    pypvs wraps transport failures into terse messages ("General error"), which
    is useless when you are trying to work out whether the gateway is off, on a
    different IP, or rejecting the password. The underlying cause is what tells
    you which.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        label = type(current).__name__
        parts.append(f"{label}: {text}" if text else label)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _derive_triple(
    solar_kw: float | None,
    home_kw: float | None,
    grid_kw: float | None,
) -> tuple[float, float, float]:
    """Fill in whichever of solar/home/grid is missing.

    The site energy balance for a PV-only system is ``home = solar + grid``,
    where ``grid`` is signed positive for import. Any two of the three values
    determine the third, so a gateway that reports only two still gives a
    complete picture.
    """
    known = sum(value is not None for value in (solar_kw, home_kw, grid_kw))
    if known < 2:
        raise GatewayError(
            "Gateway returned fewer than two of production / site load / net "
            f"power (solar={solar_kw}, home={home_kw}, grid={grid_kw})"
        )

    if solar_kw is None:
        solar_kw = home_kw - grid_kw  # type: ignore[operator]
    elif home_kw is None:
        home_kw = solar_kw + grid_kw  # type: ignore[operator]
    elif grid_kw is None:
        grid_kw = home_kw - solar_kw

    return float(solar_kw), float(home_kw), float(grid_kw)


def _normalise(
    solar_kw: float,
    home_kw: float,
    grid_kw: float,
    grid_scale: float,
) -> tuple[float, float, float, bool]:
    """Apply GRID_SCALE and flag physically impossible readings.

    Returns (solar, home, grid, implausible). When a scale is configured the
    house load is re-derived, because the gateway computed its own site_load_p
    from the uncorrected net power.
    """
    if grid_scale != 1.0:
        grid_kw *= grid_scale
        home_kw = solar_kw + grid_kw

    implausible = home_kw < _IMPLAUSIBLE_HOME_KW
    if implausible:
        # Match what the SunStrong app puts on screen rather than showing a
        # negative house load. The flag is what tells the truth about it.
        home_kw = abs(home_kw)
    return solar_kw, home_kw, grid_kw, implausible


def _group_meters(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn flat ``/sys/devices/meter/<i>/<field>`` vars into per-meter dicts."""
    grouped: dict[int, dict[str, Any]] = {}
    for key, value in raw.items():
        parts = key.split("/")
        # ['', 'sys', 'devices', 'meter', '<index>', '<field>']
        if len(parts) < 6:
            continue
        try:
            index = int(parts[4])
        except ValueError:
            continue
        grouped.setdefault(index, {})[parts[5]] = value
    return [grouped[index] for index in sorted(grouped)]


def _classify_meters(
    meters: list[PVSMeter],
) -> tuple[PVSMeter | None, PVSMeter | None]:
    """Split meters into (production, consumption) by serial/model suffix."""
    production: PVSMeter | None = None
    consumption: PVSMeter | None = None
    for meter in meters:
        tag = (meter.serial_number or meter.model or "").strip().lower()
        if tag.endswith(_CONSUMPTION_SUFFIX):
            consumption = consumption or meter
        elif tag.endswith(_PRODUCTION_SUFFIX):
            production = production or meter
    # Unrecognised naming: fall back to positional order, which is production
    # first on every PVS6 seen in the wild.
    if production is None and consumption is None and meters:
        production = meters[0]
        if len(meters) > 1:
            consumption = meters[1]
    return production, consumption


class PVSGatewayClient:
    """Connects to a PVS6 and reads one snapshot at a time."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._pvs: PVS | None = None
        self._serial: str | None = None
        #: Which API path last produced a reading, surfaced on /api/status.
        self.last_source: ReadingSource | None = None
        #: True while the gateway is reporting a physically impossible house
        #: load, i.e. the net/consumption channel is miscalibrated.
        self.implausible_readings = False
        self._warned_implausible = False
        self._cache = DiscoveryCache.load(settings.discovery_cache_path)
        #: The address that actually worked, which may not be PVS_HOST.
        self.resolved_host: str | None = self._cache.host

    @property
    def serial_number(self) -> str | None:
        """Serial reported by the gateway, or the configured one as fallback."""
        return self._serial or (self._settings.pvs_serial or None)

    @property
    def connected(self) -> bool:
        return self._pvs is not None

    # -- connection --------------------------------------------------------

    async def connect(self) -> None:
        """Open a session and authenticate against the gateway.

        The local API password is the last 5 characters of the PVS serial.
        ``PVS_SN`` from config is used to authenticate the very first request;
        if ``discover()`` reports a different serial, the gateway's own value
        wins and is used for the session password.
        """
        settings = self._settings
        if not settings.pvs_host:
            raise GatewayNotConfigured("PVS_HOST is not set (see .env.example)")
        if not settings.pvs_serial:
            raise GatewayNotConfigured("PVS_SN is not set (see .env.example)")

        await self.close()

        hosts = await candidate_hosts(settings.pvs_host, self._cache)
        failures: list[str] = []

        for host in hosts:
            try:
                await self._connect_to(host)
            except Exception as err:  # noqa: BLE001 - try the next candidate
                failures.append(f"{host}: {_explain(err)}")
                await self.close()
                continue

            self.resolved_host = host
            self._cache.save(host=host, mac=await self._read_mac())
            if host != settings.pvs_host:
                _LOGGER.info(
                    "PVS_HOST is %s but the gateway answered at %s; remembering "
                    "that for next time. A DHCP reservation on your router (or "
                    "PVS_HOST=pvs.local) makes this permanent.",
                    settings.pvs_host,
                    host,
                )
            return

        raise GatewayError(
            "Could not reach the PVS6 at any known address. Check that the "
            "gateway is powered on and on this network, and that PVS_SN "
            "matches its label. Tried -- " + " | ".join(failures)
        )

    async def _connect_to(self, host: str) -> None:
        """Open a session against one candidate address and authenticate."""
        settings = self._settings
        timeout = aiohttp.ClientTimeout(total=settings.request_timeout_seconds)
        # The PVS6 serves its local API over HTTPS with a self-signed cert;
        # pypvs passes ssl=False per request, so no connector tweaks needed.
        self._session = aiohttp.ClientSession(timeout=timeout)

        password = settings.pvs_password
        pvs = PVS(
            session=self._session,
            host=host,
            user="ssm_owner",
            password=password,
        )

        try:
            await pvs.discover()
            if pvs.serial_number:
                self._serial = pvs.serial_number
                password = pvs.serial_number[-5:]
        except Exception as err:  # noqa: BLE001 - discovery is best-effort
            _LOGGER.debug(
                "Discovery on %s failed (%s); using the configured serial",
                host,
                err,
            )
            self._serial = settings.pvs_serial

        await pvs.setup(auth_password=password)
        self._pvs = pvs
        _LOGGER.info("Connected to PVS6 at %s (serial %s)", host, self.serial_number)

    async def _read_mac(self) -> str | None:
        """Learn the gateway's MAC so ARP can find it after a lease change."""
        if self._pvs is None:
            return None
        try:
            return await self._pvs.getVarserverVar("/sys/info/active_interface_mac")
        except Exception:  # noqa: BLE001 - purely an optimisation
            return None

    async def close(self) -> None:
        self._pvs = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> PVSGatewayClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _note_plausibility(
        self, implausible: bool, solar_kw: float, home_kw: float, grid_kw: float
    ) -> None:
        self.implausible_readings = implausible
        if implausible and not self._warned_implausible:
            self._warned_implausible = True
            _LOGGER.warning(
                "Gateway reported a negative house load (solar=%.2fkW "
                "net=%.2fkW). The net/consumption CT channel is almost "
                "certainly miscalibrated -- compare ctSclFctr against the CT "
                "rating on the clamps, then set GRID_SCALE in .env. Showing "
                "the magnitude (%.2fkW) so the dashboard matches the "
                "SunStrong app meanwhile.",
                solar_kw,
                grid_kw,
                home_kw,
            )
        elif not implausible:
            self._warned_implausible = False

    # -- reading -----------------------------------------------------------

    async def read(self) -> Reading:
        """Fetch one snapshot, preferring livedata and falling back to meters.

        Raises ``GatewayError`` if neither path yields a usable reading; the
        poller catches that and backs off rather than dying.
        """
        if self._pvs is None:
            await self.connect()
        pvs = self._pvs
        assert pvs is not None  # nosec - connect() raises if it cannot set this

        livedata_error: Exception | None = None
        try:
            reading = await self._read_livedata(pvs)
            if reading is not None:
                self.last_source = "livedata"
                return reading
            _LOGGER.debug("Livedata block was empty; falling back to the meters")
        except Exception as err:  # noqa: BLE001 - try the other path before failing
            livedata_error = err
            _LOGGER.debug(
                "Livedata read failed (%s); falling back to the meters",
                _explain(err),
            )

        try:
            reading = await self._read_meters(pvs)
        except Exception as err:
            raise GatewayError(
                "Both gateway read paths failed. "
                f"livedata: {_explain(livedata_error) if livedata_error else 'empty'}"
                f" | meters: {_explain(err)}"
            ) from err

        if reading is None:
            raise GatewayError(
                "Gateway reported neither livedata nor meter power values "
                f"(livedata error: {livedata_error})"
            )
        self.last_source = "meters"
        return reading

    async def _read_livedata(self, pvs: PVS) -> Reading | None:
        raw = await pvs.getVarserverVars(VARS_MATCH_LIVEDATA)
        if not raw:
            return None
        live = PVSLiveData.from_varserver(raw)
        if live.pv_p is None and live.site_load_p is None and live.net_p is None:
            return None
        solar_kw, home_kw, grid_kw = _derive_triple(
            live.pv_p, live.site_load_p, live.net_p
        )
        solar_kw, home_kw, grid_kw, implausible = _normalise(
            solar_kw, home_kw, grid_kw, self._settings.grid_scale
        )
        self._note_plausibility(implausible, solar_kw, home_kw, grid_kw)
        return Reading.create(
            timestamp=utcnow(),
            solar_kw=solar_kw,
            home_kw=home_kw,
            grid_kw=grid_kw,
            source="livedata",
            # Stored unscaled and unmodified: these counters are the gateway's
            # own accumulators and match SunPower's monthly report, so they are
            # the trustworthy record even when the power channel is not.
            solar_kwh_lifetime=live.pv_en,
            home_kwh_lifetime=live.site_load_en,
            grid_net_kwh_lifetime=live.net_en,
        )

    async def _read_meters(self, pvs: PVS) -> Reading | None:
        raw = await pvs.getVarserverVars(VARS_MATCH_METERS)
        if not raw:
            return None
        meters = [PVSMeter.from_varserver(data) for data in _group_meters(raw)]
        if not meters:
            return None

        production, consumption = _classify_meters(meters)
        if production is None:
            return None

        solar_kw = float(production.power_3ph_kw)
        if consumption is None:
            # PV-only metering: no consumption CT, so the house load is unknown.
            # Report production with an unknown-but-balanced grid figure rather
            # than inventing a consumption number.
            raise GatewayError(
                "Only a production meter was found; house consumption cannot be "
                "derived. Check that the consumption CTs are installed and that "
                "/sys/livedata is populated."
            )

        measured = float(consumption.power_3ph_kw)
        if self._settings.meter_consumption_mode == "load":
            # The consumption CTs sit on the load side and read total house use.
            home_kw = measured
            grid_kw = home_kw - solar_kw
        else:
            # Default: net metering. The CTs read net grid flow, >0 importing.
            grid_kw = measured
            home_kw = solar_kw + grid_kw

        solar_kw, home_kw, grid_kw, implausible = _normalise(
            solar_kw, home_kw, grid_kw, self._settings.grid_scale
        )
        self._note_plausibility(implausible, solar_kw, home_kw, grid_kw)
        # The meters expose lifetime net energy per meter; the house total is
        # production plus net import, the same identity the gateway uses.
        solar_lifetime = production.net_lte_kwh or None
        grid_lifetime = consumption.net_lte_kwh or None
        home_lifetime = (
            solar_lifetime + grid_lifetime
            if solar_lifetime is not None and grid_lifetime is not None
            else None
        )
        return Reading.create(
            timestamp=utcnow(),
            solar_kw=solar_kw,
            home_kw=home_kw,
            grid_kw=grid_kw,
            source="meters",
            solar_kwh_lifetime=solar_lifetime,
            home_kwh_lifetime=home_lifetime,
            grid_net_kwh_lifetime=grid_lifetime,
        )
