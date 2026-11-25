"""A failing cloud command surfaces as a translated Home Assistant error.

Every platform that sends a command wraps the library's failures (API errors,
authentication errors, HTTP errors, timeouts) in a `HomeAssistantError` whose
`translation_key` points at `exceptions.*` in strings.json. The optimistic state
is rolled back, and a command that works behaves as before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
import aiohttp
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DATA_COMPONENT as CLIMATE_COMPONENT,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.water_heater import (
    ATTR_OPERATION_MODE,
    DOMAIN as WATER_HEATER_DOMAIN,
    SERVICE_SET_OPERATION_MODE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea.const import DOMAIN

CLIMATE = "climate.heat_pump_house"
SWITCH = "switch.heat_pump_force_dhw"
SELECT = "select.heat_pump_powerful"
BUTTON = "button.heat_pump_request_defrost"
TANK = "water_heater.heat_pump_tank"

Call = Callable[[HomeAssistant], Awaitable[None]]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _service(domain: str, service: str, entity_id: str, **data: object) -> Call:
    async def call(hass: HomeAssistant) -> None:
        await hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
        )

    return call


# (id, call, device attribute that fails)
COMMANDS = [
    pytest.param(
        _service(
            CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, CLIMATE, **{ATTR_HVAC_MODE: "cool"}
        ),
        "set_mode",
        id="climate_hvac_mode",
    ),
    pytest.param(
        _service(
            CLIMATE_DOMAIN, SERVICE_SET_TEMPERATURE, CLIMATE, **{ATTR_TEMPERATURE: 23}
        ),
        "set_temperature",
        id="climate_temperature",
    ),
    pytest.param(
        _service(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            CLIMATE,
            **{ATTR_PRESET_MODE: "eco"},
        ),
        "set_special_status",
        id="climate_preset",
    ),
    pytest.param(
        _service(CLIMATE_DOMAIN, SERVICE_TURN_OFF, CLIMATE),
        "turn_off",
        id="climate_turn_off",
    ),
    pytest.param(
        _service(CLIMATE_DOMAIN, SERVICE_TURN_ON, CLIMATE),
        "turn_on",
        id="climate_turn_on",
    ),
    pytest.param(
        _service(SWITCH_DOMAIN, SERVICE_TURN_ON, SWITCH),
        "set_force_dhw",
        id="switch",
    ),
    pytest.param(
        _service(
            SELECT_DOMAIN, SERVICE_SELECT_OPTION, SELECT, **{ATTR_OPTION: "on-30m"}
        ),
        "set_powerful_time",
        id="select",
    ),
    pytest.param(_service("button", "press", BUTTON), "request_defrost", id="button"),
]

ERRORS = [
    pytest.param(
        aioaquarea.ApiError("500", "rejected"), "command_failed", id="api_error"
    ),
    pytest.param(
        aioaquarea.RequestFailedError("bad gateway"), "command_failed", id="request"
    ),
    pytest.param(aiohttp.ClientConnectionError("down"), "command_failed", id="http"),
    pytest.param(TimeoutError(), "command_failed", id="timeout"),
    pytest.param(
        aioaquarea.AuthenticationError("401", "expired"),
        "not_authenticated",
        id="auth",
    ),
]


@pytest.mark.parametrize(("call", "method"), COMMANDS)
@pytest.mark.parametrize(("error", "key"), ERRORS)
async def test_failing_command_raises_translated_error(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    call: Call,
    method: str,
    error: Exception,
    key: str,
) -> None:
    """Each platform's command failure carries the translation key and the cause."""
    await _setup(hass, mock_config_entry)
    getattr(mock_device, method).side_effect = error

    with pytest.raises(HomeAssistantError) as exc_info:
        await call(hass)

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == key
    assert "error" in exc_info.value.translation_placeholders
    assert exc_info.value.__cause__ is error


