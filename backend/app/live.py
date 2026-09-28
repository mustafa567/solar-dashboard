"""Shaping the "right now" snapshot for the home view.

Turns one ``Reading`` into the flow arrows and status sentence the SunStrong
Connect home screen shows, so the frontend renders a decision the backend
already made rather than re-deriving the source mix in JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import EnergyTotals, GridDirection, Reading, utcnow

#: Below this, solar is treated as not contributing (night, or pre-dawn noise).
SOLAR_ACTIVE_KW = 0.05
#: Below this, the house is treated as drawing essentially nothing.
HOME_ACTIVE_KW = 0.05


@dataclass(frozen=True, slots=True)
class PowerFlows:
    """How much power is moving along each leg of the solar/home/grid triangle."""

    solar_to_home_kw: float
    solar_to_grid_kw: float
    grid_to_home_kw: float

    def as_dict(self) -> dict[str, float]:
        return {
            "solar_to_home_kw": self.solar_to_home_kw,
            "solar_to_grid_kw": self.solar_to_grid_kw,
            "grid_to_home_kw": self.grid_to_home_kw,
        }


def compute_flows(reading: Reading) -> PowerFlows:
    """Split the reading into the three flows the home view draws.

    For a PV-only site the split is unambiguous: solar covers as much of the
    house as it can, any surplus goes to the grid, and any shortfall comes from
    it.
    """
    solar = max(0.0, reading.solar_kw)
    home = max(0.0, reading.home_kw)
    solar_to_home = min(solar, home)
    return PowerFlows(
        solar_to_home_kw=round(solar_to_home, 3),
        solar_to_grid_kw=round(max(0.0, solar - home), 3),
        grid_to_home_kw=round(max(0.0, home - solar), 3),
    )


def status_line(reading: Reading | None, gateway_reachable: bool) -> str:
    """The one-sentence summary of what is powering the house right now."""
    if reading is None:
        return "Waiting for the first reading from your gateway"

    flows = compute_flows(reading)
    solar_active = reading.solar_kw > SOLAR_ACTIVE_KW
    home_active = reading.home_kw > HOME_ACTIVE_KW
    prefix = "" if gateway_reachable else "Last known: "

    if solar_active and flows.solar_to_grid_kw > SOLAR_ACTIVE_KW:
        if flows.solar_to_home_kw > HOME_ACTIVE_KW:
            return f"{prefix}Your solar is powering your home and exporting the extra"
        return f"{prefix}Your solar is exporting to the grid"
    if solar_active and flows.grid_to_home_kw > HOME_ACTIVE_KW:
        return f"{prefix}Your solar and the grid are powering your home"
    if solar_active:
        return f"{prefix}Your solar is powering your home"
    if flows.grid_to_home_kw > HOME_ACTIVE_KW:
        return f"{prefix}The grid is powering your home"
    if not home_active:
        return f"{prefix}Your home is using almost no power"
    return f"{prefix}The grid is powering your home"


#: Shown when the gateway's own numbers cannot be reconciled. Kept short and
#: concrete: it says what is wrong and what to do, not that something failed.
METER_WARNING = (
    "These figures do not balance: the gateway reports more export than it "
    "generates. Your consumption CT looks miscalibrated -- see GRID_SCALE in "
    "the README. Lifetime energy totals are unaffected."
)


def build_live_payload(
    reading: Reading | None,
    gateway_reachable: bool,
    poll_interval_seconds: int,
    today: EnergyTotals | None = None,
    now: datetime | None = None,
    implausible: bool = False,
) -> dict[str, object]:
    """Assemble the /api/live response body.

    ``stale`` marks a reading older than three poll intervals, which is what the
    UI uses to dim the numbers instead of presenting an old snapshot as current.
    """
    moment = now or utcnow()
    payload: dict[str, object] = {
        "status_line": status_line(reading, gateway_reachable),
        "gateway_reachable": gateway_reachable,
        "poll_interval_seconds": poll_interval_seconds,
        "server_time": moment.isoformat(),
        "has_data": reading is not None,
        "data_warning": METER_WARNING if implausible else None,
    }

    if reading is None:
        payload.update(
            {
                "timestamp": None,
                "age_seconds": None,
                "stale": True,
                "solar_kw": None,
                "home_kw": None,
                "grid_kw": None,
                "grid_direction": None,
                "source": None,
                "flows": PowerFlows(0.0, 0.0, 0.0).as_dict(),
                "today": _totals_dict(today),
            }
        )
        return payload

    age = max(0.0, (moment - reading.timestamp).total_seconds())
    direction: GridDirection = reading.grid_direction
    payload.update(
        {
            "timestamp": reading.timestamp.isoformat(),
            "age_seconds": round(age, 1),
            "stale": age > poll_interval_seconds * 3,
            "solar_kw": reading.solar_kw,
            "home_kw": reading.home_kw,
            "grid_kw": reading.grid_kw,
            "grid_direction": direction,
            "source": reading.source,
            "flows": compute_flows(reading).as_dict(),
            "today": _totals_dict(today),
        }
    )
    return payload


def _totals_dict(totals: EnergyTotals | None) -> dict[str, float | None] | None:
    if totals is None:
        return None
    return {
        "solar_kwh": totals.solar_kwh,
        "home_kwh": totals.home_kwh,
        "grid_import_kwh": totals.grid_import_kwh,
        "grid_export_kwh": totals.grid_export_kwh,
        "self_consumption_pct": totals.self_consumption_pct,
        "self_sufficiency_pct": totals.self_sufficiency_pct,
    }
