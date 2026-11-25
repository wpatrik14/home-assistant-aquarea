"""Panasonic rejecting the app version is a repair issue, not a reauth.

aioaquarea-ng raises an AuthenticationError with the code APP_VERSION_REJECTED
when Panasonic's login gate rejects the app version the library announces. The
password is fine then, so asking for it again would be wrong. The integration
keeps retrying and shows a repair issue until a login works. The error is built
here as a plain AuthenticationError with the code: releases before
AppVersionRejectedError never raise it, and the integration compares the value.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquarea.const import DOMAIN
from custom_components.aquarea.issues import ISSUES_URL

ERROR = aioaquarea.AuthenticationError(
    "APP_VERSION_REJECTED", "Panasonic rejected the app version"
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"app_version_rejected_{entry.entry_id}"
    )


def _reauth_flows(hass: HomeAssistant) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


@pytest.mark.parametrize("failing_call", ["login", "get_device"])
async def test_setup_retries_and_raises_repair_issue(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    failing_call: str,
) -> None:
    """Setup is retried (not failed for good), with no reauth and a repair issue."""
    getattr(mock_aquarea_client, failing_call).side_effect = ERROR

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []
    issue = _issue(hass, mock_config_entry)
    assert issue is not None
    assert issue.translation_key == "app_version_rejected"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.learn_more_url == ISSUES_URL
    assert issue.translation_placeholders == {"issues_url": ISSUES_URL}


async def test_issue_is_cleared_by_a_successful_login(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Once a login works again (an update, or Panasonic relented) the issue goes."""
    mock_aquarea_client.login.side_effect = ERROR
    await _setup(hass, mock_config_entry)
    assert _issue(hass, mock_config_entry) is not None

    mock_aquarea_client.login.side_effect = None
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _issue(hass, mock_config_entry) is None


async def test_removing_the_entry_removes_the_issue(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A removed entry leaves no repair issue behind."""
    mock_aquarea_client.login.side_effect = ERROR
    await _setup(hass, mock_config_entry)
    assert _issue(hass, mock_config_entry) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _issue(hass, mock_config_entry) is None


async def test_poll_raises_issue_without_reauth_and_recovers(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A poll hitting the rejection keeps the entry, shows the issue, then recovers."""
    await _setup(hass, mock_config_entry)
    assert _issue(hass, mock_config_entry) is None

    mock_device.refresh_data.side_effect = ERROR
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert _reauth_flows(hass) == []
    assert _issue(hass, mock_config_entry) is not None

    mock_device.refresh_data.side_effect = None
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _issue(hass, mock_config_entry) is None


async def test_rejected_credentials_raise_no_app_version_issue(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A wrong password still means reauth, and no app version issue."""
    mock_aquarea_client.login.side_effect = aioaquarea.AuthenticationError(
        aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD, "no"
    )

    await _setup(hass, mock_config_entry)

    assert len(_reauth_flows(hass)) == 1
    assert _issue(hass, mock_config_entry) is None
