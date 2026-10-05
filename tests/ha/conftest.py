"""Shared fixtures for the pytest suite.

These tests run on pytest-homeassistant-custom-component, which provides a real
`hass`, Home Assistant's flow manager and `MockConfigEntry`. Nothing here talks
to the Panasonic cloud. `aioaquarea.Client` is replaced with an `AsyncMock`, the
credentials below are placeholders, and the plugin blocks sockets during tests
(pytest-socket), so an unmocked call fails the test instead of reaching the
network.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import aioaquarea
from aioaquarea.data import DeviceZone
from homeassistant.components.recorder import Recorder
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea.const import DOMAIN

USERNAME = "User@Example.com"
PASSWORD = "not-a-real-password"
DEVICE_ID = "test-device-id"
LONG_ID = "test-long-id"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: None
) -> None:
    """Let Home Assistant load the integration from custom_components/.

    The manifest lists `recorder` as a dependency, and Home Assistant sets
    dependencies up before it even starts a config flow, so every test needs
    the plugin's in-memory recorder. `recorder_mock` has to be requested
    before `hass`, which an autouse fixture guarantees.
    """


def _make_zone() -> MagicMock:
    zone = MagicMock(spec=DeviceZone)
    zone.zone_id = 1
    zone.name = "House"
    zone.operation_status = aioaquarea.OperationStatus.ON
    zone.temperature = 21
    zone.supports_set_temperature = True
    zone.heat_target_temperature = 22
    zone.cool_target_temperature = 24
    zone.heat_min = 10
    zone.heat_max = 30
    zone.cool_min = 18
    zone.cool_max = 30
    return zone


def _make_tank() -> MagicMock:
    tank = MagicMock(spec=aioaquarea.Tank)
    tank.operation_status = aioaquarea.OperationStatus.ON
    tank.temperature = 48
    tank.target_temperature = 50
    tank.heat_min = 40
    tank.heat_max = 65
    return tank


@pytest.fixture
def mock_device_info() -> MagicMock:
    """The DeviceInfo returned by `client.get_devices()`."""
    info = MagicMock(spec=aioaquarea.DeviceInfo)
    info.device_id = DEVICE_ID
    info.long_id = LONG_ID
    info.name = "Heat pump"
    info.model = "WH-TEST"
    info.firmware_version = "1.0.0"
    info.has_tank = True
    return info


@pytest.fixture
def mock_device() -> MagicMock:
    """An idle, heating-mode device with one zone and a tank.

    Built on `spec=aioaquarea.Device`, so its async methods (`refresh_data`,
    `set_mode`, ...) are AsyncMocks and an attribute the library doesn't
    have raises AttributeError instead of returning a fresh mock.
    """
    device = MagicMock(spec=aioaquarea.Device)
    device.device_id = DEVICE_ID
    device.long_id = LONG_ID
    device.has_tank = True
    device.tank = _make_tank()
    device.zones = {1: _make_zone()}
    device.mode = aioaquarea.ExtendedOperationMode.HEAT
    device.operation_status = aioaquarea.OperationStatus.ON
    device.current_action = aioaquarea.DeviceAction.IDLE
    device.current_direction = aioaquarea.DeviceDirection.IDLE
    device.device_mode_status = aioaquarea.DeviceModeStatus.NORMAL
    device.pump_duty = aioaquarea.PumpDuty.OFF
    device.temperature_outdoor = 7
    device.is_on_error = False
    device.current_error = None
    device.quiet_mode = aioaquarea.QuietMode.OFF
    device.force_dhw = aioaquarea.ForceDHW.OFF
    device.force_heater = aioaquarea.ForceHeater.OFF
    device.holiday_timer = aioaquarea.HolidayTimer.OFF
    device.powerful_time = aioaquarea.PowerfulTime.OFF
    device.special_status = None
    device.support_special_status = True
    return device


@pytest.fixture
def mock_aquarea_client(
    mock_device_info: MagicMock, mock_device: MagicMock
) -> Generator[AsyncMock]:
    """Replace `aioaquarea.Client` with an AsyncMock that logs in successfully.

    Both the config flow and the entry setup call `aioaquarea.Client(...)`
    through the module attribute, so patching it there covers both. Tests
    change the behaviour per call, e.g. `client.login.side_effect = ...`.
    """
    client = AsyncMock(spec=aioaquarea.Client)
    client.is_logged = True
    client.get_devices.return_value = [mock_device_info]
    client.get_device.return_value = mock_device
    client.get_device_consumption.return_value = []
    with patch("aioaquarea.Client", return_value=client):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry as the current user step creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
