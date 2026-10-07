"""Hourly energy statistics follow the hours the cloud labels the energy with.

The energy sensors see an hour's consumption only at the first fetch after
the hour has closed, so the recorder files it one or two hours late. The
`corrected_sums` tests cover the matching of recorded energy to the cloud's
hours on plain numbers. The recorder tests record a sensor's states and let
the recorder's own compiler build the statistics, then check that the
correction rewrites the hourly rows under the same statistic id, keeps the
total, and leaves the hours compiled afterwards alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import itertools
from unittest.mock import AsyncMock, MagicMock, patch

import aioaquarea
from aioaquarea.core import AquareaClient
from aioaquarea.statistics import DateType
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.aquarea.const import CONF_CONSUMPTION_INTERVAL
from custom_components.aquarea.statistics import (
    CLOUD_TIME_ZONE,
    RecordedHour,
    async_redistribute_hourly_statistics,
    corrected_sums,
    hourly_consumption,
    max_match_lag,
)

STATISTIC_ID = "sensor.heat_pump_tank_accumulated_consumption"
TANK = aioaquarea.ConsumptionType.WATER_TANK


@pytest.fixture(autouse=True)
async def warsaw(hass: HomeAssistant) -> None:
    """Run in Europe/Warsaw, where the cloud's hour labels were verified."""
    await hass.config.async_set_time_zone("Europe/Warsaw")


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_util.get_default_time_zone())


def _utc(day: int, hour: int) -> datetime:
    return dt_util.as_utc(_local(day, hour))


def _cloud(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in UTC, the cloud's time zone for hour labels and dates."""
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_util.UTC)


def _label(day: int, hour: int) -> str:
    """The cloud's label for the local hour starting at day/hour (October)."""
    return dt_util.as_utc(_local(day, hour)).strftime("%Y%m%d %H")


def _record(label: str, heat: float = 0.0, tank: float = 0.0) -> aioaquarea.Consumption:
    return aioaquarea.Consumption(
        {
            "dataTime": label,
            "heatConsumption": heat,
            "coolConsumption": 0,
            "tankConsumption": tank,
        }
    )


# --- Matching recorded energy to the cloud's hours -------------------------


def _recorded(*hours: tuple) -> list[RecordedHour]:
    """Build recorded hours from (day, hour, sum[, reset]) tuples."""
    return [
        RecordedHour(_utc(day, hour), value, *rest) for day, hour, value, *rest in hours
    ]


def _corrected(recorded: list[RecordedHour], hourly: dict) -> dict[tuple, float]:
    sums = corrected_sums(
        recorded, {_utc(*key): value for key, value in hourly.items()}
    )
    return {
        (dt_util.as_local(start).day, dt_util.as_local(start).hour): round(value, 3)
        for start, value in sums.items()
    }


def test_energy_moves_back_to_its_hour() -> None:
    """The 2026-09-01 case: 04-05 energy, recorded at 05:06, moves to 04-05."""
    recorded = _recorded(
        (5, 2, 10.0), (5, 3, 10.0), (5, 4, 10.0), (5, 5, 11.19), (5, 6, 11.19)
    )

    assert _corrected(recorded, {(5, 3): 0.0, (5, 4): 1.19, (5, 5): 0.0}) == {
        (5, 2): 10.0,
        (5, 3): 10.0,
        (5, 4): 11.19,
        (5, 5): 11.19,
        (5, 6): 11.19,
    }


def test_energy_two_hours_late_moves_back_two_hours() -> None:
    """When the fetch has drifted past the next hour, the lag is two hours."""
    recorded = _recorded(
        (5, 2, 10.0), (5, 3, 10.0), (5, 4, 10.0), (5, 5, 10.5), (5, 6, 10.5)
    )

    assert _corrected(recorded, {(5, 3): 0.5})[(5, 3)] == 10.5


def test_energy_is_matched_oldest_hour_first() -> None:
    """One fetch that records two hours at once splits it over both, in order."""
    recorded = _recorded(
        (5, 2, 10.0), (5, 3, 10.0), (5, 4, 10.0), (5, 5, 11.0), (5, 6, 11.0)
    )

    assert _corrected(recorded, {(5, 3): 0.4, (5, 4): 0.6}) == {
        (5, 2): 10.0,
        (5, 3): 10.4,
        (5, 4): 11.0,
        (5, 5): 11.0,
        (5, 6): 11.0,
    }


