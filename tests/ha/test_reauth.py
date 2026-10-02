"""Reauthentication end to end: failed setup, reauth flow, working entry.

The config flow tests cover the reauth form on its own. This one follows the
whole path a user takes when Panasonic rejects the stored password: setup
fails, Home Assistant starts a reauth flow, the user enters the new password,
and the entry must end up loaded, without restarting Home Assistant.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import aioaquarea
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aquarea.const import DOMAIN

NEW_PASSWORD = "another-placeholder"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Entry stays in setup_error after a successful reauth: "
        "https://github.com/wpatrik14/home-assistant-aquarea/issues/89"
    ),
)
async def test_reauth_after_failed_setup_loads_entry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A successful reauth sets the entry up again with the new password."""
    mock_aquarea_client.login.side_effect = aioaquarea.AuthenticationError(
        aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD, "rejected"
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"

    mock_aquarea_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.heat_pump_house").state == "heat"


async def test_reauth_while_loaded_reloads_entry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Reauth for a loaded entry (rejected during polling) reloads it."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    logins = mock_aquarea_client.login.await_count

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.state is ConfigEntryState.LOADED
    # One login to validate the password in the flow, one for the reload.
    assert mock_aquarea_client.login.await_count == logins + 2
