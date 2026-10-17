"""The accumulated energy sensors follow the cloud's UTC days; no energy is lost at midnight.

aioaquarea queries consumption with osTimezone +00:00, so the Panasonic cloud
labels each day (and month) by the UTC date: in Europe/Warsaw the cloud day
"20261006" runs from 02:00 CEST on 2026-10-06 to 02:00 CEST on 2026-10-07.
The sensors used to look up the local date instead. The month-to-date sensors
lost the energy of the cloud's last two hours once a month, because the
coordinator requested the local month. The "Consumption" sensors, which show
the energy of the cloud's current hour (a momentary value), are covered at the
end.

The cloud below is a stand-in that labels days in UTC, like the real one. The
energy Home Assistant meters from each accumulated (TOTAL_INCREASING) sensor
(each rise, plus the new value after a reset) must equal the energy the cloud
recorded.
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

from custom_components.aquarea.const import CONF_CONSUMPTION_INTERVAL, DOMAIN

from .conftest import DEVICE_ID

WARSAW = ZoneInfo("Europe/Warsaw")
NEW_YORK = ZoneInfo("America/New_York")
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


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    time_zone: str = "Europe/Warsaw",
    consumption_interval: int | None = None,
) -> None:
    await hass.config.async_set_time_zone(time_zone)
    # The current-hour sensors are disabled by default.
    er.async_get(hass).async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{DEVICE_ID}_heating_energy_consumption",
        suggested_object_id="heat_pump_heating_consumption",
    )
    entry.add_to_hass(hass)
    if consumption_interval is not None:
        hass.config_entries.async_update_entry(
            entry, options={CONF_CONSUMPTION_INTERVAL: consumption_interval}
        )
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


async def test_the_new_year_starts_with_the_cloud_s(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """At 00:30 CET on 2027-01-01 the cloud is still in December 2026.

    The month and the year roll over together at 01:00 CET: December is
    requested, and counted, until then.
    """
    cloud: Cloud = {"202612": {"20261230": 50.0, "20261231": 3.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(datetime(2026, 12, 31, 23, 30, tzinfo=WARSAW))
    await _setup(hass, mock_config_entry)
    await _at(hass, freezer, datetime(2026, 12, 31, 23, 31, tzinfo=WARSAW))
    readings = [float(hass.states.get(MONTH).state)]
    assert readings[-1] == 53.0

    cloud["202612"]["20261231"] = 3.2
    await _at(hass, freezer, datetime(2027, 1, 1, 0, 30, tzinfo=WARSAW))
    readings.append(float(hass.states.get(MONTH).state))
    assert readings[-1] == 53.2

    cloud["202612"]["20261231"] = 3.4
    cloud["202701"] = {"20270101": 0.1}
    await _at(hass, freezer, datetime(2027, 1, 1, 1, 30, tzinfo=WARSAW))
    readings.append(float(hass.states.get(MONTH).state))
    assert readings[-1] == 0.1

    assert _month_queries(mock_aquarea_client) == [
        "20261201",
        "20261201",
        "20270101",
    ]
    # 0.2 kWh of December's last hour was published after the last fetch of
    # the cloud's December: the residual described in the docs.
    assert _metered(readings) == pytest.approx(0.3)


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
    freezer.move_to(datetime(2026, 10, 6, 19, 30, tzinfo=NEW_YORK))
    await _setup(hass, mock_config_entry, "America/New_York")
    await _at(hass, freezer, datetime(2026, 10, 6, 19, 31, tzinfo=NEW_YORK))
    assert float(hass.states.get(MONTH).state) == 5.0

    cloud["202610"]["20261007"] = 0.3
    await _at(hass, freezer, datetime(2026, 10, 6, 21, 0, tzinfo=NEW_YORK))
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


async def test_a_month_sensor_restored_west_of_utc_carries_on(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Upgraded mid-month in New York: the restored month started at 04:00 UTC.

    Review of #125: compared as instants, the sensor was held until the
    cloud's next month, up to a month.
    """
    await hass.config.async_set_time_zone("America/New_York")
    _restore(hass, MONTH, 30.0, datetime(2026, 10, 1, tzinfo=NEW_YORK))
    cloud: Cloud = {"202610": {"20261005": 30.0, "20261006": 4.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(datetime(2026, 10, 6, 12, 0, tzinfo=NEW_YORK))
    await _setup(hass, mock_config_entry, "America/New_York")
    await _at(hass, freezer, datetime(2026, 10, 6, 12, 1, tzinfo=NEW_YORK))
    assert float(hass.states.get(MONTH).state) == 34.0


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

    The accumulated sensor has no reset that night and keeps every hour. The
    current-hour sensor (TODAY) goes back to 0 every hour, so it is not
    recorded as a meter: it has no hourly statistics at all.

    Live, 2026-10-09: with the reset at local midnight (22:00 UTC), the
    correction kept 22:00 UTC waiting for energy that never came and gave it
    the energy recorded at 03:03 CEST, which belongs to 00:00 UTC.
    """
    await hass.config.async_set_time_zone("Europe/Warsaw")
    mock_aquarea_client.get_device_consumption.side_effect = _published_cloud
    period = datetime(2026, 10, 6, 14, tzinfo=UTC)
    freezer.move_to(period)
    await _setup(hass, mock_config_entry, consumption_interval=60)
    while period < datetime(2026, 10, 7, 9, tzinfo=UTC):
        await _at(hass, freezer, period + timedelta(seconds=30))
        await async_wait_recording_done(hass)
        freezer.move_to(period + timedelta(minutes=5, seconds=5))
        do_adhoc_statistics(hass, start=period)
        await async_wait_recording_done(hass)
        period += timedelta(minutes=5)

    # The current-hour sensor is not a meter: it has no sum to correct.
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 10, 6, 18, tzinfo=UTC),
        None,
        {TODAY},
        "hour",
        None,
        {"sum"},
    )
    assert all("sum" not in row for row in rows.get(TODAY, []))
    assert await _hourly_changes(hass, MONTH) == {
        20: 0.1,
        21: 0.1,
        22: 0.2,
        23: 0.3,
        0: 0.4,
        1: 0.5,
        2: 1.2,
    }


# --- The current-hour sensor -------------------------------------------------


def _hourly_cloud(
    hours: dict[datetime, float],
) -> Callable[..., list[Consumption]]:
    """A cloud whose DAY answer holds `hours` (UTC hour start -> heating kWh)."""

    def get_device_consumption(
        long_id: str, date_type: DateType, date_str: str
    ) -> list[Consumption]:
        if date_type != DateType.DAY:
            return []
        return [
            Consumption(
                {"dataTime": hour.strftime("%Y%m%d %H"), "heatConsumption": kwh}
            )
            for hour, kwh in sorted(hours.items())
            if hour.strftime("%Y%m%d") == date_str
        ]

    return get_device_consumption


async def test_the_consumption_sensor_shows_the_current_hour_and_rolls_to_zero(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The sensor reads the cloud's current UTC hour, and is 0 when it has no record."""
    hours = {
        datetime(2026, 10, 6, 10, tzinfo=UTC): 9.0,
        datetime(2026, 10, 6, 11, tzinfo=UTC): 0.4,
    }
    mock_aquarea_client.get_device_consumption.side_effect = _hourly_cloud(hours)
    freezer.move_to(datetime(2026, 10, 6, 11, 5, tzinfo=UTC))
    await _setup(hass, mock_config_entry)
    await _at(hass, freezer, datetime(2026, 10, 6, 11, 6, tzinfo=UTC))
    state = hass.states.get(TODAY)
    assert state is not None
    assert float(state.state) == 0.4
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert "device_class" not in state.attributes

    # The hour changes before the cloud has a record for the next one: 0
    # within a minute, with no new fetch needed.
    await _at(hass, freezer, datetime(2026, 10, 6, 12, 1, tzinfo=UTC))
    assert float(hass.states.get(TODAY).state) == 0.0

    # The next fetch (default 15-minute interval) brings the new hour's record.
    hours[datetime(2026, 10, 6, 12, tzinfo=UTC)] = 0.25
    await _at(hass, freezer, datetime(2026, 10, 6, 12, 20, tzinfo=UTC))
    assert float(hass.states.get(TODAY).state) == 0.25

    # No record at all for the new hour after midnight: still 0, not the
    # previous hours' energy.
    await _at(hass, freezer, datetime(2026, 10, 7, 0, 30, tzinfo=UTC))
    assert float(hass.states.get(TODAY).state) == 0.0


async def test_the_consumption_sensor_is_zero_without_hourly_records(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With no hourly records the sensor shows 0, not unknown."""
    mock_aquarea_client.get_device_consumption.side_effect = _hourly_cloud({})
    freezer.move_to(datetime(2026, 10, 6, 11, 5, tzinfo=UTC))
    await _setup(hass, mock_config_entry)
    await _at(hass, freezer, datetime(2026, 10, 6, 11, 6, tzinfo=UTC))
    assert float(hass.states.get(TODAY).state) == 0.0