def test_energy_the_cloud_does_not_label_stays_where_it_was_recorded() -> None:
    """Recorded energy with no cloud hour waiting for it is not moved."""
    recorded = _recorded((5, 2, 10.0), (5, 3, 10.0), (5, 4, 10.4), (5, 5, 11.0))

    assert _corrected(recorded, {(5, 3): 0.6}) == {
        (5, 2): 10.0,
        (5, 3): 10.6,
        (5, 4): 10.6,
        (5, 5): 11.0,
    }


def test_energy_not_recorded_yet_is_not_moved() -> None:
    """Cloud hours ahead of the sensor change nothing; the last hour keeps its sum."""
    recorded = _recorded((5, 2, 10.0), (5, 3, 10.0), (5, 4, 10.0))

    assert _corrected(recorded, {(5, 2): 0.0, (5, 3): 5.0, (5, 4): 5.0}) == {
        (5, 2): 10.0,
        (5, 3): 10.0,
        (5, 4): 10.0,
    }


def test_energy_lost_at_a_reset_does_not_shift_later_hours() -> None:
    """A today sensor loses 23:00-24:00 at midnight; the next day is unaffected.

    The cloud labels 0.7 kWh at 23:00 that the sensor never records: it drops
    to 0 for the new day first. Without the reset, that hour would take the
    energy recorded after midnight, which belongs to 00:00-01:00.
    """
    recorded = _recorded(
        (4, 22, 10.0), (4, 23, 10.0), (5, 0, 10.0, True), (5, 1, 10.3), (5, 2, 10.3)
    )

    assert _corrected(recorded, {(4, 23): 0.7, (5, 0): 0.3}) == {
        (4, 22): 10.0,
        (4, 23): 10.0,
        (5, 0): 10.3,
        (5, 1): 10.3,
        (5, 2): 10.3,
    }


def test_cloud_hours_up_to_the_first_recorded_hour_are_left_out() -> None:
    """What was recorded before the first hour is unknown, so its hours are context.

    The first hour's sum is the base and is kept, so 02:00's 0.5 kWh,
    recorded in 03-04, stays there, and 01:00's 9 kWh is not looked for.
    """
    recorded = _recorded((5, 2, 10.0), (5, 3, 10.5), (5, 4, 10.5))

    assert _corrected(recorded, {(5, 1): 9.0, (5, 2): 0.5}) == {
        (5, 2): 10.0,
        (5, 3): 10.5,
        (5, 4): 10.5,
    }


def test_energy_of_hours_without_a_row_goes_to_the_next_row() -> None:
    """Home Assistant was down 03:00-05:00: no rows to move the energy into."""
    recorded = _recorded((5, 2, 10.0), (5, 5, 11.5), (5, 6, 11.5))

    assert _corrected(recorded, {(5, 2): 0.5, (5, 3): 0.5, (5, 4): 0.5}) == {
        (5, 2): 10.0,
        (5, 5): 11.5,
        (5, 6): 11.5,
    }


def test_a_dip_stays_where_it_was_recorded() -> None:
    """A small drop (cloud revision) is not moved and does not unbalance the total."""
    recorded = _recorded((5, 2, 10.0), (5, 3, 9.9, True), (5, 4, 10.4), (5, 5, 10.4))

    assert _corrected(recorded, {(5, 3): 0.5}) == {
        (5, 2): 10.0,
        (5, 3): 10.4,
        (5, 4): 10.4,
        (5, 5): 10.4,
    }


def test_an_unrecorded_hour_expires_instead_of_pulling_back_later_energy() -> None:
    """Regression for the live run of 2026-10-05/06.

    The cloud's hours held more heating than the sensor ever recorded. With
    no limit, the unmatched remainder of 13:00 waited forever and took every
    kWh recorded afterwards, up to 20 hours later, leaving those hours flat.
    It now expires after the match lag (3 h), and later energy stays with
    the hours it belongs to.
    """
    recorded = _recorded(
        (5, 12, 10.0),
        (5, 13, 10.0),
        (5, 14, 11.0),  # 1.0 of 13:00's 2.0 kWh; the rest is never recorded
        (5, 15, 11.0),
        (5, 16, 11.0),
        (5, 17, 11.0),
        (5, 18, 11.1),  # 17:00's 0.1 kWh
        (5, 19, 11.2),  # 18:00's 0.1 kWh
        (5, 20, 11.2),
    )
    hourly = {(5, 13): 2.0, (5, 17): 0.1, (5, 18): 0.1}

    sums = _corrected(recorded, hourly)

    assert sums[(5, 13)] == 11.0
    assert (sums[(5, 16)], sums[(5, 17)], sums[(5, 18)]) == (11.0, 11.1, 11.2)

    # Without the limit, the old behaviour: 13:00 takes both later 0.1 kWh.
    unlimited = corrected_sums(
        recorded,
        {_utc(*key): value for key, value in hourly.items()},
        timedelta(days=2),
    )
    assert round(unlimited[_utc(5, 13)], 3) == 11.2


