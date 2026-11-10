"""Errors raised to the user carry translation keys, not English text.

Home Assistant shows `translation_key` (with placeholders) in the entry's
state and in the update failure, in the user's language; the same keys must
exist in strings.json and in every translation.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID

COMPONENT = Path(__file__).parents[2] / "custom_components" / "aquarea"
KEYS = {
    "invalid_credentials",
    "mfa_required",
    "app_version_rejected",
    "agreement_not_accepted",
    "authentication_failed",
    "cloud_error",
    "cannot_connect",
    "login_backoff",
}


@pytest.mark.parametrize(
    "path",
    [
        "strings.json",
        *(
            f"translations/{p.name}"
            for p in (COMPONENT / "translations").glob("*.json")
        ),
    ],
)
def test_every_error_key_is_translated(path: str) -> None:
    """strings.json and each translation define all the keys the code raises."""
    exceptions = json.loads((COMPONENT / path).read_text(encoding="utf-8"))[
        "exceptions"
    ]
    assert set(exceptions) >= KEYS
    for key in KEYS:
        assert exceptions[key]["message"].strip()


@pytest.mark.parametrize(
    ("side_effect", "state", "key", "placeholders"),
    [
        pytest.param(
            aioaquarea.AuthenticationError(
                aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD, "x"
            ),
            ConfigEntryState.SETUP_ERROR,
            "invalid_credentials",
            None,
            id="invalid-credentials",
        ),
        pytest.param(
            aioaquarea.AuthenticationError("APP_VERSION_REJECTED", "x"),
            ConfigEntryState.SETUP_RETRY,
            "app_version_rejected",
            None,
            id="app-version",
        ),
        pytest.param(
            aioaquarea.AuthenticationError("MFA_REQUIRED", "x"),
            ConfigEntryState.SETUP_ERROR,
            "mfa_required",
            None,
            id="mfa",
        ),
        pytest.param(
            aioaquarea.AuthenticationError(
                aioaquarea.AuthenticationErrorCodes.API_ERROR, "boom"
            ),
            ConfigEntryState.SETUP_RETRY,
            "authentication_failed",
            {"error": "Authentication error: API_ERROR - boom"},
            id="other-auth",
        ),
        pytest.param(
            aioaquarea.AgreementNotAcceptedError(),
            ConfigEntryState.SETUP_RETRY,
            "agreement_not_accepted",
            None,
            id="agreement",
        ),
        pytest.param(
            aioaquarea.ApiError("HTTP_500", "boom"),
            ConfigEntryState.SETUP_RETRY,
            "cloud_error",
            None,
            id="cloud",
        ),
        pytest.param(
            TimeoutError("slow"),
            ConfigEntryState.SETUP_RETRY,
            "cannot_connect",
            {"error": "slow"},
            id="network",
        ),
    ],
)
async def test_setup_errors_are_translated(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    side_effect: Exception,
    state: ConfigEntryState,
    key: str,
    placeholders: dict[str, str] | None,
) -> None:
    """The entry's error reason is a translation key with its placeholders."""
    mock_aquarea_client.login.side_effect = side_effect
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is state
    assert mock_config_entry.error_reason_translation_domain == DOMAIN
    assert mock_config_entry.error_reason_translation_key == key
    if placeholders is not None:
        assert mock_config_entry.error_reason_translation_placeholders == placeholders


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("side_effect", "key"),
    [
        (
            aioaquarea.AuthenticationError("APP_VERSION_REJECTED", "x"),
            "app_version_rejected",
        ),
        (aioaquarea.AuthenticationError("TOKEN_EXPIRED", "x"), "authentication_failed"),
        (aioaquarea.AgreementNotAcceptedError(), "agreement_not_accepted"),
        (aioaquarea.ApiError("HTTP_500", "boom"), "cloud_error"),
    ],
)
async def test_poll_errors_are_translated(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    side_effect: Exception,
    key: str,
) -> None:
    """A failed poll's UpdateFailed names a translation key."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]

    mock_device.refresh_data.side_effect = side_effect
    # The token-expired path logs in again and retries once; keep it failing.
    mock_aquarea_client.get_device.return_value.refresh_data.side_effect = side_effect
    await _poll(hass, freezer)

    assert not coordinator.last_update_success
    err = coordinator.last_exception
    assert err is not None
    assert err.translation_domain == DOMAIN
    assert err.translation_key == key


async def test_login_backoff_is_translated(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """While backing off, the failure carries the time of the next attempt."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]

    mock_aquarea_client.is_logged = False
    mock_aquarea_client.login.side_effect = aioaquarea.AuthenticationError(
        aioaquarea.AuthenticationErrorCodes.API_ERROR, "403"
    )
    await _poll(hass, freezer)
    await _poll(hass, freezer)

    err = coordinator.last_exception
    assert err is not None
    assert err.translation_key == "login_backoff"
    assert set(err.translation_placeholders) == {"retry_time"}


@pytest.mark.parametrize(
    ("code", "key"),
    [
        (
            aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
            "invalid_credentials",
        ),
        (
            aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
            "invalid_credentials",
        ),
        ("MFA_REQUIRED", "mfa_required"),
    ],
)
async def test_poll_credential_failure_is_translated(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    code: str,
    key: str,
) -> None:
    """The ConfigEntryAuthFailed that starts reauth carries a translation key."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]

    mock_device.refresh_data.side_effect = aioaquarea.AuthenticationError(code, "x")
    await _poll(hass, freezer)

    err = coordinator.last_exception
    assert isinstance(err, ConfigEntryAuthFailed)
    assert err.translation_domain == DOMAIN
    assert err.translation_key == key
