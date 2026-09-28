"""Correcting the gateway's miscalibrated net/consumption channel.

This site's consumption CTs read almost exactly 2x high (see AGENTS.md rule 9),
so the raw net power overstates both import and export. ``GRID_SCALE`` is the
multiplier that corrects it. Everything that applies that correction goes
through this module -- the live client, the history rewrite and the report
importer -- so the three can never disagree about the arithmetic.

The one identity everything rests on: the gateway computes house load as
``site_load = pv + net``. So the net channel is the only faulty input, and the
house load is always re-derived from the corrected net rather than read back.

Every stored reading and imported day records the scale it was stored under,
which makes a later recalibration exact: ``raw = stored / old_scale``.
"""

from __future__ import annotations

from dataclasses import replace

from .models import DailyImport, Reading, grid_direction_for

#: House load below this (kW) is physically impossible and means the net
#: channel is miscalibrated. Small negatives are just meter noise near zero.
IMPLAUSIBLE_HOME_KW = -0.05


def normalise(
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

    implausible = home_kw < IMPLAUSIBLE_HOME_KW
    if implausible:
        # Match what the SunStrong app puts on screen rather than showing a
        # negative house load. The flag is what tells the truth about it.
        home_kw = abs(home_kw)
    return solar_kw, home_kw, grid_kw, implausible


def rescale_reading(reading: Reading, grid_scale: float) -> Reading:
    """Re-express a stored reading under a different GRID_SCALE.

    Exact, because the raw net power is recoverable from what was stored:
    ``raw = grid_kw / reading.grid_scale``. The stored ``home_kw`` is not used
    -- it may be the magnitude of an impossible negative -- and is re-derived
    from ``pv + net`` exactly as the gateway does. Lifetime counters are the
    gateway's own and are left untouched.
    """
    if grid_scale == reading.grid_scale:
        return reading
    raw_grid = reading.grid_kw / reading.grid_scale
    solar, home, grid, _ = normalise(
        reading.solar_kw, reading.solar_kw + raw_grid, raw_grid, grid_scale
    )
    return replace(
        reading,
        solar_kw=round(solar, 4),
        home_kw=round(home, 4),
        grid_kw=round(grid, 4),
        grid_direction=grid_direction_for(grid),
        grid_scale=grid_scale,
    )


def corrected_daily_home(
    solar_kwh: float | None,
    reported_home_kwh: float | None,
    grid_scale: float,
) -> float | None:
    """House use for one report day, with the net channel corrected.

    SunPower's "Energy Used" is ``produced + net`` with the same faulty net
    channel, so the net part is ``used - produced`` and scales like live power.
    Returns None when the answer is still impossible (negative) or cannot be
    computed, so a gap is shown rather than a fabricated number.
    """
    if reported_home_kwh is None:
        return None
    if solar_kwh is None:
        # Without production the net part cannot be separated out; only an
        # uncorrected figure could be given, and that one is known to be wrong.
        return reported_home_kwh if grid_scale == 1.0 and reported_home_kwh >= 0 else None
    home = solar_kwh + (reported_home_kwh - solar_kwh) * grid_scale
    if home < 0:
        return None
    return round(home, 3)


def rescale_daily_import(row: DailyImport, grid_scale: float) -> DailyImport:
    """Re-derive an imported day's house use under a different GRID_SCALE."""
    return replace(
        row,
        home_kwh=corrected_daily_home(
            row.solar_kwh, row.home_kwh_reported, grid_scale
        ),
        grid_scale=grid_scale,
    )
