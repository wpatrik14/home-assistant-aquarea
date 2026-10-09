"""Multi-factor authentication: code entry in the config and reauth flows.

`aioaquarea.Client` is an AsyncMock (conftest), so these tests drive the
integration's side only: the `mfa_sms` / `mfa_otp` steps, the error mapping,
and how the refresh token Panasonic returns is stored and reused.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aioaquarea
import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea.config_flow import AquareaConfigFlow
from custom_components.aquarea.const import CONF_CODE, CONF_REFRESH_TOKEN, DOMAIN

from .conftest import PASSWORD, USERNAME

Codes = aioaquarea.AuthenticationErrorCodes
USER_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
TOKEN = "refresh-token-placeholder"
NEW_TOKEN = "rotated-token-placeholder"
CODE = "123456"
SMS = aioaquarea.MfaChallenge("sms", "***62", ("sms",), True)
OTP = aioaquarea.MfaChallenge("otp", None, ("otp",), False)


@pytest.fixture(autouse=True)
def mock_setup_entry():
    """Keep a created or updated entry from being set up for real."""
    with patch("custom_components.aquarea.async_setup_entry", return_value=True) as m:
        yield m


def _mfa(challenge: aioaquarea.MfaChallenge) -> aioaquarea.MfaRequiredError:
    return aioaquarea.MfaRequiredError(challenge)


def _error(code: str) -> aioaquarea.AuthenticationError:
    return aioaquarea.AuthenticationError(code, "message from the cloud")


async def _start_user_flow(hass: HomeAssistant, challenge) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)


@pytest.mark.parametrize(
    ("challenge", "step_id", "fields"),
    [
        (SMS, "mfa_sms", {CONF_CODE, "resend_code"}),
        (OTP, "mfa_otp", {CONF_CODE}),
    ],
)
async def test_user_flow_with_mfa(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    challenge: aioaquarea.MfaChallenge,
    step_id: str,
    fields: set[str],
) -> None:
    """The login stops at MFA: ask for the code, then create the entry with the token."""
    mock_aquarea_client.login.side_effect = _mfa(challenge)

    result = await _start_user_flow(hass, challenge)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    assert {str(key) for key in result["data_schema"].schema} == fields
    assert result["description_placeholders"]["destination"] in ("***62", "?")
    mock_aquarea_client.complete_mfa.assert_not_awaited()

    async def accept(code: str) -> None:
        mock_aquarea_client.refresh_token = TOKEN

    mock_aquarea_client.complete_mfa.side_effect = accept
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: f" {CODE} "}
    )
    await hass.async_block_till_done()

    mock_aquarea_client.complete_mfa.assert_awaited_once_with(CODE)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == {**USER_INPUT, CONF_REFRESH_TOKEN: TOKEN}
    assert result["result"].unique_id == USERNAME.lower()


async def test_wrong_code_then_right_code(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """A wrong code re-shows the form; the next, right code finishes."""
    mock_aquarea_client.login.side_effect = _mfa(OTP)
    result = await _start_user_flow(hass, OTP)

    mock_aquarea_client.complete_mfa.side_effect = _error("MFA_INVALID_CODE")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "000000"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_otp"
    assert result["errors"] == {"base": "invalid_mfa_code"}

    mock_aquarea_client.complete_mfa.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == USER_INPUT  # a client without a refresh token


async def test_empty_code_is_invalid(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """The SMS form's code is optional (the resend box can be used alone)."""
    mock_aquarea_client.login.side_effect = _mfa(SMS)
    result = await _start_user_flow(hass, SMS)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "  "}
    )

    assert result["errors"] == {"base": "invalid_mfa_code"}
    mock_aquarea_client.complete_mfa.assert_not_awaited()
    mock_aquarea_client.resend_mfa_code.assert_not_awaited()


async def test_resend_sms_code(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """Ticking the resend box sends a new SMS and shows the form again."""
    mock_aquarea_client.login.side_effect = _mfa(SMS)
    result = await _start_user_flow(hass, SMS)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"resend_code": True}
    )

    mock_aquarea_client.resend_mfa_code.assert_awaited_once()
    mock_aquarea_client.complete_mfa.assert_not_awaited()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_sms"
    assert not result["errors"]


