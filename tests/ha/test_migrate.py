"""Migration of config entries, through Home Assistant's entry manager.

An entry stored with a newer version than the flow supports (restored backup,
or an older release installed again) must load instead of ending in
`migration_error`. Home Assistant itself refuses a newer *major* version before
it calls `async_migrate_entry` (config_entries.py, ConfigEntry.async_migrate),
so only a newer minor version reaches the hook through the entry manager; the
newer major version is checked by calling the hook directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea import async_migrate_entry
from custom_components.aquarea.config_flow import AquareaConfigFlow


async def test_current_version_untouched(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An entry at the supported version is left as it is."""
    mock_config_entry.add_to_hass(hass)
    data = dict(mock_config_entry.data)

    assert await async_migrate_entry(hass, mock_config_entry)
    assert mock_config_entry.version == AquareaConfigFlow.VERSION
    assert dict(mock_config_entry.data) == data


async def test_newer_major_version_is_set_back(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A newer major version keeps its data and gets the supported version."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, version=AquareaConfigFlow.VERSION + 1
    )
    data = dict(mock_config_entry.data)

    assert await async_migrate_entry(hass, mock_config_entry)
    assert mock_config_entry.version == AquareaConfigFlow.VERSION
    assert mock_config_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert dict(mock_config_entry.data) == data
    assert "newer than the supported" in caplog.text


async def test_newer_minor_version_loads(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry with a newer minor version is set back and loads."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, minor_version=AquareaConfigFlow.MINOR_VERSION + 1
    )
    data = dict(mock_config_entry.data)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.version == AquareaConfigFlow.VERSION
    assert mock_config_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert dict(mock_config_entry.data) == data
    assert "newer than the supported" in caplog.text


async def test_newer_version_without_credentials_fails(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A newer entry without a username is refused."""
    entry = MockConfigEntry(domain="aquarea", data={}, version=2)
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert "unusable" in caplog.text
