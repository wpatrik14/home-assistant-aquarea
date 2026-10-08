"""Move the energy sensors' hourly statistics onto the hours the energy was used.

The cloud publishes an hour's consumption only after the hour has closed, and
the energy sensors pick it up at the next consumption fetch. Home Assistant's
recorder files a sensor's increase under the hour the state changed in, so a
tank heat-up between 04:00 and 05:00, seen at 05:06, lands in the 05:00-06:00
bucket of the Energy dashboard (two buckets late when the fetch drifts past
the next hour). Daily totals are right; the hourly bars are not.

The cloud's DAY query labels each hour it reports (in UTC, see CLOUD_TIME_ZONE).
`async_redistribute_hourly_statistics` uses those labels to rewrite the sums
of the sensor's own hourly statistics rows, so the Energy dashboard keeps
using the same statistic ids and needs no reconfiguration.

How it stays out of the recorder's way: the recorder keeps the running sum in
its 5-minute statistics and compiles each hourly row once, right after the
hour, from the last 5-minute row of that hour. Nothing reads an hourly row
back. The correction is computed from the 5-minute statistics, which it never
writes, and only rewrites hourly rows before the most recent one; that row
comes out unchanged, so the hours compiled next continue from the same total.

The energy recorded in each hour is matched, oldest first, to the cloud hours
up to it that are still waiting for their energy. Energy only ever moves to
an earlier hour, or stays in its own: the daily figures the sensors read
include the hour in progress, so part of an hour's energy is often recorded
within that hour (seen live on 2026-10-05). Energy the cloud labels but the sensor never
records (a reset cleared it) is never matched, so it does not shift anything
else; energy the sensor records but the cloud does not label stays where it
was recorded.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
import itertools
import logging
from typing import Any, NamedTuple

import aioaquarea
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_import_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

_LOGGER = logging.getLogger(__name__)

HOUR = timedelta(hours=1)

# The time zone of the DAY query's hour labels and dates. The cloud labels
# hours in the offset sent as `osTimezone`, and aioaquarea (1.0.7) always
# sends +00:00: its consumption manager is built with `dt.timezone.utc`, and
# the time zone given to `get_device` doesn't reach it. A test pins this.
CLOUD_TIME_ZONE = dt_util.UTC

# Differences below this are float noise, not a change worth writing.
_EPSILON = 1e-6

# How long after a cloud hour its energy may still be recorded by the sensor,
# unless the consumption interval allows longer (see `max_match_lag`).
_MIN_MATCH_LAG = timedelta(hours=3)


def max_match_lag(consumption_interval: int) -> timedelta:
    """Return how far back recorded energy may be moved, for an interval in minutes.

    The sensor sees an hour's energy at the first consumption fetch after the
    cloud publishes it: about an hour after the hour, plus up to one interval,
    plus the fetch's drift. Energy waiting longer than this is not going to be
    recorded any more, and must not take energy recorded later.
    """
    return max(_MIN_MATCH_LAG, timedelta(minutes=consumption_interval + 120))


def _kwh(value: Any) -> float:
    """Return a consumption field as kWh, or 0 when the cloud sent no number."""
    try:
        return float(value or 0.0)
    except (ValueError, TypeError):
        _LOGGER.debug("Ignoring a non-numeric hourly consumption value %r", value)
        return 0.0


def _consumption_value(
    record: aioaquarea.Consumption, consumption_type: aioaquarea.ConsumptionType
) -> float:
    """Return a record's consumption of one type, in kWh.

    The cloud sometimes sends a field that isn't a number. The total then
    falls back to the sum of the parts, as the energy sensors do, and a part
    that isn't a number counts as 0: this runs in a coordinator listener,
    where an exception would go unhandled.
    """
    if consumption_type == aioaquarea.ConsumptionType.HEAT:
        return _kwh(record.heat_consumption)
    if consumption_type == aioaquarea.ConsumptionType.COOL:
        return _kwh(record.cool_consumption)
    if consumption_type == aioaquarea.ConsumptionType.WATER_TANK:
        return _kwh(record.tank_consumption)
    try:
        return float(record.total_consumption or 0.0)
    except (ValueError, TypeError):
        return (
            _kwh(record.heat_consumption)
            + _kwh(record.cool_consumption)
            + _kwh(record.tank_consumption)
        )


def hourly_consumption(
    records: Iterable[aioaquarea.Consumption],
    consumption_type: aioaquarea.ConsumptionType,
) -> dict[datetime, float]:
    """Map the DAY query's hourly records to {start of the hour in UTC: kWh}.

    The cloud labels each record "YYYYMMDD HH" in CLOUD_TIME_ZONE (UTC), so
    the hours are unambiguous on the days the clocks change.
    """
    hourly: dict[datetime, float] = {}
    for record in records:
        label = record.data_time
        try:
            naive = datetime.strptime(label, "%Y%m%d %H")
        except (TypeError, ValueError):
            _LOGGER.debug("Skipping hourly consumption record labelled %r", label)
            continue
        start = naive.replace(tzinfo=CLOUD_TIME_ZONE)
        hourly[start] = hourly.get(start, 0.0) + _consumption_value(
            record, consumption_type
        )
    return hourly


class _Pending(NamedTuple):
    """A cloud hour whose energy has not all been matched yet."""

    cloud_hour: datetime
    energy: float


class RecordedHour(NamedTuple):
    """The sensor's sum at the end of an hour, from the 5-minute statistics."""

    start: datetime
    sum: float
    reset: bool = False


