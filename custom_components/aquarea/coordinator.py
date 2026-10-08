"""Coordinator for Aquarea."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
    CONF_CONSUMPTION_INTERVAL,
    DEFAULT_CONSUMPTION_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MFA_REQUIRED,
    YESTERDAY_REFETCH_HOURS,
)
from .statistics import CLOUD_TIME_ZONE

_LOGGER = logging.getLogger(__name__)


def _cloud_date(moment: datetime) -> date:
    """Return the cloud's date (in CLOUD_TIME_ZONE) at a moment."""
    return moment.astimezone(CLOUD_TIME_ZONE).date()


# The entry's runtime data: one coordinator per device, keyed by device ID.
type AquareaConfigEntry = ConfigEntry[dict[str, AquareaDataUpdateCoordinator]]


class AquareaDataUpdateCoordinator(DataUpdateCoordinator[aioaquarea.Device]):
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
        # Cached consumption results (lists of Consumption objects from aioaquarea.statistics)
        self._month_consumption = None
        self._last_monthly_fetch_time: datetime | None = None
        # Hourly consumption (DAY queries) of the last three days, by the
        # cloud's date (UTC, see statistics.CLOUD_TIME_ZONE), with when each day was last asked for and last received. The
        # hourly fetch has its own cadence, independent of the monthly one.
        self._day_consumption: dict[date, list[aioaquarea.Consumption]] = {}
        self._day_requested_at: dict[date, datetime] = {}
        self._day_received_at: dict[date, datetime] = {}
        # Days whose query came back empty, so the warning is logged once.
        self._day_missing: set[date] = set()
        self._hourly_consumption_fetched_at: datetime | None = None

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
            config_entry=entry,
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
    def entry(self) -> ConfigEntry:
        """Return the config entry this coordinator belongs to."""
        return self._entry

    @property
    def device_info(self) -> aioaquarea.data.DeviceInfo:
        """Return the device info."""
        return self._device_info

    @property
    def month_consumption(self):
        """Return the last cached month consumption entries or None."""
        return getattr(self, "_month_consumption", None)

    @property
    def hourly_consumption_first_day(self) -> date | None:
        """Return the first day with cached hourly consumption, or None."""
        return min(self._day_consumption, default=None)

    @property
    def hourly_consumption(self) -> list[aioaquarea.Consumption]:
        """Return the cached hourly consumption records, oldest first."""
        return [
            record
            for day in sorted(self._day_consumption)
            for record in self._day_consumption[day]
        ]

    @property
    def hourly_consumption_fetched_at(self) -> datetime | None:
        """Return when the hourly consumption was last fetched, or None."""
        return self._hourly_consumption_fetched_at

    def _hourly_days_due(self, now: datetime) -> list[date]:
        """Return the days whose hourly consumption is due for a fetch, oldest first.

        The days are the cloud's (UTC, see statistics.CLOUD_TIME_ZONE). Today
        is fetched every consumption interval. The cloud publishes
        yesterday's last hours after it ends, so yesterday is fetched again,
        at most hourly, until it has been received at least
        YESTERDAY_REFETCH_HOURS into today; after a restart that is the
        first fetch. The day before yesterday is fetched once when missing,
        after a restart: it is the context that lets yesterday be rewritten.
        The intervals run from the last request, so a failing query is
        retried at the same pace rather than on every poll.
        """
        today = _cloud_date(now)
        yesterday = today - timedelta(days=1)
        interval = timedelta(minutes=self.consumption_interval)
        complete_from = datetime.combine(today, time(), CLOUD_TIME_ZONE) + timedelta(
            hours=YESTERDAY_REFETCH_HOURS
        )
        days: list[date] = []
        before_yesterday = today - timedelta(days=2)
        requested = self._day_requested_at.get(before_yesterday)
        if before_yesterday not in self._day_received_at and (
            requested is None or now - requested >= interval
        ):
            days.append(before_yesterday)
        received = self._day_received_at.get(yesterday)
        requested = self._day_requested_at.get(yesterday)
        if (received is None or received < complete_from) and (
            requested is None or now - requested >= min(interval, timedelta(hours=1))
        ):
            days.append(yesterday)
        requested = self._day_requested_at.get(today)
        if requested is None or now - requested >= interval:
            days.append(today)
        return days

    async def _async_fetch_hourly_consumption(self, now: datetime) -> None:
        """Fetch the hourly consumption of the days that are due.

        The energy sensors use it to file their hourly statistics under the
        hours the cloud labels the energy with (see statistics.py).
        """
        days = self._hourly_days_due(now)
        if not days:
            return
        fetched = False
        for day in days:
            self._day_requested_at[day] = now
            try:
                records = await self._client.get_device_consumption(
                    self._device.long_id, DateType.DAY, day.strftime("%Y%m%d")
                )
            except aioaquarea.AuthenticationError as ex:
                # As for the month: aioaquarea-ng logs in again on an expired
                # token itself; credential failures go on to start reauth.
                if ex.error_code in (
                    aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
                    aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
                    MFA_REQUIRED,
                ):
                    raise
                _LOGGER.warning(
                    "Failed to fetch the hourly consumption of %s: %s", day, ex
                )
                continue
            except Exception as ex:  # noqa: BLE001 - deliberate: warn and keep the cached hourly data
                _LOGGER.warning(
                    "Failed to fetch the hourly consumption of %s: %s", day, ex
                )
                continue
            if records is None:
                # aioaquarea returns None on HTTP and network errors, and
                # when the cloud has no data: a failure, not an empty day.
                # Storing it would wipe the cached hours and revert the
                # corrections built on them. Warn once per day, not at every
                # interval of an outage.
                log = _LOGGER.debug if day in self._day_missing else _LOGGER.warning
                log(
                    "No hourly consumption received for %s; keeping the cached hours",
                    day,
                )
                self._day_missing.add(day)
                continue
            self._day_missing.discard(day)
            self._day_consumption[day] = records
            self._day_received_at[day] = now
            fetched = True
        # Keep the day before yesterday too: it is context for yesterday's
        # first hours, whose energy can be recorded after midnight.
        oldest = _cloud_date(now) - timedelta(days=2)
        for cache in (
            self._day_consumption,
            self._day_requested_at,
            self._day_received_at,
        ):
            for day in [day for day in cache if day < oldest]:
                del cache[day]
        self._day_missing = {day for day in self._day_missing if day >= oldest}
        if fetched:
            self._hourly_consumption_fetched_at = now

    async def _async_update_data(self) -> aioaquarea.Device:
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
                consumption_refresh_interval=timedelta(
                    minutes=15
                ),  # Not used by library for fetching, but kept for compatibility
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

            # 2. Fetch monthly consumption (used by both today and month-to-date sensors)
            # Also refetch as soon as the local date changes: the cache holds
            # the list fetched for the previous day (or, after a month boundary,
            # the previous month's YYYYMM01 list), so waiting out the interval
            # would leave the "today" sensors on yesterday's value for up to
            # consumption_interval minutes after midnight.
            last_fetch = self._last_monthly_fetch_time
            fetch_monthly = (
                last_fetch is None
                or now.date() != last_fetch.date()
                or now - last_fetch >= timedelta(minutes=self.consumption_interval)
            )

            if fetch_monthly:
                _LOGGER.debug(
                    "Fetching monthly consumption data from Cloud API (%sm interval)",
                    self.consumption_interval,
                )
                month_date_str = now.strftime("%Y%m01")
                try:
                    self._month_consumption = await self._client.get_device_consumption(
                        self._device.long_id, DateType.MONTH, month_date_str
                    )
                    self._last_monthly_fetch_time = now
                except aioaquarea.AuthenticationError as ex:
                    # Since aioaquarea-ng 1.2.0 auth errors are raised by consumption
                    # calls (they used to return None). Credential failures must not
                    # be hidden behind the cache: let the handler below start reauth.
                    if ex.error_code in (
                        aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
                        aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
                        MFA_REQUIRED,
                    ):
                        raise
                    _LOGGER.warning("Failed to fetch month consumption: %s", ex)
                except Exception as ex:  # noqa: BLE001 - deliberate: warn and keep the cached month data
                    _LOGGER.warning("Failed to fetch month consumption: %s", ex)

            # 3. Fetch hourly consumption, on its own cadence
            await self._async_fetch_hourly_consumption(now)

            return self._device  # noqa: TRY300 - the handlers below cover the whole fetch
        except aioaquarea.AuthenticationError as err:
            if err.error_code in (
                aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
                aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
                MFA_REQUIRED,
            ):
                raise ConfigEntryAuthFailed from err
            raise UpdateFailed(f"Authentication error: {err}") from err
        except aioaquarea.ClientError as err:
            # Covers RequestFailedError, non-auth ApiError and InvalidData,
            # which share only this base class. Anything else would reach
            # Home Assistant's generic handler and log a traceback every poll.
            raise UpdateFailed(
                f"Error communicating with Aquarea Smart Cloud API: {err}"
            ) from err