def test_the_match_lag_follows_the_consumption_interval() -> None:
    """At least 3 h, and longer when the consumption interval is longer."""
    assert max_match_lag(60) == timedelta(hours=3)
    assert max_match_lag(10) == timedelta(hours=3)
    assert max_match_lag(180) == timedelta(hours=5)


def test_energy_recorded_within_its_own_hour_stays_there() -> None:
    """Part of an hour's energy is often recorded while the hour is in progress.

    Seen live on 2026-10-05: heating rose by 1.325 kWh at 14:11 and by 0.52 kWh
    at 14:39 local, inside the 14:00 hour. The first is the 13:00 hour's
    energy; the second belongs to 14:00 itself. Matching only earlier hours
    sent the 0.52 kWh back to 13:00 and left 14:00 waiting for energy that
    had already been recorded, which then took the next hours' energy.
    """
    recorded = _recorded(
        (5, 12, 10.0),
        (5, 13, 10.0),
        (5, 14, 11.845),  # 13:00's 1.325 kWh at 14:11, 14:00's first 0.52 at 14:39
        (5, 15, 12.145),  # 14:00's last 0.3 kWh at 15:38
        (5, 16, 12.165),  # 15:00's 0.02 kWh
        (5, 17, 12.165),
    )
    hourly = {(5, 13): 1.325, (5, 14): 0.82, (5, 15): 0.02}

    assert _corrected(recorded, hourly) == {
        (5, 12): 10.0,
        (5, 13): 11.325,
        (5, 14): 12.145,
        (5, 15): 12.165,
        (5, 16): 12.165,
        (5, 17): 12.165,
    }


def test_a_reset_keeps_the_hour_it_happens_in() -> None:
    """A reset drops the hours before it, but not the hour it happens in."""
    recorded = _recorded((4, 23, 10.0), (5, 0, 10.2, True), (5, 1, 10.5))

    # 23:00's 0.7 kWh is lost at the reset; 00:00's 0.5 kWh is recorded
    # within 00:00 (0.2) and in 01:00 (0.3), and all of it stays with 00:00.
    assert _corrected(recorded, {(4, 23): 0.7, (5, 0): 0.5}) == {
        (4, 23): 10.0,
        (5, 0): 10.5,
        (5, 1): 10.5,
    }


def test_nothing_recorded_gives_nothing() -> None:
    """An empty recording has no sums."""
    assert corrected_sums([], {_utc(5, 3): 1.0}) == {}


# --- The cloud's hour labels ------------------------------------------------


def test_hourly_consumption_uses_the_utc_hour_labels() -> None:
    """Each record lands on its labelled UTC hour, per consumption type.

    aioaquarea sends `osTimezone: +00:00`, and the cloud labels the hours in
    it. Validated on a live instance on 2026-10-05: the tank heated 04:00-04:20
    Europe/Warsaw (CEST), and the cloud labelled that energy "20261005 02".
    """
    records = [
        _record("20261005 01", heat=0.016),
        _record("20261005 02", heat=0.016, tank=0.711),
        _record("not a label", tank=5),
        _record(None, tank=5),
    ]

    assert hourly_consumption(records, TANK) == {
        _local(5, 3): 0.0,
        _local(5, 4): 0.711,
    }
    total = hourly_consumption(records, aioaquarea.ConsumptionType.TOTAL)
    assert total[_local(5, 4)] == pytest.approx(0.727)
    assert hourly_consumption(records, aioaquarea.ConsumptionType.HEAT)[
        _local(5, 3)
    ] == pytest.approx(0.016)
    assert hourly_consumption(records, aioaquarea.ConsumptionType.COOL)[
        _local(5, 3)
    ] == pytest.approx(0.0)


