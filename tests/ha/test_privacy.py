"""The Panasonic ID (an e-mail address) must not leak into names or logs."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aioaquarea
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import DEVICE_ID, USERNAME


async def test_coordinator_name_has_no_username(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The coordinator name ends up in Home Assistant's log lines."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]

    assert USERNAME.lower() not in coordinator.name.lower()
    assert DEVICE_ID in coordinator.name


async def test_failed_update_logs_no_username(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An update failure is logged by the coordinator under its name."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]

    mock_aquarea_client.get_device.side_effect = aioaquarea.ApiError("HTTP_500", "boom")
    caplog.clear()
    await coordinator.async_refresh()

    assert "Error fetching" in caplog.text
    assert USERNAME.lower() not in caplog.text.lower()
