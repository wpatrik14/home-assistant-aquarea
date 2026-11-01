"""A login Panasonic refuses is retried with a growing delay, not every poll.

Panasonic's edge security can answer the login with an HTTP 403 "request is
blocked" page (#165). aioaquarea raises that as an AuthenticationError with the
code API_ERROR. The coordinator used to start another login on every 60 second
poll; it now waits 5 minutes after the first refusal, doubling up to 30.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

BLOCKED = aioaquarea.AuthenticationError(
    aioaquarea.AuthenticationErrorCodes.API_ERROR,
    "Error in authorize: Unexpected status code 403",
)


async def _poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int
) -> None:
    """Advance the clock one minute at a time, so every poll runs."""
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_refused_login_is_not_retried_every_poll(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refused login backs off, keeps the entry and recovers once it works."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator = next(iter(mock_config_entry.runtime_data.values()))
    base = mock_aquarea_client.login.await_count

    # The session is gone and Panasonic refuses the next login.
    mock_aquarea_client.is_logged = False
    mock_aquarea_client.login.side_effect = BLOCKED
    await _poll(hass, freezer, 1)
    assert mock_aquarea_client.login.await_count == base + 1
    assert not coordinator.last_update_success

    # The next polls fail without another login: the first wait is 5 minutes.
    await _poll(hass, freezer, 3)
    assert mock_aquarea_client.login.await_count == base + 1
    assert not coordinator.last_update_success
    assert mock_config_entry.state is ConfigEntryState.LOADED

    # Once it is over the login is tried again, and refused again.
    await _poll(hass, freezer, 3)
    assert mock_aquarea_client.login.await_count == base + 2

    # The second wait is 10 minutes: no further attempt within the next 9.
    await _poll(hass, freezer, 9)
    assert mock_aquarea_client.login.await_count == base + 2

    # A login that works after the wait restores the data.
    mock_aquarea_client.login.side_effect = None
    await _poll(hass, freezer, 3)
    assert mock_aquarea_client.login.await_count == base + 3
    assert coordinator.last_update_success