def test_the_live_2026_10_05_tank_runs_land_on_their_hours() -> None:
    """Replays the live validation of 2026-10-05 (Europe/Warsaw, CEST).

    The tank heated 04:00-04:20 and 11:58-12:35 (direction WATER). The sensor
    recorded 0.711 kWh at 05:04 and 1.1 kWh at 13:10; the cloud labelled them
    "20261005 02" and "20261005 10". Read as local hours, the first version
    moved them to 02-03 and 10-11, two hours early.
    """
    hourly = hourly_consumption(
        [_record("20261005 02", tank=0.711), _record("20261005 10", tank=1.1)], TANK
    )
    recorded = _recorded(
        *((5, hour, 577.327) for hour in range(5)),
        *((5, hour, 578.038) for hour in range(5, 13)),
        *((5, hour, 579.138) for hour in range(13, 15)),
    )

    sums = corrected_sums(recorded, hourly)
    changes = {
        dt_util.as_local(later).hour: round(sums[later] - sums[earlier], 3)
        for earlier, later in itertools.pairwise(sorted(sums))
    }

    assert {hour: kwh for hour, kwh in changes.items() if kwh} == {4: 0.711, 12: 1.1}


def test_a_total_that_is_not_a_number_falls_back_to_the_parts() -> None:
    """A field the cloud sends as a non-number doesn't raise in the listener.

    aioaquarea's `total_consumption` adds the raw fields, so a string in one
    of them raises TypeError there. The total then falls back to the sum of
    the parts, and a part that isn't a number counts as 0.
    """
    records = [
        aioaquarea.Consumption(
            {"dataTime": _label(5, 4), "heatConsumption": "0.5", "tankConsumption": 1.0}
        ),
        aioaquarea.Consumption(
            {"dataTime": _label(5, 5), "heatConsumption": "n/a", "tankConsumption": 0.2}
        ),
    ]

    total = hourly_consumption(records, aioaquarea.ConsumptionType.TOTAL)
    heat = hourly_consumption(records, aioaquarea.ConsumptionType.HEAT)

    assert total == {_local(5, 4): pytest.approx(1.5), _local(5, 5): pytest.approx(0.2)}
    assert heat == {_local(5, 4): pytest.approx(0.5), _local(5, 5): 0.0}


def test_hourly_labels_are_unambiguous_when_the_clocks_change() -> None:
    """UTC labels have no skipped or repeated hour on the days the clocks change.

    On 2026-10-25 Europe/Warsaw has two 02:00 hours; the cloud labels them
    "00" and "01", and each keeps its own energy.
    """
    records = [
        _record("20261025 00", tank=0.2),
        _record("20261025 01", tank=0.3),
    ]

    hourly = hourly_consumption(records, TANK)

    assert hourly == {
        datetime(2026, 10, 25, 0, tzinfo=dt_util.UTC): pytest.approx(0.2),
        datetime(2026, 10, 25, 1, tzinfo=dt_util.UTC): pytest.approx(0.3),
    }
    assert [dt_util.as_local(start).hour for start in hourly] == [2, 2]


async def test_aioaquarea_asks_for_consumption_in_utc() -> None:
    """Tripwire: CLOUD_TIME_ZONE must match the `osTimezone` aioaquarea sends.

    The cloud labels the DAY query's hours in that offset. If a new aioaquarea
    sends another one, the labels move with it and CLOUD_TIME_ZONE must follow.
    """
    response = MagicMock()
    response.json = AsyncMock(return_value={})
    with patch(
        "aioaquarea.api_client.AquareaAPIClient.request",
        AsyncMock(return_value=response),
    ) as request:
        client = AquareaClient(MagicMock(), "user", "not-a-real-password")
        await client.get_device_consumption("long-id", DateType.DAY, "20261005")

    sent = request.await_args.kwargs["json"]["bodyParam"]["osTimezone"]
    assert sent == "+00:00"
    assert CLOUD_TIME_ZONE.utcoffset(None) == timedelta(0)


# --- On the recorder ---------------------------------------------------------


