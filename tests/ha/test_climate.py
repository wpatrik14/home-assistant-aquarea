"""The zone climate entity, driven through Home Assistant's climate services.

State is read from `hass.states` after a coordinator poll, and commands go
through `hass.services`, so the tests cover the mapping from the device to
Home Assistant and back, the optimistic update, the rollback when the cloud
rejects a command, and the delayed refresh that follows a command.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

ENTITY_ID = "climate.heat_pump_house"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the coordinator run its next one-minute poll."""
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, service: str, **data: object) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        service,
        {ATTR_ENTITY_ID: ENTITY_ID, **data},
        blocking=True,
    )


async def test_initial_state(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A heating zone shows the heat-side target and limits."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(ENTITY_ID)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 21
    assert state.attributes[ATTR_TEMPERATURE] == 22
    assert state.attributes[ATTR_MIN_TEMP] == 10
    assert state.attributes[ATTR_MAX_TEMP] == 30
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_NONE


@pytest.mark.parametrize(
    ("mode", "hvac_mode", "target", "limits"),
    [
        (aioaquarea.ExtendedOperationMode.HEAT, HVACMode.HEAT, 22, (10, 30)),
        (aioaquarea.ExtendedOperationMode.COOL, HVACMode.COOL, 24, (18, 28)),
        (aioaquarea.ExtendedOperationMode.AUTO_HEAT, HVACMode.AUTO, 22, (10, 30)),
        (aioaquarea.ExtendedOperationMode.AUTO_COOL, HVACMode.AUTO, 24, (18, 28)),
    ],
)
async def test_mode_selects_target_and_limits(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    mode: aioaquarea.ExtendedOperationMode,
    hvac_mode: HVACMode,
    target: int,
    limits: tuple[int, int],
) -> None:
    """Cooling modes show the cool-side target and limits, heating the heat side."""
    await _setup(hass, mock_config_entry)

    # Distinct cool and heat maximums, so swapping them is caught.
    mock_device.zones[1].cool_max = 28
    mock_device.mode = mode
    await _poll(hass, freezer)

    state = hass.states.get(ENTITY_ID)
    assert state.state == hvac_mode
    assert state.attributes[ATTR_TEMPERATURE] == target
    assert (state.attributes[ATTR_MIN_TEMP], state.attributes[ATTR_MAX_TEMP]) == limits


async def test_limits_pinned_when_temperature_cannot_be_set(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A zone without a settable temperature gets min = max = current."""
    await _setup(hass, mock_config_entry)

    mock_device.zones[1].supports_set_temperature = False
    await _poll(hass, freezer)

    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_MIN_TEMP] == 21
    assert state.attributes[ATTR_MAX_TEMP] == 21


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (aioaquarea.DeviceAction.HEATING, HVACAction.HEATING),
        (aioaquarea.DeviceAction.COOLING, HVACAction.COOLING),
        (aioaquarea.DeviceAction.IDLE, HVACAction.IDLE),
        (aioaquarea.DeviceAction.OFF, HVACAction.OFF),
        # Heating the tank is not heating the zone.
        (aioaquarea.DeviceAction.HEATING_WATER, HVACAction.IDLE),
    ],
)
async def test_hvac_action_follows_device_action(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    action: aioaquarea.DeviceAction,
    expected: HVACAction,
) -> None:
    """The HVAC action comes from the device's current action."""
    await _setup(hass, mock_config_entry)

    mock_device.current_action = action
    await _poll(hass, freezer)

    assert hass.states.get(ENTITY_ID).attributes[ATTR_HVAC_ACTION] == expected


async def test_zone_off(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A zone that is switched off is off, whatever the device is doing."""
    await _setup(hass, mock_config_entry)

    mock_device.zones[1].operation_status = aioaquarea.OperationStatus.OFF
    mock_device.current_action = aioaquarea.DeviceAction.HEATING
    await _poll(hass, freezer)

    state = hass.states.get(ENTITY_ID)
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.OFF


@pytest.mark.parametrize(
    ("special_status", "preset"),
    [
        (aioaquarea.SpecialStatus.ECO, PRESET_ECO),
        (aioaquarea.SpecialStatus.COMFORT, PRESET_COMFORT),
        (None, PRESET_NONE),
    ],
)
async def test_preset_follows_special_status(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    special_status: aioaquarea.SpecialStatus | None,
    preset: str,
) -> None:
    """The preset reflects the device's eco/comfort special status."""
    await _setup(hass, mock_config_entry)

    mock_device.special_status = special_status
    await _poll(hass, freezer)

    assert hass.states.get(ENTITY_ID).attributes[ATTR_PRESET_MODE] == preset


@pytest.mark.parametrize(
    ("hvac_mode", "update_mode"),
    [
        (HVACMode.HEAT, aioaquarea.UpdateOperationMode.HEAT),
        (HVACMode.COOL, aioaquarea.UpdateOperationMode.COOL),
        (HVACMode.AUTO, aioaquarea.UpdateOperationMode.AUTO),
        (HVACMode.OFF, aioaquarea.UpdateOperationMode.OFF),
    ],
)
async def test_set_hvac_mode(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    hvac_mode: HVACMode,
    update_mode: aioaquarea.UpdateOperationMode,
) -> None:
    """Setting the mode sends it for this zone, shows it at once, then refreshes."""
    await _setup(hass, mock_config_entry)
    polls = mock_aquarea_client.get_device.await_count

    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: hvac_mode})

    mock_device.set_mode.assert_awaited_once_with(update_mode, 1)
    assert hass.states.get(ENTITY_ID).state == hvac_mode

    # One refresh about ten seconds later picks up what the cloud applied.
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_aquarea_client.get_device.await_count == polls + 1


async def test_set_hvac_mode_failure_rolls_back(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """When the cloud rejects the mode, the entity returns to the old one."""
    await _setup(hass, mock_config_entry)
    mock_device.set_mode.side_effect = aioaquarea.ApiError("500", "rejected")

    with pytest.raises(aioaquarea.ApiError):
        await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.COOL})

    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT


