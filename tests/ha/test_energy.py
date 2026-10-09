"""The energy sensors follow the cloud's UTC days, so no energy is lost at midnight.

aioaquarea queries consumption with osTimezone +00:00, so the Panasonic cloud
labels each day (and month) by the UTC date: in Europe/Warsaw the cloud day
"20261006" runs from 02:00 CEST on 2026-10-06 to 02:00 CEST on 2026-10-07.
The sensors used to look up the local date instead. The "today" sensors then
switched to the next day's entry at local midnight, two hours before the cloud
did, and never counted the energy used between 00:00 and 02:00. The
month-to-date sensors lost the same slice once a month, because the
coordinator requested the local month.

The cloud below is a stand-in that labels days in UTC, like the real one. The
energy Home Assistant meters from each TOTAL_INCREASING sensor (each rise,
plus the new value after a reset) must equal the energy the cloud recorded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from aioaquarea.statistics import Consumption, DateType
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID

WARSAW = ZoneInfo("Europe/Warsaw")
TODAY = "sensor.heat_pump_heating_consumption"
MONTH = "sensor.heat_pump_heating_accumulated_consumption"

# Heating energy per cloud (UTC) day, in kWh, as the cloud reports it at the
# given Warsaw time. Each cloud month is the list of its days.
type Cloud = dict[str, dict[str, float]]


def _consumption(cloud: Cloud) -> Callable[..., list[Consumption]]:
    def get_device_consumption(
        long_id: str, date_type: DateType, date_str: str
    ) -> list[Consumption]:
        if date_type != DateType.MONTH:
            return []
        days = cloud.get(date_str[:6], {})
        return [
            Consumption({"dataTime": day, "heatConsumption": kwh})
            for day, kwh in sorted(days.items())
        ]

    return get_device_consumption


def _month_queries(client: AsyncMock) -> list[str]:
    return [
        call.args[2]
        for call in client.get_device_consumption.call_args_list
        if call.args[1] == DateType.MONTH
    ]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config.async_set_time_zone("Europe/Warsaw")
    # The "today" sensors are disabled by default.
    er.async_get(hass).async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{DEVICE_ID}_heating_energy_consumption",
        suggested_object_id="heat_pump_heating_consumption",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _at(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _metered(values: list[float]) -> float:
    """Energy recorded from a TOTAL_INCREASING sensor's successive values."""
    total = 0.0
    for previous, value in pairwise(values):
        total += value if value < previous else value - previous
    return total


