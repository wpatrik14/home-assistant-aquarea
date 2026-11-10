"""The pump status binary sensor and the pump direction enum sensor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aioaquarea
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import ATTR_OPTIONS, SensorDeviceClass
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID

PUMP_STATUS = "binary_sensor.heat_pump_pump_status"
DIRECTION = "sensor.heat_pump_direction"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _push_update(entry: MockConfigEntry) -> None:
    entry.runtime_data[DEVICE_ID].async_update_listeners()


@pytest.mark.parametrize(
    ("duty", "state"),
    [
        (aioaquarea.PumpDuty.ON, STATE_ON),
        (aioaquarea.PumpDuty.OFF, STATE_OFF),
        (None, STATE_UNKNOWN),
    ],
)
async def test_pump_status_binary_sensor(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    duty: aioaquarea.PumpDuty | None,
    state: str,
) -> None:
    """The pump is on when its duty is 1, off otherwise, unknown if not reported."""
    mock_device.pump_duty = duty
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PUMP_STATUS).state == state
    assert hass.states.get(PUMP_STATUS).attributes["device_class"] == "running"
    # The sensor it replaced is gone.
    assert hass.states.get("sensor.heat_pump_pump_status") is None


async def test_pump_status_follows_updates(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """The state changes with the coordinator data."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(PUMP_STATUS).state == STATE_OFF

    mock_device.pump_duty = aioaquarea.PumpDuty.ON
    _push_update(mock_config_entry)
    await hass.async_block_till_done()

    assert hass.states.get(PUMP_STATUS).state == STATE_ON


async def test_pump_status_registry_entry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The unique id keeps its suffix, in the binary_sensor domain."""
    await _setup(hass, mock_config_entry)

    assert entity_registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DEVICE_ID}_pump_status"
    )
    assert (
        entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DEVICE_ID}_pump_status"
        )
        is None
    )
    assert entity_registry.async_get(PUMP_STATUS).translation_key == "pump_status"
    assert BinarySensorDeviceClass.RUNNING == "running"


async def test_direction_enum_options(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The options are the lowercase names of the library's directions."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(DIRECTION)
    assert state.attributes["device_class"] == SensorDeviceClass.ENUM
    assert state.attributes[ATTR_OPTIONS] == ["idle", "pump", "water"]
    assert {d.name.lower() for d in aioaquarea.DeviceDirection} == set(
        state.attributes[ATTR_OPTIONS]
    )
    assert entity_registry.async_get(DIRECTION).unique_id == f"{DEVICE_ID}_direction"


@pytest.mark.parametrize(
    ("direction", "state"),
    [
        (aioaquarea.DeviceDirection.IDLE, "idle"),
        (aioaquarea.DeviceDirection.PUMP, "pump"),
        (aioaquarea.DeviceDirection.WATER, "water"),
    ],
)
async def test_direction_states_are_lowercase(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    direction: aioaquarea.DeviceDirection,
    state: str,
) -> None:
    """The state is the lowercase direction name."""
    mock_device.current_direction = direction
    await _setup(hass, mock_config_entry)

    assert hass.states.get(DIRECTION).state == state
