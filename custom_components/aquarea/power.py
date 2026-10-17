"""Average power derived from the cloud's cumulative daily energy.

The Panasonic cloud has no instantaneous power. It publishes the energy used so
far in the cloud's (UTC) day, in kWh with three decimals, some 15-30 minutes
after the fact. Two consecutive fetches of that figure give the energy used in
between, so (kWh now - kWh before) / time between the fetches is the average
power over that interval. It is a smoothed, lagging figure, not a live reading:
the cloud publishes in steps, so an interval can read 0 W and the next one the
energy of both.

This module is plain Python (no Home Assistant imports) so the arithmetic can
be tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import aioaquarea

# The cloud reports kWh with three decimals: a fall of at most one step is
# rounding noise, anything larger is the cloud's counter starting over.
NOISE_KWH = 0.001

# A previous reading older than this many consumption intervals is too far
# back to say anything about the present: a fetch was missed (or several).
MAX_GAP_INTERVALS = 2

CONSUMPTION_TYPES = (
    aioaquarea.ConsumptionType.TOTAL,
    aioaquarea.ConsumptionType.HEAT,
    aioaquarea.ConsumptionType.COOL,
    aioaquarea.ConsumptionType.WATER_TANK,
)


@dataclass(frozen=True)
class EnergyReading:
    """The energy of one cloud day at the moment it was fetched."""

    day: date
    kwh: float
    fetched_at: datetime


def average_power(
    previous: EnergyReading | None,
    current: EnergyReading | None,
    interval: timedelta,
) -> float | None:
    """Return the average power in W between two readings, or None if unknown.

    None when there is no earlier reading or no current one, when the readings
    are more than MAX_GAP_INTERVALS consumption intervals apart, when the
    cloud's day changed, or when the day's energy dropped by more than noise
    (the counter was reset): the interval cannot be measured then. A drop of
    at most NOISE_KWH counts as no change, so the result is never negative.
    """
    if previous is None or current is None:
        return None
    elapsed = current.fetched_at - previous.fetched_at
    if elapsed <= timedelta(0) or elapsed > MAX_GAP_INTERVALS * interval:
        return None
    if current.day != previous.day:
        return None
    delta = current.kwh - previous.kwh
    if delta < -NOISE_KWH - 1e-9:  # float slack: 0.348 - 0.347 is a hair over
        return None
    return max(delta, 0.0) * 1000 / (elapsed.total_seconds() / 3600)


def daily_energy(
    month_consumption: Iterable[Any] | None, day: date
) -> dict[aioaquarea.ConsumptionType, float] | None:
    """Return the energy of a cloud day by consumption type, or None.

    Reads the monthly records the way the daily energy sensors do: the entry
    whose data_time is the day. None when the cloud has not published the day
    (it can be hours late after midnight).
    """
    for entry in month_consumption or []:
        dt_str = entry.data_time
        if not dt_str:
            continue
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                entry_date = datetime.strptime(dt_str, fmt).date()
            except ValueError:
                continue
            break
        else:
            continue
        if entry_date != day:
            continue
        heat = float(entry.heat_consumption or 0.0)
        cool = float(entry.cool_consumption or 0.0)
        tank = float(entry.tank_consumption or 0.0)
        try:
            total = float(entry.total_consumption or 0.0)
        except (ValueError, TypeError):
            total = heat + cool + tank
        return {
            aioaquarea.ConsumptionType.TOTAL: total,
            aioaquarea.ConsumptionType.HEAT: heat,
            aioaquarea.ConsumptionType.COOL: cool,
            aioaquarea.ConsumptionType.WATER_TANK: tank,
        }
    return None
