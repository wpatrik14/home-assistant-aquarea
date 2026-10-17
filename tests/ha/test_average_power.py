"""The average power sensors derive W from the cloud's cumulative daily kWh.

The cloud has no instantaneous power. Between two consecutive consumption
fetches the day's energy rises by some kWh; over the time between the fetches
that is an average power. The first two tests drive the arithmetic in
power.py, the rest the sensors through a real coordinator.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

from aioaquarea import ConsumptionType
from aioaquarea.statistics import Consumption, DateType
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquarea.const import CONF_CONSUMPTION_INTERVAL, DOMAIN
from custom_components.aquarea.power import EnergyReading, average_power, daily_energy

from .conftest import DEVICE_ID, PASSWORD, USERNAME

INTERVAL = timedelta(minutes=15)
DAY = date(2026, 10, 17)
T0 = datetime(2026, 10, 17, 11, 18, tzinfo=UTC)


def _reading(kwh: float, minutes: float, day: date = DAY) -> EnergyReading:
    return EnergyReading(day, kwh, T0 + timedelta(minutes=minutes))


def test_first_fetch_has_no_power() -> None:
    """Without an earlier reading (or without today's entry) power is unknown."""
    assert average_power(None, _reading(0.296, 0), INTERVAL) is None
    assert average_power(_reading(0.0, 0), None, INTERVAL) is None


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        # 0.052 kWh in 15 minutes is 208 W.
        (_reading(0.296, 0), _reading(0.348, 15), pytest.approx(208.0)),
        # The elapsed time is the real one, not the nominal interval.
        (_reading(0.0, 0), _reading(0.296, 16), pytest.approx(1110.0)),
        # Unchanged energy is 0 W.
        (_reading(0.348, 0), _reading(0.348, 15), 0.0),
        # A fall of one cloud step is rounding noise: clamped, never negative.
        (_reading(0.348, 0), _reading(0.347, 15), 0.0),
        # A larger fall is the counter starting over: not measurable.
        (_reading(0.348, 0), _reading(0.1, 15), None),
        (_reading(0.348, 0), _reading(0.0, 15), None),
        # The cloud's day changed between the fetches.
        (_reading(5.0, 0), _reading(0.2, 15, DAY + timedelta(days=1)), None),
        # Exactly two intervals apart still counts; more does not.
        (_reading(0.0, 0), _reading(0.5, 30), 1000.0),
        (_reading(0.0, 0), _reading(0.5, 31), None),
        # Time must move forward.
        (_reading(0.0, 0), _reading(0.5, 0), None),
    ],
)
def test_average_power(
    previous: EnergyReading, current: EnergyReading, expected: float | None
) -> None:
    """Average power between two readings."""
    assert average_power(previous, current, INTERVAL) == expected


def test_daily_energy_reads_the_days_entry() -> None:
    """The day's entry gives each type; a missing day gives None."""
    month = [
        Consumption({"dataTime": "20261016", "heatConsumption": 9.0}),
        Consumption(
            {
                "dataTime": "20261017",
                "heatConsumption": 1.0,
                "coolConsumption": 0.5,
                "tankConsumption": 0.25,
            }
        ),
    ]
    energy = daily_energy(month, DAY)
    assert energy is not None
    assert energy[ConsumptionType.HEAT] == 1.0
    assert energy[ConsumptionType.COOL] == 0.5
    assert energy[ConsumptionType.WATER_TANK] == 0.25
    assert energy[ConsumptionType.TOTAL] == pytest.approx(1.75)
    assert daily_energy(month, date(2026, 10, 18)) is None
    assert daily_energy(None, DAY) is None
    assert daily_energy([Consumption({"heatConsumption": 1.0})], DAY) is None
    bad_date = [Consumption({"dataTime": "nonsense", "heatConsumption": 1.0})]
    assert daily_energy(bad_date, DAY) is None


# Sensor tests: the sensors are enabled by default except cooling.
TOTAL = "sensor.heat_pump_average_power_total"
HEATING = "sensor.heat_pump_average_power_heating"
TANK = "sensor.heat_pump_average_power_tank"
COOLING = "sensor.heat_pump_average_power_cooling"

type Day = dict[str, float]


def _consumption(cloud: dict[str, Day]) -> object:
    def get_device_consumption(
        long_id: str, date_type: DateType, date_str: str
    ) -> list[Consumption] | None:
        if date_type != DateType.MONTH:
            return []
        if cloud.get("fail") is not None:
            msg = "boom"
            raise RuntimeError(msg)
        return [
            Consumption({"dataTime": day, **values})
            for day, values in sorted(cloud.items())
            if day != "fail"
        ]

    return get_device_consumption