class _Sensor:
    """Records a sensor's states and compiles them with the recorder's own compiler."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        start: datetime,
        unit: str = UnitOfEnergy.KILO_WATT_HOUR,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.next_period = start
        self.attributes = {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": unit,
        }

    async def run_until(self, end: datetime, states: dict[datetime, float]) -> None:
        """Set each state at its time and compile every 5 minutes up to `end`."""
        while self.next_period < end:
            period_end = self.next_period + timedelta(minutes=5)
            for changed_at, value in sorted(states.items()):
                if self.next_period <= changed_at < period_end:
                    self.freezer.move_to(changed_at)
                    self.hass.states.async_set(
                        STATISTIC_ID, str(value), self.attributes
                    )
                    await async_wait_recording_done(self.hass)
            self.freezer.move_to(period_end + timedelta(seconds=10))
            do_adhoc_statistics(self.hass, start=dt_util.as_utc(self.next_period))
            await async_wait_recording_done(self.hass)
            self.next_period = period_end

    async def correct(self, hourly: dict[datetime, float], write_from: datetime) -> int:
        """Run the correction and wait for the recorder to write it."""
        written = await async_redistribute_hourly_statistics(
            self.hass, STATISTIC_ID, hourly, write_from
        )
        await async_wait_recording_done(self.hass)
        return written

    async def hourly_changes(self, period: str = "hour") -> dict[tuple, float]:
        """Return the energy of each hour (or 5 minutes) as the dashboard sees it."""
        rows = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            _local(1, 0),
            None,
            {STATISTIC_ID},
            period,
            None,
            {"change"},
        )
        changes: dict[tuple, float] = {}
        for row in rows.get(STATISTIC_ID, []):
            start = dt_util.as_local(dt_util.utc_from_timestamp(row["start"]))
            key = (start.day, start.hour)
            changes[key] = round(changes.get(key, 0.0) + row["change"], 3)
        return changes


@pytest.fixture
async def sensor_recorder(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> _Sensor:
    """A tank sensor recorded from 2026-10-05 02:00."""
    # The sensor integration's recorder platform compiles the statistics.
    assert await async_setup_component(hass, "sensor", {})
    return _Sensor(hass, freezer, _local(5, 2))


async def test_the_issue_case_on_the_recorder(sensor_recorder: _Sensor) -> None:
    """The 04-05 heat-up recorded at 05:06 moves to 04-05; later hours compile as usual.

    The recorder's own compiler files the 1.19 kWh under 05-06. After the
    correction it is under 04-05, the 06-07 hour is compiled afterwards from
    the 5-minute statistics and continues from the same total, and the
    5-minute statistics are not touched.
    """
    hourly = {_local(5, 3): 0.0, _local(5, 4): 1.19, _local(5, 5): 0.0}
    await sensor_recorder.run_until(
        _local(5, 6), {_local(5, 2): 0.0, _local(5, 5, 6): 1.19}
    )
    assert await sensor_recorder.hourly_changes() == {
        (5, 2): 0.0,
        (5, 3): 0.0,
        (5, 4): 0.0,
        (5, 5): 1.19,
    }

    assert await sensor_recorder.correct(hourly, _local(5, 0)) == 1
    await sensor_recorder.run_until(_local(5, 7), {_local(5, 6, 30): 1.5})

    assert await sensor_recorder.hourly_changes() == {
        (5, 2): 0.0,
        (5, 3): 0.0,
        (5, 4): 1.19,
        (5, 5): 0.0,
        (5, 6): 0.31,
    }
    assert (await sensor_recorder.hourly_changes("5minute"))[(5, 5)] == 1.19

    # Idempotent: the same data again changes nothing.
    assert await sensor_recorder.correct(hourly, _local(5, 0)) == 0


async def test_every_hourly_run_builds_on_the_recorded_sums(
    sensor_recorder: _Sensor,
) -> None:
    """Runs every hour, through a midnight reset that loses an hour, keep correcting.

    Regression: an earlier version anchored each run on a row it had already
    rewritten, so energy the cloud labelled but the sensor never recorded
    became a permanent offset, and the correction stopped for good.
    """
    sensor_recorder.next_period = _local(4, 20)
    # A today sensor: 0.4 kWh for 21-22 recorded at 22:05; 23-24 (0.7) is
    # lost at the midnight reset; 00-01 (0.3) recorded at 01:05; 01-02 (0.2)
    # recorded at 03:01, two hours late.
    states = {
        _local(4, 20): 5.0,
        _local(4, 22, 5): 5.4,
        _local(5, 0, 5): 0.0,
        _local(5, 1, 5): 0.3,
        _local(5, 3, 1): 0.5,
    }
    hourly = {
        _local(4, 20): 0.0,
        _local(4, 21): 0.4,
        _local(4, 22): 0.0,
        _local(4, 23): 0.7,
        _local(5, 0): 0.3,
        _local(5, 1): 0.2,
        _local(5, 2): 0.0,
    }
    for hour in (21, 22, 23, 24, 25, 26, 27, 28):
        await sensor_recorder.run_until(_local(4, 0) + timedelta(hours=hour), states)
        await sensor_recorder.correct(hourly, _local(4, 0))

    assert await sensor_recorder.hourly_changes() == {
        (4, 20): 0.0,
        (4, 21): 0.4,
        (4, 22): 0.0,
        (4, 23): 0.0,
        (5, 0): 0.3,
        (5, 1): 0.2,
        (5, 2): 0.0,
        (5, 3): 0.0,
    }


async def test_rows_before_write_from_are_kept(sensor_recorder: _Sensor) -> None:
    """The context day's own rows are not rewritten, even where they are off."""
    await sensor_recorder.run_until(
        _local(5, 6), {_local(5, 2): 0.0, _local(5, 3, 5): 0.5, _local(5, 5, 6): 1.69}
    )
    hourly = {_local(5, 2): 0.5, _local(5, 4): 1.19}

    assert await sensor_recorder.correct(hourly, _local(5, 4)) == 1

    assert await sensor_recorder.hourly_changes() == {
        (5, 2): 0.0,
        (5, 3): 0.5,
        (5, 4): 1.19,
        (5, 5): 0.0,
    }


