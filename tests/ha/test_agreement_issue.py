"""Panasonic wanting its new terms accepted is a repair issue, not a reauth.

Only the user can accept them, in the Panasonic Comfort Cloud app. The issue
says so, and goes away by itself once a request works again (#152).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquarea.const import DOMAIN
from custom_components.aquarea.issues import ISSUES_URL


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"agreement_not_accepted_{entry.entry_id}"
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_raises_issue_and_clears_it(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setup retries with a repair issue; a working setup removes it."""
    mock_aquarea_client.get_devices.side_effect = aioaquarea.AgreementNotAcceptedError()

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.translation_key == "agreement_not_accepted"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.learn_more_url == ISSUES_URL
    assert issue.translation_placeholders == {
        "entry_title": mock_config_entry.title,
        "issues_url": ISSUES_URL,
    }

    mock_aquarea_client.get_devices.side_effect = None
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _issue(hass, mock_config_entry) is None


async def test_poll_raises_issue_and_recovers(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A poll hitting it keeps the entry, shows the issue, then recovers."""
    await _setup(hass, mock_config_entry)
    assert _issue(hass, mock_config_entry) is None

    mock_device.refresh_data.side_effect = aioaquarea.AgreementNotAcceptedError()
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _issue(hass, mock_config_entry) is not None

    mock_device.refresh_data.side_effect = None
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _issue(hass, mock_config_entry) is None


async def test_removing_the_entry_removes_the_issue(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A removed entry leaves no repair issue behind."""
    mock_aquarea_client.get_devices.side_effect = aioaquarea.AgreementNotAcceptedError()
    await _setup(hass, mock_config_entry)
    assert _issue(hass, mock_config_entry) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, mock_config_entry) is None
