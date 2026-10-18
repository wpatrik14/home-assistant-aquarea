"""Tests for the config entry and device diagnostics."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aioaquarea
from aioaquarea.data import FaultError
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID, LONG_ID, PASSWORD, USERNAME


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device_info: MagicMock,
    device: MagicMock,
) -> None:
    # Explicit values for the properties aioaquarea-ng 1.4.0 added, which the
    # spec'd mock would otherwise return as truthy MagicMocks.
    device_info.status_data_mode = aioaquarea.StatusDataMode.LIVE
    device_info.connection_status = 0
    device_info.dhw_flag = False
    device.water_pressure = 1.5
    device.cool_mode = 0
    device.bivalent = 0
    device.bivalent_actual = 1
    device.external_heater = None
    device.electric_anode = 0
    device.control_box = 0
    device.uncontrollable = False
    device.service_type = "typeA"
    device.multi_od_connection = 0
    device.model_series_selection = 2
    device.stand_alone = 0
    zone = device.zones[1]
    zone.zone_type_id = 1
    zone.zone_sensor_id = 0
    zone.sensor_mode = aioaquarea.data.ZoneSensor.WATER_TEMPERATURE
    zone.cool_mode = False
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _assert_no_sensitive(result: dict) -> None:
    dumped = json.dumps(result)
    for secret in (
        USERNAME,
        USERNAME.lower(),
        DEVICE_ID,
        LONG_ID,
        "Heat pump",
        "House",
        PASSWORD,
    ):
        assert secret not in dumped


async def test_config_entry_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device_info: MagicMock,
    mock_device: MagicMock,
) -> None:
    """The entry diagnostics hold the device state and no account or ids."""
    mock_device.current_error = FaultError(error_message="Low flow", error_code="H62")
    await _setup(hass, mock_config_entry, mock_device_info, mock_device)

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    _assert_no_sensitive(result)
    assert result["entry"]["data"]["username"] == REDACTED
    assert result["entry"]["data"]["password"] == REDACTED
    assert {"integration", "home_assistant", "library"} <= set(result["versions"])
    assert result["versions"]["library"]

    (device,) = result["devices"]
    assert device["coordinator"]["consumption_interval"] == 15
    assert device["coordinator"]["update_interval"] == 60
    assert device["coordinator"]["last_update_success"] is True
    assert device["coordinator"]["last_exception"] is None
    assert device["device_info"]["model"] == "WH-TEST"
    assert device["device_info"]["has_tank"] is True
    assert device["device_info"]["status_data_mode"] == "LIVE"
    assert device["device_info"]["device_id"] == REDACTED
    assert device["status"]["mode"] == "HEAT"
    assert device["status"]["bivalent_actual"] == 1
    assert device["status"]["external_heater"] is None
    assert device["status"]["faults"] == [{"code": "H62", "message": "Low flow"}]
    assert device["tank"]["target_temperature"] == 50
    (zone,) = device["zones"]
    assert zone["name"] == REDACTED
    assert zone["temperature"] == 21
    assert zone["sensor_mode"] == "WATER_TEMPERATURE"
    assert zone["supports_set_temperature"] is True


async def test_diagnostics_without_tank_fault_or_library(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device_info: MagicMock,
    mock_device: MagicMock,
) -> None:
    """Missing tank, fault and library metadata show up as empty values."""
    mock_device.tank = None
    mock_device.current_error = None
    await _setup(hass, mock_config_entry, mock_device_info, mock_device)
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]
    coordinator.update_interval = None
    coordinator.last_exception = RuntimeError("boom with a secret")

    with patch(
        "custom_components.aquarea.diagnostics.package_version",
        side_effect=PackageNotFoundError,
    ):
        result = await get_diagnostics_for_config_entry(
            hass, hass_client, mock_config_entry
        )

    assert result["versions"]["library"] is None
    (device,) = result["devices"]
    assert device["tank"] is None
    assert device["status"]["faults"] == []
    assert device["coordinator"]["update_interval"] is None
    assert device["coordinator"]["last_exception"] == "RuntimeError"
    assert "secret" not in json.dumps(result)


async def test_device_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device_info: MagicMock,
    mock_device: MagicMock,
) -> None:
    """The device diagnostics describe the coordinator of that device."""
    await _setup(hass, mock_config_entry, mock_device_info, mock_device)
    device_entry = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE_ID), mock_config_entry.entry_id
    )
    assert device_entry is not None

    result = await get_diagnostics_for_device(
        hass, hass_client, mock_config_entry, device_entry
    )

    _assert_no_sensitive(result)
    assert "library" in result["versions"]
    (device,) = result["devices"]
    assert device["device_info"]["model"] == "WH-TEST"
    assert device["status"]["temperature_outdoor"] == 7


async def test_device_diagnostics_other_device_is_empty(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device_info: MagicMock,
    mock_device: MagicMock,
) -> None:
    """A device of the entry that has no coordinator gets no device block."""
    await _setup(hass, mock_config_entry, mock_device_info, mock_device)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "other-device")},
    )

    result = await get_diagnostics_for_device(
        hass, hass_client, mock_config_entry, other
    )

    assert result["devices"] == []
