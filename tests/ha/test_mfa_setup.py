"""Setup with a stored refresh token: reuse, rotation, and MFA needed again.

`aioaquarea.Client` is an AsyncMock (conftest); the library's own behaviour is
tested in aioaquarea-ng. These check how the integration creates the client
and what it does with the tokens the client reports.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aioaquarea
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea import _create_client
from custom_components.aquarea.const import CONF_REFRESH_TOKEN, DOMAIN

from .conftest import PASSWORD, USERNAME

USER_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
TOKEN = "refresh-token-placeholder"
NEW_TOKEN = "rotated-token-placeholder"


async def test_setup_passes_stored_refresh_token_and_never_texts_a_code(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """Setup hands the stored token to the client and never texts a code unasked."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={**USER_INPUT, CONF_REFRESH_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    with patch("aioaquarea.Client", return_value=mock_aquarea_client) as client_cls:
        client = _create_client(hass, entry)

    assert client is mock_aquarea_client
    args, kwargs = client_cls.call_args
    assert args[1:] == (USERNAME, PASSWORD)
    assert kwargs["refresh_token"] == TOKEN
    assert kwargs["mfa_send_code"] is False


async def test_setup_without_token_uses_the_password(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An entry without a token logs in with the password, as before."""
    with patch("aioaquarea.Client", return_value=mock_aquarea_client) as client_cls:
        _create_client(hass, mock_config_entry)
    assert client_cls.call_args.kwargs["refresh_token"] is None


async def test_rotated_refresh_token_is_stored(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The library's callback keeps the entry's token current; unchanged is a no-op."""
    mock_config_entry.add_to_hass(hass)
    with patch("aioaquarea.Client", return_value=mock_aquarea_client) as client_cls:
        _create_client(hass, mock_config_entry)
    store = client_cls.call_args.kwargs["refresh_token_callback"]

    store(TOKEN)
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == TOKEN
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD

    with patch.object(hass.config_entries, "async_update_entry") as update:
        store(TOKEN)
    update.assert_not_called()

    store(NEW_TOKEN)
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == NEW_TOKEN


async def test_setup_with_stored_token_loads_without_password_login(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """The entry loads on the stored token; the client (not the flow) decides on MFA."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={**USER_INPUT, CONF_REFRESH_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_needing_mfa_again_starts_reauth(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """The refresh token died and the password login wants MFA: reauth, no retry loop."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={**USER_INPUT, CONF_REFRESH_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    mock_aquarea_client.login.side_effect = aioaquarea.MfaRequiredError(
        aioaquarea.MfaChallenge("sms", "***62", ("sms",), False)
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
