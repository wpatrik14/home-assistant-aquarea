"""Coordinator for Aquarea."""
from __future__ import annotations

from datetime import timedelta
import logging

import aioaquarea
from aioaquarea.statistics import DateType

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN

DEFAULT_SCAN_INTERVAL_SECONDS = 10
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS)
CONSUMPTION_REFRESH_INTERVAL_MINUTES = 1
CONSUMPTION_REFRESH_INTERVAL = timedelta(minutes=CONSUMPTION_REFRESH_INTERVAL_MINUTES)
_LOGGER = logging.getLogger(__name__)


class AquareaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Aquarea data."""

    _device: aioaquarea.Device

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: aioaquarea.Client,
        device_info: aioaquarea.data.DeviceInfo,
    ) -> None:
        """Initialize a data updater per Device."""

        self._client = client
        self._entry = entry
        self._device_info = device_info
        self._device = None

        # Consumption caching / rate limiting
        # Last hour string in format YYYYMMDDHH for which consumption was fetched
        self._last_consumption_hour: str | None = None
        # Cached consumption results (lists of Consumption objects from aioaquarea.statistics)
        self._day_consumption = None
        self._month_consumption = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{entry.data[CONF_USERNAME]}-{device_info.device_id}",
            update_interval=SCAN_INTERVAL,
        )

    async def async_request_refresh(self, force_fetch: bool = False) -> None:
        """Request a refresh of the data."""
        _LOGGER.debug("async_request_refresh called for device %s", self.device.device_id)
        if force_fetch:
            self._device = None
        await self._async_update_data()
        self.async_update_listeners()

    @property
    def device(self) -> aioaquarea.Device:
        """Return the device."""
        return self._device

    @property
    def device_info(self) -> aioaquarea.data.DeviceInfo:
        """Return the device info."""
        return self._device_info

    @property
    def day_consumption(self):
        """Return the last cached day (hourly) consumption entries or None."""
        return getattr(self, "_day_consumption", None)

    @property
    def month_consumption(self):
        """Return the last cached month consumption entries or None."""
        return getattr(self, "_month_consumption", None)

    async def _async_update_data(self) -> None:
        """Fetch data from Aquarea Smart Cloud Service and hourly consumption when needed."""
        _LOGGER.debug("Fetching data from Aquarea Smart Cloud Service")
        try:
            # Initialize or refresh device state
            if not self._device:
                _LOGGER.debug("Initializing device for the first time")
                self._device = await self._client.get_device(
                    device_info=self._device_info,
                    consumption_refresh_interval=CONSUMPTION_REFRESH_INTERVAL,
                    timezone=dt_util.get_time_zone(self.hass.config.time_zone),
                )
            else:
                _LOGGER.debug("Refreshing device data")
                await self.device.refresh_data()

            # Centralized hourly consumption fetch (once per YYYYMMDDHH)
            try:
                now = dt_util.now()
                current_hour = now.strftime("%Y%m%d%H")
                if self._last_consumption_hour != current_hour:
                    self._last_consumption_hour = current_hour

                    # Day (hourly) consumption for the current date
                    date_str = now.strftime("%Y%m%d")
                    _LOGGER.debug(
                        "Coordinator fetching day (hourly) consumption for date %s", date_str
                    )
                    try:
                        self._day_consumption = await self._client.get_device_consumption(
                            self._device.long_id, DateType.DAY, date_str
                        )
                    except Exception as exc:
                        _LOGGER.warning(
                            "Failed to fetch day consumption for device %s: %s",
                            getattr(self._device, "long_id", "<unknown>"),
                            exc,
                        )
                        self._day_consumption = None

                    # Month (monthly entries) - fetch month-to-date data
                    month_date_str = now.strftime("%Y%m01")
                    _LOGGER.debug(
                        "Coordinator fetching month consumption for %s", month_date_str
                    )
                    try:
                        self._month_consumption = await self._client.get_device_consumption(
                            self._device.long_id, DateType.MONTH, month_date_str
                        )
                    except Exception as exc:
                        _LOGGER.warning(
                            "Failed to fetch month consumption for device %s: %s",
                            getattr(self._device, "long_id", "<unknown>"),
                            exc,
                        )
                        self._month_consumption = None
            except Exception:
                _LOGGER.exception("Unexpected error while fetching consumption data")

            _LOGGER.debug("Data fetching complete")
        except aioaquarea.AuthenticationError as err:
            if err.error_code in (
                aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
                aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
            ):
                raise ConfigEntryAuthFailed from err
        except aioaquarea.errors.RequestFailedError as err:
            raise UpdateFailed(
                f"Error communicating with Aquarea Smart Cloud API: {err}"
            ) from err