def _recorded_hours(rows: Iterable[StatisticsRow]) -> list[RecordedHour]:
    """Reduce 5-minute rows to the sum at the end of each hour, as the recorder does.

    An hour is flagged as a reset when the sensor's state dropped in it: a
    month-to-date sensor at the start of a month, a today sensor after
    midnight. Cloud hours from before a reset are not recorded after it.
    """
    hours: dict[datetime, RecordedHour] = {}
    previous_state: float | None = None
    for row in rows:
        if (row_sum := row.get("sum")) is None:
            continue
        start = dt_util.utc_from_timestamp(row["start"]).replace(minute=0)
        reset = start in hours and hours[start].reset
        if (state := row.get("state")) is not None:
            if previous_state is not None and state < previous_state:
                reset = True
            previous_state = state
        hours[start] = RecordedHour(start, row_sum, reset)
    return list(hours.values())


def corrected_sums(
    recorded: Sequence[RecordedHour],
    hourly: Mapping[datetime, float],
    max_lag: timedelta = _MIN_MATCH_LAG,
) -> dict[datetime, float]:
    """Return each recorded hour's sum with the energy moved to the cloud's hours.

    `recorded` are the sums at the end of each hour, oldest first, and
    `hourly` the cloud's consumption per hour, in the same unit. The first
    recorded hour is the base: its sum is kept, and cloud hours up to it are
    left out, as what was recorded before it is not known. The last recorded
    hour always keeps its sum, as all the energy matched by then belongs to
    the hours before it. Energy recorded in an hour is matched only to cloud
    hours that started at most `max_lag` before it; older unmatched hours
    expire, so a cloud hour the sensor never records can't pull back the
    energy of the hours after it.
    """
    if not recorded:
        return {}
    cloud_hours = sorted(start for start in hourly if start > recorded[0].start)
    pending: deque[_Pending] = deque()
    matched: dict[datetime, float] = {}
    unmatched: dict[datetime, float] = {}
    next_cloud = 0
    for previous, hour in itertools.pairwise(recorded):
        # Energy recorded in this hour belongs to it or to the hours before
        # it: the sensors' daily figures include the hour in progress.
        while next_cloud < len(cloud_hours) and cloud_hours[next_cloud] <= hour.start:
            start = cloud_hours[next_cloud]
            if hourly[start] > _EPSILON:
                pending.append(_Pending(start, hourly[start]))
            next_cloud += 1
        # After a reset the sensor counts only what comes next: the hours
        # before it are lost to the sensor, recorded or not.
        if hour.reset:
            pending = deque(item for item in pending if item.cloud_hour >= hour.start)
        while pending and pending[0].cloud_hour < hour.start - max_lag:
            expired = pending.popleft()
            _LOGGER.debug(
                "%.3f kWh labelled for %s was never recorded; not matched",
                expired.energy,
                expired.cloud_hour,
            )
        energy = hour.sum - previous.sum
        while energy > _EPSILON and pending:
            item = pending[0]
            used = min(energy, item.energy)
            matched[item.cloud_hour] = matched.get(item.cloud_hour, 0.0) + used
            energy -= used
            if item.energy - used <= _EPSILON:
                pending.popleft()
            else:
                pending[0] = item._replace(energy=item.energy - used)
        unmatched[hour.start] = energy

    sums = {recorded[0].start: recorded[0].sum}
    total = recorded[0].sum
    matched_hours = sorted(matched)
    next_matched = 0
    for hour in recorded[1:]:
        # The energy of a cloud hour with no recorded hour of its own (Home
        # Assistant was down) goes to the next hour that has one.
        while (
            next_matched < len(matched_hours)
            and matched_hours[next_matched] <= hour.start
        ):
            total += matched[matched_hours[next_matched]]
            next_matched += 1
        total += unmatched[hour.start]
        sums[hour.start] = total
    return sums