async def test_resend_failure_is_reported(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """A failing resend is reported on the form, not swallowed."""
    mock_aquarea_client.login.side_effect = _mfa(SMS)
    result = await _start_user_flow(hass, SMS)
    mock_aquarea_client.resend_mfa_code.side_effect = _error("API_ERROR")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"resend_code": True}
    )

    assert result["step_id"] == "mfa_sms"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("failing", ["complete_mfa", "resend_mfa_code"])
async def test_expired_restarts_login(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, failing: str
) -> None:
    """An expired MFA transaction goes back to the password form."""
    mock_aquarea_client.login.side_effect = _mfa(SMS)
    result = await _start_user_flow(hass, SMS)
    getattr(mock_aquarea_client, failing).side_effect = _error("MFA_EXPIRED")

    user_input = (
        {"resend_code": True} if failing == "resend_mfa_code" else {CONF_CODE: CODE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "mfa_expired"}

    # logging in again starts a new challenge
    mock_aquarea_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_error("API_ERROR"), "cannot_connect"),
        (aioaquarea.ApiError("E1", "maintenance"), "cannot_connect"),
        (aioaquarea.RequestFailedError("bad gateway"), "cannot_connect"),
        (aiohttp.ClientError(), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (ValueError("boom"), "unknown"),
    ],
)
async def test_mfa_unexpected_errors_allow_retry(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """Other failures while checking the code keep the form open for a retry."""
    mock_aquarea_client.login.side_effect = _mfa(OTP)
    result = await _start_user_flow(hass, OTP)
    mock_aquarea_client.complete_mfa.side_effect = error

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE}
    )

    assert result["step_id"] == "mfa_otp"
    assert result["errors"] == {"base": expected}


async def test_login_reports_expired_mfa_request(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """An MFA_EXPIRED from the login itself is shown on the password form."""
    mock_aquarea_client.login.side_effect = _error("MFA_EXPIRED")
    result = await _start_user_flow(hass, SMS)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "mfa_expired"}


async def test_unsupported_mfa_page_keeps_the_old_error(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """Push notifications and the like: a plain MFA_REQUIRED without a challenge."""
    mock_aquarea_client.login.side_effect = _error("MFA_REQUIRED")
    result = await _start_user_flow(hass, SMS)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "mfa_required"}


async def test_mfa_step_without_pending_login(hass: HomeAssistant) -> None:
    """A flow without a pending challenge (restored, restarted) starts over."""
    flow = AquareaConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    result = await flow.async_step_mfa_otp({CONF_CODE: CODE})
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "mfa_expired"}


async def test_mfa_step_shows_form_without_input(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """Reaching the step without input shows its form."""
    flow = AquareaConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow._challenge = OTP  # noqa: SLF001
    flow._api = mock_aquarea_client  # noqa: SLF001
    result = await flow.async_step_mfa_otp()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa_otp"


# --- reauth ------------------------------------------------------------------


@pytest.mark.parametrize("challenge", [SMS, OTP])
async def test_reauth_with_mfa(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    challenge: aioaquarea.MfaChallenge,
) -> None:
    """Reauth: password, then the code; the new token replaces the old one."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_REFRESH_TOKEN: "old"}
    )
    mock_aquarea_client.login.side_effect = _mfa(challenge)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    assert result["step_id"] == f"mfa_{challenge.factor}"

    async def accept(code: str) -> None:
        mock_aquarea_client.refresh_token = NEW_TOKEN

    mock_aquarea_client.complete_mfa.side_effect = accept
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: "new-password",
        CONF_REFRESH_TOKEN: NEW_TOKEN,
    }


async def test_reauth_without_new_token_drops_the_old_one(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A reauth login that returns no token must not keep the dead one."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_REFRESH_TOKEN: "old"}
    )
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["reason"] == "reauth_successful"
    assert CONF_REFRESH_TOKEN not in mock_config_entry.data


async def test_reauth_expired_returns_to_password_form(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An expired MFA transaction during reauth asks for the password again."""
    mock_config_entry.add_to_hass(hass)
    mock_aquarea_client.login.side_effect = _mfa(SMS)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    mock_aquarea_client.complete_mfa.side_effect = _error("MFA_EXPIRED")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE}
    )

    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "mfa_expired"}
    assert result["description_placeholders"]["username"] == USERNAME