async def test_a_sensor_shown_in_wh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The cloud's kWh are converted to the statistic's unit."""
    assert await async_setup_component(hass, "sensor", {})
    recorder = _Sensor(hass, freezer, _local(5, 2), unit=UnitOfEnergy.WATT_HOUR)
    await recorder.run_until(_local(5, 6), {_local(5, 2): 0.0, _local(5, 5, 6): 1190.0})

    assert await recorder.correct({_local(5, 4): 1.19}, _local(5, 0)) == 1

    changes = await recorder.hourly_changes()
    assert (changes[(5, 4)], changes[(5, 5)]) == (1190.0, 0.0)


async def test_nothing_to_do_without_statistics(
    hass: HomeAssistant, sensor_recorder: _Sensor
) -> None:
    """No cloud hours, no statistics, or statistics only before the cloud's hours."""
    hourly = {_local(5, 4): 1.19}

    assert await sensor_recorder.correct({}, _local(5, 0)) == 0
    assert await sensor_recorder.correct(hourly, _local(5, 0)) == 0

    await sensor_recorder.run_until(_local(5, 3), {_local(5, 2): 0.0})
    assert await sensor_recorder.correct(hourly, _local(5, 0)) == 0


async def test_a_statistic_that_is_not_an_energy_sum_is_left_alone(
    hass: HomeAssistant, sensor_recorder: _Sensor
) -> None:
    """A sensor whose unit was changed to something that isn't energy is skipped."""
    sensor_recorder.attributes["unit_of_measurement"] = "kWh-ish"
    sensor_recorder.attributes["device_class"] = None
    await sensor_recorder.run_until(
        _local(5, 6), {_local(5, 2): 0.0, _local(5, 5, 6): 1.19}
    )

    assert await sensor_recorder.correct({_local(5, 4): 1.19}, _local(5, 0)) == 0


# --- Fetching and scheduling --------------------------------------------------


def _day_calls(client: AsyncMock) -> list[str]:
    return [
        call.args[2]
        for call in client.get_device_consumption.await_args_list
        if call.args[1] == DateType.DAY
    ]


