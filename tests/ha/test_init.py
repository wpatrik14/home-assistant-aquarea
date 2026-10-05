"""Setup and unload of a config entry, through Home Assistant's entry manager.

These describe behaviour, not structure: they look at the entry's state, the
entities and devices Home Assistant ends up with, and the flows it starts. So
they keep passing when code moves between modules (entity.py) or the entry's
data moves (`hass.data` to `entry.runtime_data`), which is what they guard.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquarea.const import CONF_CONSUMPTION_INTERVAL, DOMAIN

from .conftest import DEVICE_ID

# One of each platform, for an idle device in heating mode with a tank.
EXPECTED_STATES = {
    "climate.heat_pump_house": "heat",
    "water_heater.heat_pump_tank": "idle",
    "sensor.heat_pump_outdoor_temperature": "7",
    "sensor.heat_pump_tank_temperature": "48",
    "binary_sensor.heat_pump_status": "off",
    "switch.heat_pump_force_dhw": "off",
    "select.heat_pump_quiet_mode": "off",
    "button.heat_pump_request_defrost": "unknown",
}


def _auth_error(code: aioaquarea.AuthenticationErrorCodes) -> Exception:
    return aioaquarea.AuthenticationError(code, "message from the cloud")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
        and flow["context"].get("entry_id") == entry.entry_id
    ]


async def test_setup_entry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The entry loads, with one device and entities on every platform."""
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_aquarea_client.login.assert_awaited_once()
    mock_aquarea_client.get_devices.assert_awaited_once()

    devices = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == {(DOMAIN, DEVICE_ID)}
    assert device.manufacturer == "Panasonic"
    assert device.model == "WH-TEST"

    for entity_id, state in EXPECTED_STATES.items():
        assert hass.states.get(entity_id).state == state, entity_id

    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert {entry.domain for entry in entries} == {
        "binary_sensor",
        "button",
        "climate",
        "select",
        "sensor",
        "switch",
        "water_heater",
    }
    assert all(entry.device_id == device.id for entry in entries)


async def test_setup_entry_without_tank(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device_info: MagicMock,
    mock_device: MagicMock,
) -> None:
    """A device without a tank gets no tank entities."""
    mock_device_info.has_tank = False
    mock_device.has_tank = False
    mock_device.tank = None

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.heat_pump_house") is not None
    assert hass.states.async_entity_ids("water_heater") == []
    assert hass.states.get("sensor.heat_pump_tank_temperature") is None
    assert hass.states.get("switch.heat_pump_force_dhw") is None


@pytest.mark.parametrize(
    "code",
    [
        aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
        aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
    ],
)
async def test_setup_entry_invalid_credentials_start_reauth(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    code: aioaquarea.AuthenticationErrorCodes,
) -> None:
    """Rejected credentials fail setup and start a reauth flow."""
    mock_aquarea_client.login.side_effect = _auth_error(code)

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass, mock_config_entry)) == 1


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            _auth_error(aioaquarea.AuthenticationErrorCodes.SESSION_CLOSED),
            id="session_closed",
        ),
        pytest.param(
            _auth_error(aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED),
            id="token_expired",
        ),
        pytest.param(aioaquarea.ApiError("E1", "maintenance"), id="api_error"),
        pytest.param(aioaquarea.RequestFailedError("bad gateway"), id="request_failed"),
        pytest.param(aiohttp.ClientError(), id="aiohttp_error"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
async def test_setup_entry_cloud_error_retries(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    """A transient login or cloud failure means setup is retried later."""
    mock_aquarea_client.login.side_effect = error

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass, mock_config_entry) == []


async def test_setup_entry_first_refresh_cloud_error_retries(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A failing first poll is retried too, not reported as a loaded entry."""
    mock_device.refresh_data.side_effect = aioaquarea.RequestFailedError("down")

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_first_refresh_auth_error_starts_reauth(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Credentials rejected during the first poll also start a reauth flow."""
    mock_aquarea_client.get_device.side_effect = _auth_error(
        aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS
    )

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass, mock_config_entry)) == 1


async def test_unload_entry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Unloading leaves the entities unavailable and the entry not loaded."""
    await _setup(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    for entity_id in EXPECTED_STATES:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id


async def test_unload_and_set_up_again(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An unloaded entry can be set up again in the same Home Assistant run."""
    await _setup(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.heat_pump_house").state == "heat"


async def test_options_change_reloads_entry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Changing the options reloads the entry, so the new interval applies."""
    await _setup(hass, mock_config_entry)
    assert mock_aquarea_client.login.await_count == 1

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_CONSUMPTION_INTERVAL: 15}
    )
    await hass.async_block_till_done()

    assert mock_config_entry.options == {CONF_CONSUMPTION_INTERVAL: 15}
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_aquarea_client.login.await_count == 2


async def test_poll_updates_entities(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The coordinator polls every minute and the entities follow."""
    await _setup(hass, mock_config_entry)
    polls = mock_aquarea_client.get_device.await_count

    mock_device.temperature_outdoor = 3
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert mock_aquarea_client.get_device.await_count == polls + 1
    assert hass.states.get("sensor.heat_pump_outdoor_temperature").state == "3"


@pytest.mark.parametrize(
    ("data", "options", "expected"),
    [
        pytest.param({}, {}, 60, id="default"),
        pytest.param({}, {CONF_CONSUMPTION_INTERVAL: 15}, 15, id="option"),
        # Entries created while the interval was still asked at setup.
        pytest.param({CONF_CONSUMPTION_INTERVAL: 45}, {}, 45, id="legacy_data"),
        pytest.param(
            {CONF_CONSUMPTION_INTERVAL: 45},
            {CONF_CONSUMPTION_INTERVAL: 15},
            15,
            id="option_over_legacy_data",
        ),
    ],
)
async def test_consumption_interval_source(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    data: dict,
    options: dict,
    expected: int,
) -> None:
    """The option wins, then a value from the entry data, then 60 minutes."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, **data}, options=options
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    [coordinator] = mock_config_entry.runtime_data.values()
    assert coordinator.consumption_interval == expected
