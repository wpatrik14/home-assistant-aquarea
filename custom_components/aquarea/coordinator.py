"""Coordinator for Aquarea."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

import aioaquarea
from aioaquarea.statistics import DateType

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_SCAN_INTERVAL,
    CONF_CONSUMPTION_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_CONSUMPTION_INTERVAL,
)

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
        self._last_consumption_fetch_time: datetime | None = None

        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        self.consumption_interval = entry.options.get(
            CONF_CONSUMPTION_INTERVAL,
            entry.data.get(CONF_CONSUMPTION_INTERVAL, DEFAULT_CONSUMPTION_INTERVAL),
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{entry.data[CONF_USERNAME]}-{device_info.device_id}",
            update_interval=timedelta(seconds=scan_interval),
        )

    async def async_request_refresh(self, force_fetch: bool = False) -> None:
        """Request a refresh of the data."""
        _LOGGER.debug("async_request_refresh called for device %s", self._device_info.device_id)
        if force_fetch:
            self._device = None
        await super().async_request_refresh()

    @property
    def device(self) -> aioaquarea.Device:
        """Return the device."""
        return self.data if self.data is not None else self._device

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
            # Ensure we are logged in and token is valid
            if not self._client.is_logged:
                _LOGGER.debug("Client not logged in or token expired, logging in")
                await self._client.login()

            # Initialize or refresh device state
            # We always re-fetch the device to ensure all internal objects (like zones) are correctly updated
            # as the library's refresh_data method may not update zone status references.
            _LOGGER.debug("Fetching device data")
            self._device = await self._client.get_device(
                device_info=self._device_info,
                consumption_refresh_interval=timedelta(
                    minutes=self.consumption_interval
                ),
                timezone=dt_util.get_time_zone(self.hass.config.time_zone),
            )

            # Refresh zones data immediately to ensure they are properly initialized
            _LOGGER.debug("Refreshing zones data for device %s", self._device_info.device_id)
            try:
                await self._device.refresh_data()
            except aioaquarea.AuthenticationError:
                _LOGGER.debug("Token expired during refresh, logging in again")
                await self._client.login()
                # Re-fetch device to ensure we have a fresh state with the new token
                self._device = await self._client.get_device(
                    device_info=self._device_info,
                    consumption_refresh_interval=timedelta(
                        minutes=self.consumption_interval
                    ),
                    timezone=dt_util.get_time_zone(self.hass.config.time_zone),
                )
                await self._device.refresh_data()

            # Centralized hourly consumption fetch (once per hour at :00)
            now = dt_util.now()
            if (self._last_consumption_fetch_time is None) or (
                now - self._last_consumption_fetch_time
                >= timedelta(minutes=self.consumption_interval)
            ):
                self._last_consumption_fetch_time = now
                self._last_consumption_hour = now.strftime("%Y%m%d%H")
                
                # Parallelize consumption fetching to avoid blocking
                previous_hour = now - timedelta(hours=1)
                date_str = previous_hour.strftime("%Y%m%d")
                month_date_str = now.strftime("%Y%m01")
                
                _LOGGER.debug("Coordinator fetching consumption data in parallel")
                results = await asyncio.gather(
                    self._client.get_device_consumption(self._device.long_id, DateType.DAY, date_str),
                    self._client.get_device_consumption(self._device.long_id, DateType.MONTH, month_date_str),
                    return_exceptions=True
                )
                
                # Handle day consumption result
                if isinstance(results[0], Exception):
                    _LOGGER.warning("Failed to fetch day consumption for device %s: %s", self._device.long_id, results[0])
                    self._day_consumption = None
                else:
                    self._day_consumption = results[0]
                    if self._day_consumption:
                        _LOGGER.debug("Hourly consumption data for past 24 hours fetched")

                # Handle month consumption result
                if isinstance(results[1], Exception):
                    _LOGGER.warning("Failed to fetch month consumption for device %s: %s", self._device.long_id, results[1])
                    self._month_consumption = None
                else:
                    self._month_consumption = results[1]
                    if self._month_consumption:
                        _LOGGER.debug("Month consumption data fetched")

            _LOGGER.debug("Data fetching complete")
            return self._device
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