def _read(
    hass: HomeAssistant, statistic_id: str, first_hour: datetime
) -> tuple[StatisticMetaData | None, list[StatisticsRow], list[StatisticsRow]]:
    """Read the metadata, and the 5-minute and hourly rows up to the latest hour.

    The rows are read in the statistic's own unit. Left to itself,
    statistics_during_period converts them to the entity's current display
    unit, which can differ (e.g. Wh for a statistic kept in kWh), and the
    sums written back would then be off by that factor.
    """
    last = get_last_statistics(hass, 1, statistic_id, False, {"sum"})
    if not last.get(statistic_id):
        return None, [], []
    end = dt_util.utc_from_timestamp(last[statistic_id][0]["start"]) + HOUR
    if end <= first_hour:
        return None, [], []
    metadata = get_metadata(hass, statistic_ids={statistic_id}).get(statistic_id)
    if metadata is None:
        return None, [], []
    unit = metadata[1].get("unit_of_measurement")
    if unit is None or unit not in EnergyConverter.VALID_UNITS:
        return None, [], []
    units = {EnergyConverter.UNIT_CLASS: unit}
    five_minute, hourly = (
        statistics_during_period(
            hass,
            first_hour,
            end,
            {statistic_id},
            period,
            units,
            {"last_reset", "state", "sum"},
        ).get(statistic_id, [])
        for period in ("5minute", "hour")
    )
    return metadata[1], five_minute, hourly


async def async_redistribute_hourly_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    hourly: Mapping[datetime, float],
    write_from: datetime,
    max_lag: timedelta = _MIN_MATCH_LAG,
) -> int:
    """Rewrite a sensor's past hourly sums to follow the cloud's hour labels.

    `hourly` is the cloud's consumption in kWh per hour (see
    `hourly_consumption`). Only hourly rows from `write_from` on are
    rewritten; the cloud hours before it are context, so that energy
    recorded early in the rewritten hours is matched to them. The most
    recent hourly row is never rewritten.

    Returns the number of rows rewritten. Running it again with the same data
    changes nothing, and running it with revised data corrects the rows again.
    """
    if not hourly:
        return 0
    first_hour = min(hourly) - HOUR
    metadata, five_minute, rows = await get_instance(hass).async_add_executor_job(
        _read, hass, statistic_id, first_hour
    )
    if (
        metadata is None
        or metadata.get("source") != "recorder"
        or not metadata.get("has_sum")
        or len(rows) < 2
    ):
        return 0
    unit = metadata["unit_of_measurement"]
    cloud = {
        start: EnergyConverter.convert(value, UnitOfEnergy.KILO_WATT_HOUR, unit)
        for start, value in hourly.items()
    }
    recorded = _recorded_hours(five_minute)
    if not recorded:
        return 0
    sums = corrected_sums(recorded, cloud, max_lag)
    # The first recorded hour is the base and keeps its raw sum. When the
    # 5-minute rows start after write_from (purged with a short keep_days),
    # writing it would revert a row an earlier run corrected.
    base = recorded[0].start

    changed: list[StatisticData] = []
    for row in rows[:-1]:
        start = dt_util.utc_from_timestamp(row["start"])
        if start < write_from or start <= base:
            continue
        if (new_sum := sums.get(start)) is None:
            continue
        old_sum = row.get("sum")
        if old_sum is not None and abs(new_sum - old_sum) <= _EPSILON:
            continue
        stat = StatisticData(start=start, sum=new_sum)
        if (state := row.get("state")) is not None:
            stat["state"] = state
        if (last_reset := row.get("last_reset")) is not None:
            stat["last_reset"] = dt_util.utc_from_timestamp(last_reset)
        changed.append(stat)
    if changed:
        _LOGGER.debug(
            "Moving the hourly statistics of %s onto the cloud's hours: %s rows",
            statistic_id,
            len(changed),
        )
        async_import_statistics(hass, metadata, changed)
    return len(changed)