@pytest.fixture
def entry_15() -> MockConfigEntry:
    """A config entry with the 15 minute consumption interval."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        options={CONF_CONSUMPTION_INTERVAL: 15},
    )


async def _at(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_sensors_follow_the_consumption_fetches(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    entry_15: MockConfigEntry,
) -> None:
    """The first fetch gives unknown, then each fetch the average since the last."""
    cloud: dict[str, Day] = {"20261017": {"heatConsumption": 0.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(T0)
    await _setup(hass, entry_15)

    state = hass.states.get(HEATING)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["unit_of_measurement"] == UnitOfPower.WATT
    assert state.attributes["device_class"] == "power"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["interval_minutes"] == 15
    assert datetime.fromisoformat(state.attributes["based_on_fetch_at"]) == T0

    cloud["20261017"] = {
        "heatConsumption": 0.296,
        "tankConsumption": 0.1,
    }
    await _at(hass, freezer, T0 + timedelta(minutes=16))
    heating = hass.states.get(HEATING)
    assert heating is not None
    assert float(heating.state) == pytest.approx(0.296 * 1000 / (16 / 60))
    assert float(hass.states.get(TANK).state) == pytest.approx(0.1 * 1000 / (16 / 60))
    # The total is the sum of the cloud's own total, which here is absent
    # from the record and so falls back to heat + cool + tank.
    assert float(hass.states.get(TOTAL).state) == pytest.approx(
        0.396 * 1000 / (16 / 60)
    )

    # Nothing published since the last fetch: 0 W.
    await _at(hass, freezer, T0 + timedelta(minutes=31))
    assert float(hass.states.get(HEATING).state) == 0.0


async def test_cooling_sensor_is_disabled_by_default(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    entry_15: MockConfigEntry,
) -> None:
    """Heating, tank and total are enabled; cooling is opt-in like its energy sensor."""
    await _setup(hass, entry_15)
    registry = er.async_get(hass)
    ids = {
        key: registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_ID}_{key}")
        for key in (
            "heating_average_power",
            "tank_average_power",
            "cooling_average_power",
            "average_power",
        )
    }
    assert all(ids.values())
    assert hass.states.get(COOLING) is None
    for entity_id in (HEATING, TANK, TOTAL):
        assert hass.states.get(entity_id) is not None
    assert registry.async_get(ids["cooling_average_power"]).disabled


async def test_midnight_reset_is_skipped_not_negative(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    entry_15: MockConfigEntry,
) -> None:
    """Across the cloud's (UTC) midnight the power is unknown, then resumes."""
    cloud: dict[str, Day] = {"20261017": {"heatConsumption": 5.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    start = datetime(2026, 10, 17, 23, 50, tzinfo=UTC)
    freezer.move_to(start)
    await _setup(hass, entry_15)

    # The new cloud day is not published yet: unknown.
    await _at(hass, freezer, start + timedelta(minutes=16))
    assert hass.states.get(HEATING).state == "unknown"

    # The new day's entry appears, with its first energy: still no baseline.
    cloud["20261018"] = {"heatConsumption": 0.1}
    await _at(hass, freezer, start + timedelta(minutes=32))
    assert hass.states.get(HEATING).state == "unknown"

    # The next fetch measures the new day.
    cloud["20261018"] = {"heatConsumption": 0.35}
    await _at(hass, freezer, start + timedelta(minutes=48))
    assert float(hass.states.get(HEATING).state) == pytest.approx(
        0.25 * 1000 / (16 / 60)
    )


async def test_failed_fetch_keeps_the_value_and_a_long_gap_gives_unknown(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    entry_15: MockConfigEntry,
) -> None:
    """A failed fetch changes nothing; once the gap exceeds 2 intervals power is unknown."""
    cloud: dict[str, Day] = {"20261017": {"heatConsumption": 0.0}}
    mock_aquarea_client.get_device_consumption.side_effect = _consumption(cloud)
    freezer.move_to(T0)
    await _setup(hass, entry_15)
    cloud["20261017"] = {"heatConsumption": 0.25}
    await _at(hass, freezer, T0 + timedelta(minutes=15))
    value = hass.states.get(HEATING).state
    assert float(value) == pytest.approx(1000.0)

    cloud["fail"] = {}
    await _at(hass, freezer, T0 + timedelta(minutes=30))
    await _at(hass, freezer, T0 + timedelta(minutes=45))
    assert hass.states.get(HEATING).state == value

    # The cloud is back after 45 minutes without a reading (3 intervals).
    del cloud["fail"]
    cloud["20261017"] = {"heatConsumption": 0.5}
    await _at(hass, freezer, T0 + timedelta(minutes=61))
    assert hass.states.get(HEATING).state == "unknown"

    # The next pair of fetches measures again.
    cloud["20261017"] = {"heatConsumption": 0.75}
    await _at(hass, freezer, T0 + timedelta(minutes=76))
    assert float(hass.states.get(HEATING).state) == pytest.approx(
        0.25 * 1000 / (15 / 60)
    )


async def test_no_data_from_the_cloud_keeps_the_readings(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    entry_15: MockConfigEntry,
) -> None:
    """Aioaquarea's None (an HTTP error) is a failed fetch, not an empty day."""
    cloud: dict[str, Day] = {"20261017": {"heatConsumption": 0.0}}
    consumption = _consumption(cloud)
    answers: list[object] = [None]

    def get_device_consumption(*args: object) -> object:
        if answers:
            return answers.pop()
        return consumption(*args)

    mock_aquarea_client.get_device_consumption.side_effect = get_device_consumption
    freezer.move_to(T0)
    await _setup(hass, entry_15)  # the first month fetch returns None
    assert hass.states.get(HEATING).state == "unknown"
    cloud["20261017"] = {"heatConsumption": 0.2}
    await _at(hass, freezer, T0 + timedelta(minutes=15))
    # The first real reading: still no baseline.
    assert hass.states.get(HEATING).state == "unknown"
