"""Config flow tests, driven through Home Assistant's flow manager.

Every step is started with `flow.async_init` and answered with
`flow.async_configure`, as the frontend does, so a submitted form goes to
`async_step_<step_id>`, which is the dispatch #65 got wrong. Only the last
test calls flow methods directly, for defensive branches the flow manager
cannot reach.
"""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import aiohttp
import aioaquarea
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aquarea.config_flow import AquareaConfigFlow
from custom_components.aquarea.const import CONF_CONSUMPTION_INTERVAL, DOMAIN

from .conftest import PASSWORD, USERNAME

NEW_PASSWORD = "another-placeholder"

USER_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep a created or updated entry from being set up for real."""
    with patch(
        "custom_components.aquarea.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def _auth_error(code: aioaquarea.AuthenticationErrorCodes) -> Exception:
    return aioaquarea.AuthenticationError(code, "message from the cloud")


LOGIN_ERRORS = [
    pytest.param(
        _auth_error(aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD),
        "invalid_auth",
        id="invalid_username_or_password",
    ),
    pytest.param(
        _auth_error(aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS),
        "invalid_auth",
        id="invalid_credentials",
    ),
    pytest.param(
        _auth_error(aioaquarea.AuthenticationErrorCodes.API_ERROR),
        "invalid_auth",
        id="other_auth_code",
    ),
    pytest.param(
        _auth_error(aioaquarea.AuthenticationErrorCodes.SESSION_CLOSED),
        "cannot_connect",
        id="session_closed",
    ),
    pytest.param(
        _auth_error(aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED),
        "cannot_connect",
        id="token_expired",
    ),
    pytest.param(
        aioaquarea.ApiError("E1", "maintenance"), "api_error", id="api_error"
    ),
    pytest.param(
        aioaquarea.RequestFailedError("bad gateway"),
        "cannot_connect",
        id="request_failed",
    ),
    pytest.param(aiohttp.ClientError(), "cannot_connect", id="aiohttp_error"),
    pytest.param(TimeoutError(), "cannot_connect", id="timeout"),
    pytest.param(ValueError("boom"), "unknown", id="unexpected"),
]


async def test_user_step_creates_entry(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, mock_setup_entry: AsyncMock
) -> None:
    """A successful login creates the entry, unique per lower-cased username."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    # Only what is needed to connect; the consumption interval is an option.
    assert set(result["data_schema"].schema) == {CONF_USERNAME, CONF_PASSWORD}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == USERNAME.lower()
    mock_aquarea_client.login.assert_awaited_once()
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(("error", "expected"), LOGIN_ERRORS)
async def test_user_step_error_then_recovery(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_setup_entry: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """A failed login re-shows the form with the error; a retry can succeed."""
    mock_aquarea_client.login.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}
    if expected == "api_error":
        assert result["description_placeholders"] == {
            "api_error_msg": str(error)
        }

    mock_aquarea_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == USER_INPUT


async def test_user_step_duplicate_account(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The same account, in any letter case, can only be added once."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_USERNAME: USERNAME.upper()}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_aquarea_client.login.assert_not_awaited()


async def test_reauth_updates_password(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
) -> None:
    """Reauth asks for the password on reauth_confirm and stores it."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: NEW_PASSWORD,
    }
    mock_aquarea_client.login.assert_awaited_once()


@pytest.mark.parametrize(("error", "expected"), LOGIN_ERRORS)
async def test_reauth_error_then_recovery(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """A rejected password re-shows reauth_confirm; a retry can succeed."""
    mock_config_entry.add_to_hass(hass)
    mock_aquarea_client.login.side_effect = error

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": expected}
    assert mock_config_entry.data[CONF_PASSWORD] == PASSWORD

    mock_aquarea_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == NEW_PASSWORD


async def test_reauth_falls_back_to_unique_id(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, mock_setup_entry: AsyncMock
) -> None:
    """An entry without a stored username reauthenticates as its unique ID."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME.lower()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        CONF_USERNAME: USERNAME.lower(),
        CONF_PASSWORD: NEW_PASSWORD,
    }


async def test_reauth_entry_without_unique_id(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, mock_setup_entry: AsyncMock
) -> None:
    """An entry with a stored username but no unique ID completes reauth.

    Entries from very old versions can lack a unique ID. Looking the entry up
    by unique ID found nothing there, and the flow failed on an assert after
    the new password had already been validated.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=None,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: NEW_PASSWORD,
    }

async def test_reauth_without_username_aborts(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """With no username and no unique ID there is nothing to reauthenticate."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PASSWORD: PASSWORD})
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_no_username"
    mock_aquarea_client.login.assert_not_awaited()


async def test_options_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The options form defaults to 60 minutes and stores the new value."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema = result["data_schema"].schema
    default = next(key for key in schema if key == CONF_CONSUMPTION_INTERVAL).default()
    assert default == 60

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONSUMPTION_INTERVAL: 15}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_CONSUMPTION_INTERVAL: 15}


async def test_options_flow_defaults_to_legacy_setup_value(
    hass: HomeAssistant,
) -> None:
    """Entries created when the interval was asked at setup keep that value.

    It sits in their data, not their options, so the form must default to it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME.lower(),
        data={**USER_INPUT, CONF_CONSUMPTION_INTERVAL: 45},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    schema = result["data_schema"].schema
    default = next(key for key in schema if key == CONF_CONSUMPTION_INTERVAL).default()
    assert default == 45


async def test_options_flow_defaults_to_current_option(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Once set, the option (not the setup value) is the form's default."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CONSUMPTION_INTERVAL: 20}
    )

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)

    schema = result["data_schema"].schema
    default = next(key for key in schema if key == CONF_CONSUMPTION_INTERVAL).default()
    assert default == 20


async def test_defensive_branches_unreachable_through_flow_manager(
    hass: HomeAssistant,
) -> None:
    """Branches the flow manager never takes, so they are called directly.

    The flow manager passes the same mapping as both `entry_data` and
    `init_data`, and `async_step_reauth` runs once per flow, so neither the
    init_data fallback nor the already-known username can be reached through
    it; nor can `reauth_confirm` without a username.
    """
    flow = AquareaConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_REAUTH}

    assert (await flow.async_step_reauth_confirm())["reason"] == "reauth_no_username"

    flow.init_data = {CONF_USERNAME: "from-init-data"}
    assert flow._try_get_username({}) == "from-init-data"
    # Now known, so it wins over anything in entry_data.
    assert flow._try_get_username({CONF_USERNAME: "other"}) == "from-init-data"