async def test_each_hourly_fetch_corrects_every_energy_sensor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The coordinator fetches today and yesterday; each sensor corrects its own statistic.

    The days are the cloud's (UTC): at 01:05 UTC they are 2026-10-04 and -05,
    and after a start the day before yesterday too, as context.
    """
    freezer.move_to(_cloud(5, 1, 5))
    day_records = {
        "20261003": [],
        "20261004": [_record("20261004 21", tank=0.7)],
        "20261005": [_record("20261005 01", heat=0.2)],
    }

    async def consumption(
        long_id: str, aggregation: DateType, date_input: str
    ) -> list[aioaquarea.Consumption]:
        if aggregation == DateType.DAY:
            return day_records[date_input]
        return []

    mock_aquarea_client.get_device_consumption.side_effect = consumption
    with patch(
        "custom_components.aquarea.sensor.async_redistribute_hourly_statistics",
        AsyncMock(return_value=0),
    ) as redistribute:
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert _day_calls(mock_aquarea_client) == ["20261003", "20261004", "20261005"]

    corrected = {call.args[1]: call.args[2:] for call in redistribute.await_args_list}
    # The enabled-by-default energy sensors: heat, tank and total month to date.
    assert set(corrected) == {
        "sensor.heat_pump_heating_accumulated_consumption",
        "sensor.heat_pump_tank_accumulated_consumption",
        "sensor.heat_pump_accumulated_consumption",
    }
    # The day before yesterday is the first cached day, so it is context:
    # rows from the start of yesterday (UTC) on. Matching reaches back 3 h at
    # the default 60-minute interval.
    assert corrected["sensor.heat_pump_tank_accumulated_consumption"] == (
        {_local(4, 23): 0.7, _local(5, 3): 0.0},
        _cloud(4, 0),
        timedelta(hours=3),
    )
    hourly, _, _ = corrected["sensor.heat_pump_accumulated_consumption"]
    assert hourly == {
        _local(4, 23): pytest.approx(0.7),
        _local(5, 3): pytest.approx(0.2),
    }


async def test_three_days_are_cached_and_the_oldest_is_context(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Days are kept as they roll over, up to the day before yesterday."""
    freezer.move_to(_cloud(5, 12))
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    # After a start: the day before yesterday, as context for yesterday.
    assert coordinator.hourly_consumption_first_day == _cloud(3, 0).date()

    for day in (6, 7):
        freezer.move_to(_cloud(day, 1))
        await coordinator.async_refresh()

    assert coordinator.hourly_consumption_first_day == _cloud(5, 0).date()
    with patch(
        "custom_components.aquarea.sensor.async_redistribute_hourly_statistics",
        AsyncMock(return_value=0),
    ) as redistribute:
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert {call.args[3] for call in redistribute.await_args_list} == {_cloud(6, 0)}


