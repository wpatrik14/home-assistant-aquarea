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
    DEFAULT_DAILY_CONSUMPTION_INTERVAL,
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
        
        self._last_daily_fetch_time: datetime | None = None
        self._last_monthly_fetch_time: datetime | None = None

        # Main device and zones are fixed at 1 minute
        scan_interval = DEFAULT_SCAN_INTERVAL
        
        # Monthly consumption is configurable
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
        """Fetch data from Aquarea Smart Cloud Service with tiered intervals."""
        try:
            # Ensure we are logged in and token is valid
            if not self._client.is_logged:
                _LOGGER.debug("Client not logged in or token expired, logging in")
                await self._client.login()

            now = dt_util.now()

            # 1. Fetch Main Device & Zone Details (Every 1 minute - the coordinator's tick)
            # We always re-fetch the device to ensure all internal objects (like zones) are correctly updated
            _LOGGER.debug("Fetching device and zones data from Cloud API (1m interval)")
            self._device = await self._client.get_device(
                device_info=self._device_info,
                consumption_refresh_interval=timedelta(minutes=15), # Not used by library for fetching, but kept for compatibility
                timezone=dt_util.get_time_zone(self.hass.config.time_zone),
            )
            
            try:
                await self._device.refresh_data()
            except aioaquarea.AuthenticationError:
                _LOGGER.debug("Token expired during refresh, logging in again")
                await self._client.login()
                self._device = await self._client.get_device(
                    device_info=self._device_info,
                    consumption_refresh_interval=timedelta(minutes=15),
                    timezone=dt_util.get_time_zone(self.hass.config.time_zone),
                )
                await self._device.refresh_data()

            # 2. Determine if we need to fetch consumption data
            fetch_daily = (self._last_daily_fetch_time is None) or (
                now - self._last_daily_fetch_time >= timedelta(minutes=DEFAULT_DAILY_CONSUMPTION_INTERVAL)
            )
            fetch_monthly = (self._last_monthly_fetch_time is None) or (
                now - self._last_monthly_fetch_time >= timedelta(minutes=self.consumption_interval)
            )

            if fetch_daily or fetch_monthly:
                tasks = []
                if fetch_daily:
                    _LOGGER.debug("Fetching daily consumption data from Cloud API (15m interval)")
                    previous_hour = now - timedelta(hours=1)
                    date_str = previous_hour.strftime("%Y%m%d")
                    tasks.append(self._client.get_device_consumption(self._device.long_id, DateType.DAY, date_str))
                else:
                    tasks.append(asyncio.sleep(0, result=self._day_consumption))

                if fetch_monthly:
                    _LOGGER.debug("Fetching monthly consumption data from Cloud API (%sm interval)", self.consumption_interval)
                    month_date_str = now.strftime("%Y%m01")
                    tasks.append(self._client.get_device_consumption(self._device.long_id, DateType.MONTH, month_date_str))
                else:
                    tasks.append(asyncio.sleep(0, result=self._month_consumption))

                results = await asyncio.gather(*tasks, return_exceptions=True)

                if fetch_daily:
                    if isinstance(results[0], Exception):
                        _LOGGER.warning("Failed to fetch day consumption: %s", results[0])
                    else:
                        self._day_consumption = results[0]
                        self._last_daily_fetch_time = now
                
                if fetch_monthly:
                    if isinstance(results[1], Exception):
                        _LOGGER.warning("Failed to fetch month consumption: %s", results[1])
                    else:
                        self._month_consumption = results[1]
                        self._last_monthly_fetch_time = now

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
