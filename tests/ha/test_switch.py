"""The three switches, driven through Home Assistant's switch services.

Each switch shows the device's flag, turns on and off optimistically, rolls
back when the cloud rejects the command, and drops the optimistic value at the
delayed refresh so the device's own state shows again.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ICON,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

# entity ID, device attribute, setter, enum, icon when on, icon when off
SWITCHES = [
    pytest.param(
        "switch.heat_pump_force_dhw",
        "force_dhw",
        "set_force_dhw",
        aioaquarea.ForceDHW,
        "mdi:water-boiler",
        "mdi:water-boiler-off",
        id="force_dhw",
    ),
    pytest.param(
        "switch.heat_pump_force_heater",
        "force_heater",
        "set_force_heater",
        aioaquarea.ForceHeater,
        "mdi:hvac",
        "mdi:hvac-off",
        id="force_heater",
    ),
    pytest.param(
        "switch.heat_pump_holiday_timer",
        "holiday_timer",
        "set_holiday_timer",
        aioaquarea.HolidayTimer,
        "mdi:timer-check",
        "mdi:timer-off",
        id="holiday_timer",
    ),
]
SWITCH_ARGS = ("entity_id", "attribute", "setter", "flag", "icon_on", "icon_off")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


@pytest.mark.parametrize(SWITCH_ARGS, SWITCHES)
async def test_state_follows_device(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    entity_id: str,
    attribute: str,
    setter: str,
    flag: type,
    icon_on: str,
    icon_off: str,
) -> None:
    """The switch shows the device's flag after a poll, with a matching icon."""
    await _setup(hass, mock_config_entry)
    state = hass.states.get(entity_id)
    assert (state.state, state.attributes[ATTR_ICON]) == (STATE_OFF, icon_off)

    setattr(mock_device, attribute, flag.ON)
    await _advance(hass, freezer, timedelta(minutes=1))

    state = hass.states.get(entity_id)
    assert (state.state, state.attributes[ATTR_ICON]) == (STATE_ON, icon_on)


@pytest.mark.parametrize(SWITCH_ARGS, SWITCHES)
async def test_turn_on_then_refresh(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    entity_id: str,
    attribute: str,
    setter: str,
    flag: type,
    icon_on: str,
    icon_off: str,
) -> None:
    """Turning on shows on at once; the delayed refresh then shows the device.

    The device here never applies the command, so after the refresh the switch
    goes back to off: the optimistic value does not outlive the refresh.
    """
    await _setup(hass, mock_config_entry)
    polls = mock_aquarea_client.get_device.await_count

    await _call(hass, SERVICE_TURN_ON, entity_id)

    getattr(mock_device, setter).assert_awaited_once_with(flag.ON)
    assert hass.states.get(entity_id).state == STATE_ON

    await _advance(hass, freezer, timedelta(seconds=11))

    assert mock_aquarea_client.get_device.await_count == polls + 1
    assert hass.states.get(entity_id).state == STATE_OFF


@pytest.mark.parametrize(SWITCH_ARGS, SWITCHES)
async def test_turn_off(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    entity_id: str,
    attribute: str,
    setter: str,
    flag: type,
    icon_on: str,
    icon_off: str,
) -> None:
    """Turning off an active switch sends OFF and shows off at once."""
    setattr(mock_device, attribute, flag.ON)
    await _setup(hass, mock_config_entry)
    assert hass.states.get(entity_id).state == STATE_ON

    await _call(hass, SERVICE_TURN_OFF, entity_id)

    getattr(mock_device, setter).assert_awaited_once_with(flag.OFF)
    assert hass.states.get(entity_id).state == STATE_OFF


@pytest.mark.parametrize(SWITCH_ARGS, SWITCHES)
@pytest.mark.parametrize(
    ("service", "initial"),
    [(SERVICE_TURN_ON, "OFF"), (SERVICE_TURN_OFF, "ON")],
)
async def test_command_failure_rolls_back(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    entity_id: str,
    attribute: str,
    setter: str,
    flag: type,
    icon_on: str,
    icon_off: str,
    service: str,
    initial: str,
) -> None:
    """When the cloud rejects the command, the device's state shows again."""
    setattr(mock_device, attribute, flag[initial])
    getattr(mock_device, setter).side_effect = aioaquarea.ApiError("500", "x")
    await _setup(hass, mock_config_entry)
    before = hass.states.get(entity_id).state

    with pytest.raises(aioaquarea.ApiError):
        await _call(hass, service, entity_id)

    assert hass.states.get(entity_id).state == before