async def test_a_failed_hourly_fetch_keeps_the_cached_hours(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A DAY query that fails is logged and skipped, the other day still counts."""
    freezer.move_to(_local(5, 12))

    async def consumption(
        long_id: str, aggregation: DateType, date_input: str
    ) -> list[aioaquarea.Consumption] | None:
        if aggregation == DateType.DAY and date_input == "20261004":
            raise aioaquarea.ClientError("cloud unhappy")
        if aggregation == DateType.DAY:
            return None
        return []

    mock_aquarea_client.get_device_consumption.side_effect = consumption
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    assert coordinator.hourly_consumption == []
    assert coordinator.hourly_consumption_fetched_at == dt_util.now()

    # Yesterday is asked for again at the next fetch, as it never arrived.
    # Once it has, later fetches past the refetch hours ask for today only.
    for expected in (["20261004", "20261005"], ["20261005"]):
        mock_aquarea_client.get_device_consumption.reset_mock()
        mock_aquarea_client.get_device_consumption.side_effect = None
        mock_aquarea_client.get_device_consumption.return_value = []
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
        assert _day_calls(mock_aquarea_client) == expected


async def test_no_correction_when_every_hourly_fetch_failed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With nothing fetched, the sensors have nothing to correct with."""
    freezer.move_to(_local(5, 12))

    async def consumption(
        long_id: str, aggregation: DateType, date_input: str
    ) -> list[aioaquarea.Consumption]:
        if aggregation == DateType.DAY:
            raise aioaquarea.ClientError("cloud unhappy")
        return []

    mock_aquarea_client.get_device_consumption.side_effect = consumption
    with patch(
        "custom_components.aquarea.sensor.async_redistribute_hourly_statistics",
        AsyncMock(return_value=0),
    ) as redistribute:
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    assert coordinator.hourly_consumption_fetched_at is None
    redistribute.assert_not_awaited()


async def test_a_bad_total_reaches_the_correction_through_the_listener(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The coordinator listener corrects with the fallback instead of raising."""
    freezer.move_to(_local(5, 12))
    bad = aioaquarea.Consumption(
        {"dataTime": _label(5, 4), "heatConsumption": "0.5", "tankConsumption": 1.0}
    )

    async def consumption(
        long_id: str, aggregation: DateType, date_input: str
    ) -> list[aioaquarea.Consumption]:
        if aggregation == DateType.DAY and date_input == "20261005":
            return [bad]
        return []

    mock_aquarea_client.get_device_consumption.side_effect = consumption
    with patch(
        "custom_components.aquarea.sensor.async_redistribute_hourly_statistics",
        AsyncMock(return_value=0),
    ) as redistribute:
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    corrected = {call.args[1]: call.args[2] for call in redistribute.await_args_list}
    assert corrected["sensor.heat_pump_accumulated_consumption"] == {
        _local(5, 4): pytest.approx(1.5)
    }


async def test_hourly_fetch_has_its_own_cadence_when_the_month_query_fails(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A failing MONTH query no longer drags the DAY query along on every poll."""
    freezer.move_to(_local(5, 12))

    async def consumption(
        long_id: str, aggregation: DateType, date_input: str
    ) -> list[aioaquarea.Consumption]:
        if aggregation == DateType.MONTH:
            raise aioaquarea.ClientError("month unhappy")
        return []

    mock_aquarea_client.get_device_consumption.side_effect = consumption
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    assert _day_calls(mock_aquarea_client) == ["20261003", "20261004", "20261005"]
    fetched_at = coordinator.hourly_consumption_fetched_at

    # Every 60 s poll retries the month, but not the hours.
    for _ in range(5):
        freezer.tick(timedelta(minutes=1))
        await coordinator.async_refresh()
    assert _day_calls(mock_aquarea_client) == ["20261003", "20261004", "20261005"]
    assert coordinator.hourly_consumption_fetched_at == fetched_at
    month_calls = [
        call
        for call in mock_aquarea_client.get_device_consumption.await_args_list
        if call.args[1] == DateType.MONTH
    ]
    assert len(month_calls) == 6

    # Once the interval has passed, today's hours are fetched again.
    freezer.tick(timedelta(minutes=55))
    await coordinator.async_refresh()
    assert _day_calls(mock_aquarea_client) == [
        "20261003",
        "20261004",
        "20261005",
        "20261005",
    ]


async def test_yesterday_is_refetched_after_midnight_with_a_long_interval(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
) -> None:
    """With a 180-minute interval, yesterday is still fetched until it is complete.

    It is asked for at most hourly from midnight (UTC, the cloud's day) until
    it has been received at 03:00 UTC or later, then no more; today keeps the
    180-minute interval.
    """
    entry = MockConfigEntry(
        domain="aquarea",
        title="user",
        unique_id="user",
        data={"username": "user", "password": "not-a-real-password"},
        options={CONF_CONSUMPTION_INTERVAL: 180},
    )
    freezer.move_to(_cloud(5, 23, 30))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(iter(entry.runtime_data.values()))

    polls: dict[str, list[str]] = {}
    moment = _cloud(6, 0, 1)
    while moment < _cloud(6, 6):
        freezer.move_to(moment)
        mock_aquarea_client.get_device_consumption.reset_mock()
        await coordinator.async_refresh()
        if calls := _day_calls(mock_aquarea_client):
            polls[moment.strftime("%H:%M")] = calls
        moment += timedelta(minutes=1)

    assert polls == {
        "00:01": ["20261006"],
        "00:30": ["20261005"],
        "01:30": ["20261005"],
        "02:30": ["20261005"],
        "03:01": ["20261006"],
        "03:30": ["20261005"],
    }


async def test_today_is_corrected_when_only_today_was_fetched(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If the earlier days' queries fail at startup, today's rows are still rewritten."""
    freezer.move_to(_local(5, 12))

    async def consumption(
        long_id: str, aggregation: DateType, date_input: str
    ) -> list[aioaquarea.Consumption]:
        if aggregation == DateType.DAY and date_input in {"20261003", "20261004"}:
            raise aioaquarea.ClientError("cloud unhappy")
        if aggregation == DateType.DAY:
            return [_record(_label(5, 4), tank=1.19)]
        return []

    mock_aquarea_client.get_device_consumption.side_effect = consumption
    with patch(
        "custom_components.aquarea.sensor.async_redistribute_hourly_statistics",
        AsyncMock(return_value=0),
    ) as redistribute:
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    assert coordinator.hourly_consumption_first_day == _cloud(5, 0).date()
    assert {call.args[3] for call in redistribute.await_args_list} == {_cloud(5, 0)}