async def test_set_temperature(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A new target is sent as a whole number for this zone and shown at once."""
    await _setup(hass, mock_config_entry)

    await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 23})

    mock_device.set_temperature.assert_awaited_once_with(23, 1)
    mock_device.set_mode.assert_not_awaited()
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 23


async def test_set_temperature_with_hvac_mode(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A target with a mode sets the mode first, then the temperature."""
    await _setup(hass, mock_config_entry)
    calls: list[str] = []
    mock_device.set_mode.side_effect = lambda *_: calls.append("mode")
    mock_device.set_temperature.side_effect = lambda *_: calls.append("temperature")

    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        **{ATTR_TEMPERATURE: 25, ATTR_HVAC_MODE: HVACMode.COOL},
    )

    mock_device.set_mode.assert_awaited_once_with(
        aioaquarea.UpdateOperationMode.COOL, 1
    )
    mock_device.set_temperature.assert_awaited_once_with(25, 1)
    # In cool mode the target belongs to the cool side, so the mode goes first.
    assert calls == ["mode", "temperature"]
    state = hass.states.get(ENTITY_ID)
    assert state.state == HVACMode.COOL
    assert state.attributes[ATTR_TEMPERATURE] == 25


async def test_set_temperature_not_supported(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A zone without a settable temperature sends nothing."""
    await _setup(hass, mock_config_entry)
    mock_device.zones[1].supports_set_temperature = False
    await _poll(hass, freezer)

    # min = max = 21 now, so 21 is the only value the service accepts.
    await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 21})

    mock_device.set_temperature.assert_not_awaited()


async def test_set_temperature_failure_rolls_back(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """When the cloud rejects the target, the old one comes back."""
    await _setup(hass, mock_config_entry)
    mock_device.set_temperature.side_effect = aioaquarea.ApiError("500", "rejected")

    with pytest.raises(aioaquarea.ApiError):
        await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 23})

    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 22


@pytest.mark.parametrize(
    ("preset", "special_status"),
    [
        (PRESET_ECO, aioaquarea.SpecialStatus.ECO),
        (PRESET_COMFORT, aioaquarea.SpecialStatus.COMFORT),
        (PRESET_NONE, None),
    ],
)
async def test_set_preset_mode(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    preset: str,
    special_status: aioaquarea.SpecialStatus | None,
) -> None:
    """Each preset maps to its special status, and none clears it."""
    await _setup(hass, mock_config_entry)

    await _call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: preset})

    mock_device.set_special_status.assert_awaited_once_with(special_status)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_PRESET_MODE] == preset


async def test_set_preset_mode_failure_rolls_back(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """When the cloud rejects the preset, the old one comes back."""
    await _setup(hass, mock_config_entry)
    mock_device.set_special_status.side_effect = aioaquarea.ApiError("500", "x")

    with pytest.raises(aioaquarea.ApiError):
        await _call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_ECO})

    assert hass.states.get(ENTITY_ID).attributes[ATTR_PRESET_MODE] == PRESET_NONE


@pytest.mark.parametrize(
    ("service", "method", "start_mode", "expected"),
    [
        (SERVICE_TURN_ON, "turn_on", HVACMode.OFF, HVACMode.HEAT),
        (SERVICE_TURN_OFF, "turn_off", HVACMode.HEAT, HVACMode.OFF),
    ],
)
async def test_turn_on_off(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    service: str,
    method: str,
    start_mode: HVACMode,
    expected: HVACMode,
) -> None:
    """Turning on shows heat and turning off shows off until the next refresh."""
    await _setup(hass, mock_config_entry)
    if start_mode is HVACMode.OFF:
        mock_device.zones[1].operation_status = aioaquarea.OperationStatus.OFF
        await _poll(hass, freezer)
    assert hass.states.get(ENTITY_ID).state == start_mode

    await _call(hass, service)

    getattr(mock_device, method).assert_awaited_once_with()
    assert hass.states.get(ENTITY_ID).state == expected


@pytest.mark.parametrize(
    ("service", "method", "start_mode"),
    [
        # Each case starts from the opposite of the optimistic value, so a
        # missing rollback is visible: turn_on shows HEAT, turn_off shows OFF.
        (SERVICE_TURN_ON, "turn_on", HVACMode.OFF),
        (SERVICE_TURN_OFF, "turn_off", HVACMode.HEAT),
    ],
)
async def test_turn_on_off_failure_rolls_back(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    service: str,
    method: str,
    start_mode: HVACMode,
) -> None:
    """When the cloud rejects turning on or off, the old mode comes back."""
    await _setup(hass, mock_config_entry)
    if start_mode is HVACMode.OFF:
        mock_device.zones[1].operation_status = aioaquarea.OperationStatus.OFF
        await _poll(hass, freezer)
    assert hass.states.get(ENTITY_ID).state == start_mode
    getattr(mock_device, method).side_effect = aioaquarea.ApiError("500", "x")

    with pytest.raises(aioaquarea.ApiError):
        await _call(hass, service)

    assert hass.states.get(ENTITY_ID).state == start_mode