@pytest.mark.parametrize(
    ("service", "data", "method", "restored"),
    [
        pytest.param(SERVICE_TURN_ON, {}, "turn_on", STATE_OFF, id="turn_on"),
        pytest.param(
            SERVICE_SET_OPERATION_MODE,
            {ATTR_OPERATION_MODE: "heating"},
            "turn_on",
            STATE_OFF,
            id="mode_heating",
        ),
        pytest.param(
            SERVICE_SET_OPERATION_MODE,
            {ATTR_OPERATION_MODE: STATE_OFF},
            "turn_off",
            "idle",
            id="mode_off",
        ),
        pytest.param(
            "set_temperature",
            {ATTR_TEMPERATURE: 55},
            "set_target_temperature",
            None,
            id="temperature",
        ),
    ],
)
async def test_water_heater_failure_raises_and_rolls_back(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    service: str,
    data: dict[str, object],
    method: str,
    restored: str | None,
) -> None:
    """The tank commands raise the translated error and restore the old state."""
    if service == SERVICE_TURN_ON:
        mock_device.tank.operation_status = aioaquarea.OperationStatus.OFF
    if service == SERVICE_SET_OPERATION_MODE and method == "turn_on":
        mock_device.tank.operation_status = aioaquarea.OperationStatus.OFF
    await _setup(hass, mock_config_entry)
    before = hass.states.get(TANK)
    getattr(mock_device.tank, method).side_effect = aioaquarea.ApiError("500", "x")

    with pytest.raises(HomeAssistantError) as exc_info:
        await _service(WATER_HEATER_DOMAIN, service, TANK, **data)(hass)

    assert exc_info.value.translation_key == "command_failed"
    after = hass.states.get(TANK)
    assert after.state == before.state
    assert after.attributes[ATTR_TEMPERATURE] == before.attributes[ATTR_TEMPERATURE]


async def test_failing_switch_rolls_back_optimistic_state(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A timeout leaves the switch showing the device, not the attempted value."""
    await _setup(hass, mock_config_entry)
    mock_device.set_force_dhw.side_effect = TimeoutError()

    with pytest.raises(HomeAssistantError):
        await _service(SWITCH_DOMAIN, SERVICE_TURN_ON, SWITCH)(hass)

    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_unexpected_exception_is_not_wrapped(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A bug (not a cloud failure) keeps its own type so it can be diagnosed."""
    await _setup(hass, mock_config_entry)
    mock_device.set_force_dhw.side_effect = KeyError("bug")

    with pytest.raises(KeyError):
        await _service(SWITCH_DOMAIN, SERVICE_TURN_ON, SWITCH)(hass)


@pytest.mark.parametrize(
    ("method", "value"),
    [("async_set_hvac_mode", HVACMode.DRY), ("async_set_preset_mode", "nope")],
)
async def test_unsupported_value_is_a_validation_error(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    method: str,
    value: str,
) -> None:
    """A mode the device lacks is the caller's fault: ServiceValidationError.

    Home Assistant's own service validation normally stops these first, so the
    entity's guard is called directly.
    """
    await _setup(hass, mock_config_entry)
    entity = hass.data[CLIMATE_COMPONENT].get_entity(CLIMATE)

    with pytest.raises(ServiceValidationError) as exc_info:
        await getattr(entity, method)(value)

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "unsupported_value"
    assert exc_info.value.translation_placeholders == {"value": str(value)}
    mock_device.set_mode.assert_not_awaited()
    mock_device.set_special_status.assert_not_awaited()


async def test_successful_commands_are_unchanged(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Working commands still reach the device and show their optimistic state."""
    await _setup(hass, mock_config_entry)

    await _service(SWITCH_DOMAIN, SERVICE_TURN_ON, SWITCH)(hass)
    await _service("button", "press", BUTTON)(hass)
    await _service(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, CLIMATE, **{ATTR_HVAC_MODE: HVACMode.OFF}
    )(hass)

    mock_device.set_force_dhw.assert_awaited_once_with(aioaquarea.ForceDHW.ON)
    mock_device.request_defrost.assert_awaited_once()
    mock_device.set_mode.assert_awaited_once()
    assert hass.states.get(SWITCH).state == STATE_ON