async def test_today_sensor_counts_the_hours_after_local_midnight(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Energy used 00:00-02:00 CEST lands in the previous cloud day and is kept."""
    cloud: Cloud = {"202610": {"20261006": 5.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(datetime(2026, 10, 6, 23, 30, tzinfo=WARSAW))
    await _setup(hass, mock_config_entry)
    # A new sensor shows 0 until the coordinator's next tick.
    await _at(hass, freezer, datetime(2026, 10, 6, 23, 31, tzinfo=WARSAW))
    readings = [float(hass.states.get(TODAY).state)]
    assert readings[-1] == 5.0

    # 00:30 and 01:59 CEST are still 2026-10-06 in UTC: the cloud adds the
    # hours since local midnight to "20261006", and there is no "20261007".
    for when, kwh in (
        (datetime(2026, 10, 7, 0, 30), 5.2),
        (datetime(2026, 10, 7, 1, 59), 5.4),
    ):
        cloud["202610"]["20261006"] = kwh
        await _at(hass, freezer, when.replace(tzinfo=WARSAW))
        readings.append(float(hass.states.get(TODAY).state))
        assert readings[-1] == kwh

    # 02:30 CEST: the cloud day "20261007" has started.
    cloud["202610"]["20261007"] = 0.1
    await _at(hass, freezer, datetime(2026, 10, 7, 2, 30, tzinfo=WARSAW))
    readings.append(float(hass.states.get(TODAY).state))
    assert readings[-1] == 0.1

    # The cloud recorded 0.4 more for 2026-10-06 and 0.1 for 2026-10-07.
    assert _metered(readings) == pytest.approx(0.5)


async def test_month_sensor_keeps_the_last_hours_of_the_cloud_month(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Local October starts two hours before the cloud's; nothing is dropped."""
    cloud: Cloud = {"202609": {"20260929": 30.0, "20260930": 4.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(datetime(2026, 9, 30, 23, 30, tzinfo=WARSAW))
    await _setup(hass, mock_config_entry)
    await _at(hass, freezer, datetime(2026, 9, 30, 23, 31, tzinfo=WARSAW))
    readings = [float(hass.states.get(MONTH).state)]
    assert readings[-1] == 34.0

    # 00:30 and 01:59 CEST on 2026-10-01 are still September in UTC.
    for when, kwh in (
        (datetime(2026, 10, 1, 0, 30), 4.2),
        (datetime(2026, 10, 1, 1, 59), 4.4),
    ):
        cloud["202609"]["20260930"] = kwh
        await _at(hass, freezer, when.replace(tzinfo=WARSAW))
        readings.append(float(hass.states.get(MONTH).state))
        assert readings[-1] == 30.0 + kwh

    # 02:30 CEST: the cloud's October has started.
    cloud["202610"] = {"20261001": 0.1}
    await _at(hass, freezer, datetime(2026, 10, 1, 2, 30, tzinfo=WARSAW))
    readings.append(float(hass.states.get(MONTH).state))
    assert readings[-1] == 0.1

    assert _month_queries(mock_aquarea_client) == [
        "20260901",
        "20260901",
        "20260901",
        "20261001",
    ]
    assert _metered(readings) == pytest.approx(0.5)


async def test_month_sensor_counts_the_cloud_day_that_started_before_local_midnight(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """West of UTC the cloud's day starts before the local one.

    At 21:00 EDT on 2026-10-06 the cloud's day is already 2026-10-07. Summing
    only the days up to the local date left that day out, and the sensor
    dropped by its energy, which the recorder counts as a meter reset.
    """
    cloud: Cloud = {"202610": {"20261006": 5.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    new_york = ZoneInfo("America/New_York")
    freezer.move_to(datetime(2026, 10, 6, 19, 30, tzinfo=new_york))
    await _setup(hass, mock_config_entry)
    await hass.config.async_set_time_zone("America/New_York")
    await _at(hass, freezer, datetime(2026, 10, 6, 19, 31, tzinfo=new_york))
    assert float(hass.states.get(MONTH).state) == 5.0

    cloud["202610"]["20261007"] = 0.3
    await _at(hass, freezer, datetime(2026, 10, 6, 21, 0, tzinfo=new_york))
    assert float(hass.states.get(MONTH).state) == 5.3


# --- Upgrading from a version that followed the local day --------------------


def _restore(
    hass: HomeAssistant, entity_id: str, value: float, period: datetime
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(entity_id, str(value)),
                {
                    "native_value": value,
                    "native_unit_of_measurement": "kWh",
                    "period_being_processed": period.isoformat(),
                    "accumulated_period_being_processed": value,
                },
            )
        ],
    )


async def test_a_today_sensor_restored_after_local_midnight_waits_for_the_cloud(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Upgraded at 00:30 CEST: the old version had reset to 0 at local midnight.

    The cloud's day is still 2026-10-06 (5.2 kWh), which the recorder metered
    before the reset. Showing it again would count those 5.2 kWh twice, so
    the sensor keeps 0 until the cloud's day 2026-10-07 starts at 02:00.
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")
    _restore(hass, TODAY, 0.0, datetime(2026, 10, 7, tzinfo=WARSAW))
    cloud: Cloud = {"202610": {"20261006": 5.2}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(datetime(2026, 10, 7, 0, 30, tzinfo=WARSAW))
    await _setup(hass, mock_config_entry)
    await _at(hass, freezer, datetime(2026, 10, 7, 0, 31, tzinfo=WARSAW))
    assert float(hass.states.get(TODAY).state) == 0.0

    cloud["202610"]["20261006"] = 5.4
    cloud["202610"]["20261007"] = 0.1
    await _at(hass, freezer, datetime(2026, 10, 7, 2, 30, tzinfo=WARSAW))
    assert float(hass.states.get(TODAY).state) == 0.1


async def test_a_month_sensor_restored_after_local_midnight_waits_for_the_cloud(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Upgraded at 00:30 CET on 2026-11-01: the old version showed November's 0.

    The cloud's October runs until 01:00 CET; its 300 kWh were metered
    already, and showing them again would add a month of energy to the
    Energy dashboard in one hour.
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")
    _restore(hass, MONTH, 0.0, datetime(2026, 11, 1, tzinfo=WARSAW))
    cloud: Cloud = {"202610": {"20261031": 300.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(datetime(2026, 11, 1, 0, 30, tzinfo=WARSAW))
    await _setup(hass, mock_config_entry)
    await _at(hass, freezer, datetime(2026, 11, 1, 0, 31, tzinfo=WARSAW))
    assert float(hass.states.get(MONTH).state) == 0.0

    cloud["202611"] = {"20261101": 0.2}
    await _at(hass, freezer, datetime(2026, 11, 1, 1, 30, tzinfo=WARSAW))
    assert float(hass.states.get(MONTH).state) == 0.2


# --- With the hourly statistics correction, on the recorder ------------------

# Heating energy the cloud labels with each UTC hour; 00:00-02:00 CEST on
# 2026-10-07 is 22:00-24:00 UTC on 2026-10-06, the last hours of the cloud
# day "20261006".
CLOUD_HOURS = {
    datetime(2026, 10, 6, 10, tzinfo=UTC): 5.0,
    datetime(2026, 10, 6, 20, tzinfo=UTC): 0.1,
    datetime(2026, 10, 6, 21, tzinfo=UTC): 0.1,
    datetime(2026, 10, 6, 22, tzinfo=UTC): 0.2,
    datetime(2026, 10, 6, 23, tzinfo=UTC): 0.3,
    datetime(2026, 10, 7, 0, tzinfo=UTC): 0.4,
    datetime(2026, 10, 7, 1, tzinfo=UTC): 0.5,
    datetime(2026, 10, 7, 2, tzinfo=UTC): 1.2,
}
# The cloud publishes an hour's energy this long after the hour started.
PUBLISH_LAG = timedelta(minutes=65)


def _published_cloud(
    long_id: str, date_type: DateType, date_str: str
) -> list[Consumption]:
    """The cloud's MONTH and DAY answers at the frozen time, labelled in UTC."""
    published = {
        hour: kwh
        for hour, kwh in CLOUD_HOURS.items()
        if hour + PUBLISH_LAG <= dt_util.utcnow()
    }
    if date_type == DateType.DAY:
        return [
            Consumption(
                {"dataTime": hour.strftime("%Y%m%d %H"), "heatConsumption": kwh}
            )
            for hour, kwh in published.items()
            if hour.strftime("%Y%m%d") == date_str
        ]
    days: dict[str, float] = {}
    for hour, kwh in published.items():
        if hour.strftime("%Y%m") == date_str[:6]:
            day = hour.strftime("%Y%m%d")
            days[day] = days.get(day, 0.0) + kwh
    return [
        Consumption({"dataTime": day, "heatConsumption": kwh})
        for day, kwh in sorted(days.items())
    ]


async def _hourly_changes(hass: HomeAssistant, statistic_id: str) -> dict[int, float]:
    """Return each UTC hour's energy on 2026-10-06/07 as the Energy dashboard sees it."""
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 10, 6, 18, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"change"},
    )
    changes = {
        dt_util.utc_from_timestamp(row["start"]).hour: round(row["change"], 3)
        for row in rows[statistic_id]
    }
    return {hour: change for hour, change in changes.items() if change}


async def test_the_reset_at_the_cloud_midnight_keeps_the_hourly_statistics_right(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The today sensor's reset meets the correction of its hourly statistics.

    The integration runs against a cloud that publishes each hour 65 minutes
    after it starts, fetching the month at :00:30 every hour, and the
    recorder compiles its statistics every 5 minutes. The correction (statistics.py) reads a drop in the sensor's
    state as a reset: cloud hours before it are lost to the sensor, the
    reset hour's own one is still to come. That holds when the reset is at
    the cloud's midnight.

    The today sensor loses 22:00 and 23:00 UTC, which the cloud publishes after
    the last fetch of its day (the hour the cloud publishes after its day has
    ended, and up to a consumption interval before it), and no other hour
    takes their energy; every other hour keeps its own energy, none of it
    twice. The month sensor, which has no reset that night, keeps every hour.

    Live, 2026-10-09: with the reset at local midnight (22:00 UTC), the
    correction kept 22:00 UTC waiting for energy that never came and gave it
    the energy recorded at 03:03 CEST, which belongs to 00:00 UTC.
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")
    mock_aquarea_client.get_device_consumption.side_effect = _published_cloud
    period = datetime(2026, 10, 6, 14, tzinfo=UTC)
    freezer.move_to(period)
    await _setup(hass, mock_config_entry)
    while period < datetime(2026, 10, 7, 9, tzinfo=UTC):
        await _at(hass, freezer, period + timedelta(seconds=30))
        await async_wait_recording_done(hass)
        freezer.move_to(period + timedelta(minutes=5, seconds=5))
        do_adhoc_statistics(hass, start=period)
        await async_wait_recording_done(hass)
        period += timedelta(minutes=5)

    assert await _hourly_changes(hass, TODAY) == {
        20: 0.1,
        21: 0.1,
        0: 0.4,
        1: 0.5,
        2: 1.2,
    }
    assert await _hourly_changes(hass, MONTH) == {
        20: 0.1,
        21: 0.1,
        22: 0.2,
        23: 0.3,
        0: 0.4,
        1: 0.5,
        2: 1.2,
    }
