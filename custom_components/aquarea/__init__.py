"""The Aquarea Smart Cloud integration."""
from __future__ import annotations

import logging

import aiohttp
import aioaquarea

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CLIENT, DEVICES, DOMAIN
from .coordinator import AquareaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.WATER_HEATER,
    Platform.SWITCH,
    Platform.SELECT
]


def _create_client(hass: HomeAssistant, entry: ConfigEntry) -> aioaquarea.Client:
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    session = async_create_clientsession(hass)
    return aioaquarea.Client(session, username, password)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options (or data) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Aquarea Smart Cloud from a config entry."""

    client = _create_client(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        CLIENT: client,
        DEVICES: dict[str, AquareaDataUpdateCoordinator](),
    }

    try:
        await client.login()
        # Get all the devices, we will filter the disabled ones later
        devices = await client.get_devices()

        # We create a Coordinator per Device and store it in the hass.data[DOMAIN] dict to be able to access it from the platform
        for device in devices:
            coordinator = AquareaDataUpdateCoordinator(
                hass=hass, entry=entry, client=client, device_info=device
            )
            hass.data[DOMAIN][entry.entry_id][DEVICES][device.device_id] = coordinator
            _LOGGER.debug("Performing first refresh for device %s", device.device_id)
            await coordinator.async_config_entry_first_refresh()

        _LOGGER.debug("Forwarding entry setups for platforms")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Reload on options changes: the coordinator reads consumption_interval
        # only once at construction, so without this a change in the options
        # dialog would have no effect until Home Assistant restarts.
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    except aioaquarea.AuthenticationError as err:
        if err.error_code in (
            aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
            aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
        ):
            raise ConfigEntryAuthFailed(
                "Invalid Aquarea Smart Cloud credentials"
            ) from err
        # Any other authentication failure (SESSION_CLOSED, API_ERROR,
        # TOKEN_EXPIRED, ...) is transient. Never fall through to `return True`
        # here: that would report a successful setup while no devices,
        # coordinators or platforms had been set up.
        raise ConfigEntryNotReady(
            f"Aquarea Smart Cloud authentication failed: {err}"
        ) from err
    except aioaquarea.ClientError as err:
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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
