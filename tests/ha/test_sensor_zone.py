"""The temperature sensor of each zone."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import DEVICE_ID, _make_zone

ENTITY_ID = "sensor.heat_pump_house_temperature"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_zone_temperature_follows_the_zone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """The sensor shows the zone temperature and changes after a refresh."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(ENTITY_ID)
    assert state.state == "21"
    assert state.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS
    assert state.attributes["device_class"] == "temperature"
    assert state.attributes["state_class"] == "measurement"
    entry = er.async_get(hass).async_get(ENTITY_ID)
    assert entry.unique_id == f"{DEVICE_ID}_zone_1_temperature"
    assert entry.disabled_by is None

    mock_device.zones[1].temperature = 34
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "34"


async def test_each_zone_gets_its_own_sensor(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A second zone gets a sensor with its own unique ID, name and value."""
    second = _make_zone()
    second.zone_id = 2
    second.name = "Garage"
    second.temperature = 12
    mock_device.zones[2] = second
    await _setup(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == "21"
    garage = hass.states.get("sensor.heat_pump_garage_temperature")
    assert garage.state == "12"
    entry = er.async_get(hass).async_get("sensor.heat_pump_garage_temperature")
    assert entry.unique_id == f"{DEVICE_ID}_zone_2_temperature"


async def test_zone_missing_from_the_data_keeps_the_last_value(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A refresh without the zone leaves the sensor at its last value."""
    await _setup(hass, mock_config_entry)

    mock_device.zones.clear()
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "21"
