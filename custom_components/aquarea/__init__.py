"""The Aquarea Smart Cloud integration."""

from __future__ import annotations

import logging

import aioaquarea
import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import APP_VERSION_REJECTED, CONF_REFRESH_TOKEN, MFA_REQUIRED
from .coordinator import (
    AGREEMENT_NOT_ACCEPTED_CODE,
    AGREEMENT_NOT_ACCEPTED_MESSAGE,
    AquareaConfigEntry,
    AquareaDataUpdateCoordinator,
)
from .issues import (
    APP_VERSION_REJECTED_MESSAGE,
    async_clear_app_version_issue,
    async_create_app_version_issue,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.WATER_HEATER,
    Platform.SWITCH,
    Platform.SELECT,
]


def _create_client(hass: HomeAssistant, entry: AquareaConfigEntry) -> aioaquarea.Client:
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    session = async_create_clientsession(hass)

    @callback
    def _store_refresh_token(token: str) -> None:
        """Keep the entry's refresh token current (it can rotate on every refresh)."""
        if entry.data.get(CONF_REFRESH_TOKEN) != token:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

    # Without a stored token the password login is used. Setup and polling run
    # unattended: a login that needs multi-factor authentication must not text
    # a code nobody is waiting for; it starts reauth, which asks for the code.
    return aioaquarea.Client(
        session,
        username,
        password,
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        mfa_send_code=False,
        refresh_token_callback=_store_refresh_token,
    )


async def async_setup_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> bool:
    """Set up Aquarea Smart Cloud from a config entry."""

    client = _create_client(hass, entry)
    coordinators: dict[str, AquareaDataUpdateCoordinator] = {}

    try:
        await client.login()
        async_clear_app_version_issue(hass, entry)
        # Get all the devices, we will filter the disabled ones later
        devices = await client.get_devices()

        # One coordinator per device; the platforms read them from the
        # entry's runtime_data.
        for device in devices:
            coordinator = AquareaDataUpdateCoordinator(
                hass=hass, entry=entry, client=client, device_info=device
            )
            coordinators[device.device_id] = coordinator
            _LOGGER.debug("Performing first refresh for device %s", device.device_id)
            await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = coordinators
        _LOGGER.debug("Forwarding entry setups for platforms")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except aioaquarea.AuthenticationError as err:
        if err.error_code in (
            aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
            aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
        ):
            raise ConfigEntryAuthFailed(
                "Invalid Aquarea Smart Cloud credentials"
            ) from err
        if err.error_code == APP_VERSION_REJECTED:
            # Panasonic stopped accepting the app version: the credentials are
            # fine, so no reauth (it would ask for a password that is not the
            # problem). Show a repair issue, and retry: an update of the
            # library or Panasonic accepting the version again fixes it.
            async_create_app_version_issue(hass, entry)
            raise ConfigEntryNotReady(APP_VERSION_REJECTED_MESSAGE) from err
        if err.error_code == MFA_REQUIRED:
            # Retrying cannot pass the challenge, and every attempt sends the
            # user a Panasonic login email. Reauth asks for the code.
            raise ConfigEntryAuthFailed(
                "The Panasonic ID requires multi-factor authentication"
            ) from err
        # Any other authentication failure (SESSION_CLOSED, API_ERROR,
        # TOKEN_EXPIRED, ...) is transient. Never fall through to `return True`
        # here: that would report a successful setup while no devices,
        # coordinators or platforms had been set up.
        raise ConfigEntryNotReady(
            f"Aquarea Smart Cloud authentication failed: {err}"
        ) from err
    except aioaquarea.ClientError as err:
        if getattr(err, "error_code", None) == AGREEMENT_NOT_ACCEPTED_CODE:
            raise ConfigEntryNotReady(AGREEMENT_NOT_ACCEPTED_MESSAGE) from err
        # ApiError, RequestFailedError, InvalidData - the cloud is reachable
        # but unhappy. Worth retrying.
        raise ConfigEntryNotReady(f"Aquarea Smart Cloud error: {err}") from err
    except (aiohttp.ClientError, TimeoutError) as err:
        # DNS failures, connection resets, timeouts. Without this, a transient
        # network blip leaves the entry in `setup_error` forever, because Home
        # Assistant only retries setup when ConfigEntryNotReady is raised.
        raise ConfigEntryNotReady(
            f"Unable to reach Aquarea Smart Cloud: {err}"
        ) from err

    return True


async def async_remove_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> None:
    """Drop the repair issue of a removed entry."""
    async_clear_app_version_issue(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
