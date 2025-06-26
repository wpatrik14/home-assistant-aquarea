"""Aquarea Client for asyncio."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

import aiohttp

from aioaquarea.statistics import Consumption, DateType

from .const import (
    AQUAREA_SERVICE_BASE,
    AQUAREA_SERVICE_DEMO_BASE,
    AquareaEnvironment,
)
from .data import (
    Device,
    DeviceInfo,
    DeviceStatus,
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    OperationStatus,
    PowerfulTime,
    QuietMode,
    SpecialStatus,
    UpdateOperationMode,
    ZoneTemperatureSetUpdate,
)
from .auth import Authenticator, PanasonicSettings, CCAppVersion
from .decorators import auth_required
from .entities import DeviceImpl
from .device_manager import DeviceManager
from .api_client import AquareaAPIClient
from .device_control import AquareaDeviceControl
from .consumption_manager import AquareaConsumptionManager

_LOGGER = logging.getLogger(__name__)

class AquareaClient: # Renamed Client to AquareaClient
    """Aquarea Client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str | None = None,
        password: str | None = None,
        refresh_login: bool = True,
        logger: Optional[logging.Logger] = None,
        environment: AquareaEnvironment = AquareaEnvironment.PRODUCTION,
        device_direct: bool = True,
    ):
        """
        Initializes a new instance of the `AquareaClient` class.

        Args:
            session (aiohttp.ClientSession): The aiohttp client session.
            username (str, optional): The username for authentication. Defaults to None.
            password (str, optional): The password for authentication. Defaults to None.
            refresh_login (bool, optional): Whether to refresh the login. Defaults to True.
            logger (Optional[logging.Logger], optional): The logger instance. Defaults to None.
            environment (AquareaEnvironment, optional): The environment to use. Defaults to AquareaEnvironment.PRODUCTION.
            device_direct (bool, optional): Whether to use device direct mode. Defaults to True.

        Raises:
            ValueError: If the environment is set to PRODUCTION and username or password are not provided.
        """
        if environment == AquareaEnvironment.PRODUCTION and (
            not username or not password
        ):
            raise ValueError("Username and password must be provided")

        self._login_lock = asyncio.Lock()
        self._sess = session
        self._username = username
        self._password = password
        self._refresh_login = refresh_login
        self._logger = logger or logging.getLogger("aioaquarea")
        self._last_login: dt.datetime = dt.datetime.min
        self._environment = environment
        self._base_url = (
            AQUAREA_SERVICE_BASE
            if environment == AquareaEnvironment.PRODUCTION
            else AQUAREA_SERVICE_DEMO_BASE
        )
        self._device_direct = (
            device_direct if environment == AquareaEnvironment.PRODUCTION else False
        )
        self._settings = PanasonicSettings()
        self._app_version = CCAppVersion()
        self._authenticator = Authenticator(self._sess, self._settings, self._app_version, self._environment, self._logger)
        self._device_manager = DeviceManager(self, self._settings, self._app_version, self._logger)
        self._api_client = AquareaAPIClient(self._sess, self._settings, self._app_version, self._environment, self._logger)
        self._device_control = AquareaDeviceControl(self._api_client, self._base_url)
        self._consumption_manager = AquareaConsumptionManager(self._api_client, self._base_url)
        self._settings.username = username
        self._settings.password = password
        self._settings.access_token = self._api_client.access_token
        self._settings.refresh_token = None
        self._settings.expires_at = None
        self._settings.scope = None
        self._settings.clientId = None

    @property
    def username(self) -> str:
        """The username used to login."""
        return self._username

    @property
    def password(self) -> str:
        """Return the password."""
        return self._password

    @property
    def is_refresh_login_enabled(self) -> bool:
        """Return True if the client is allowed to refresh the login."""
        return self._refresh_login

    @property
    def token_expiration(self) -> Optional[dt.datetime]:
        """Return the expiration date of the token."""
        return self._api_client.token_expiration

    @property
    def is_logged(self) -> bool:
        """Return True if the user is logged in."""
        if not self._api_client.access_token:
            return False

        # We don't have an expiration time, so we assume the token is valid
        if not self._api_client.token_expiration:
            return True
        
        now = dt.datetime.now(tz=dt.timezone.utc)
        return now < self._api_client.token_expiration

    @property
    def logger(self) -> logging.Logger:
        """Return the logger."""
        return self._logger

    async def login(self) -> None:
        """Login to Aquarea and stores a token in the session."""
        intent = dt.datetime.now()
        await self._login_lock.acquire()
        try:
            if self._last_login > intent:
                return

            if self._environment is AquareaEnvironment.DEMO:
                # In a real scenario, this would be handled by the Authenticator
                _ = await self._api_client.request("GET", "", referer=self._base_url)
                self._api_client.token_expiration = dt.datetime.astimezone(
                    dt.datetime.utcnow(), tz=dt.timezone.utc
                ) + dt.timedelta(days=1)
            else:
                await self._authenticator.authenticate(self._username, self._password)
                
            self._last_login = dt.datetime.now()
            self._api_client.access_token = self._settings.access_token
            self._api_client.token_expiration = dt.datetime.fromtimestamp(self._settings.expires_at, tz=dt.timezone.utc)
            # Removed await self._device_manager.get_groups() as it's not a public method and device fetching handles it.
        finally:
            self._login_lock.release()

    @auth_required
    async def get_devices(self) -> list[DeviceInfo]:
        """Get list of devices and its configuration, without status."""
        return await self._device_manager.get_devices()

    @auth_required
    async def get_device_status(self, device_info: DeviceInfo) -> DeviceStatus:
        """Retrives device status."""
        return await self._device_manager.get_device_status(device_info)

    @auth_required
    async def get_device(
        self,
        device_info: DeviceInfo | None = None,
        device_id: str | None = None,
        consumption_refresh_interval: Optional[dt.timedelta] = None,
        timezone: dt.timezone = dt.timezone.utc,
    ) -> Device:
        """Retrieve device."""
        if not device_info and not device_id:
            raise ValueError("Either device_info or device_id must be provided")

        if not device_info:
            devices = await self.get_devices()
            device_info = next(
                filter(lambda d: d.device_id == device_id, devices), None
            )

        return DeviceImpl(
            device_info,
            await self.get_device_status(device_info),
            self,
            consumption_refresh_interval,
            timezone,
        )

    @auth_required
    async def post_device_operation_status(
        self, long_device_id: str, new_operation_status: OperationStatus
    ) -> None:
        """Post device operation status."""
        return await self._device_control.post_device_operation_status(long_device_id, new_operation_status)

    @auth_required
    async def post_device_tank_temperature(
        self, long_device_id: str, new_temperature: int
    ) -> None:
        """Post device tank temperature."""
        return await self._device_control.post_device_tank_temperature(long_device_id, new_temperature)

    @auth_required
    async def post_device_tank_operation_status(
        self,
        long_device_id: str,
        new_operation_status: OperationStatus,
        new_device_operation_status: OperationStatus = OperationStatus.ON,
    ) -> None:
        """Post device tank operation status."""
        return await self._device_control.post_device_tank_operation_status(long_device_id, new_operation_status, new_device_operation_status)

    @auth_required
    async def post_device_operation_update(
        self,
        long_id: str,
        mode: UpdateOperationMode,
        zones: dict[int, OperationStatus],
        operation_status: OperationStatus,
    ) -> None:
        """Post device operation update."""
        return await self._device_control.post_device_operation_update(long_id, mode, zones, operation_status)

    @auth_required
    async def post_device_set_special_status(
        self,
        long_id: str,
        special_status: SpecialStatus | None,
        zones: list[ZoneTemperatureSetUpdate],
    ) -> None:
        """Post device operation update."""
        return await self._device_control.post_device_set_special_status(long_id, special_status, zones)

    async def post_device_zone_heat_temperature(
        self, long_id: str, zone_id: int, temperature: int
    ) -> None:
        """Post device zone heat temperature."""
        return await self._device_control.post_device_zone_heat_temperature(long_id, zone_id, temperature)

    async def post_device_zone_cool_temperature(
        self, long_id: str, zone_id: int, temperature: int
    ) -> None:
        """Post device zone cool temperature."""
        return await self._device_control.post_device_zone_cool_temperature(long_id, zone_id, temperature)

    @auth_required
    async def _post_device_zone_temperature(
        self, long_id: str, zone_id: int, temperature: int, key: str
    ) -> None:
        """Post device zone temperature."""
        return await self._device_control._post_device_zone_temperature(long_id, zone_id, temperature, key)

    @auth_required
    async def post_device_set_quiet_mode(self, long_id: str, mode: QuietMode) -> None:
        """Post quiet mode."""
        return await self._device_control.post_device_set_quiet_mode(long_id, mode)

    @auth_required
    async def post_device_force_dhw(self, long_id: str, force_dhw: ForceDHW) -> None:
        """Post quiet mode."""
        return await self._device_control.post_device_force_dhw(long_id, force_dhw)

    @auth_required
    async def post_device_force_heater(
        self, long_id: str, force_heater: ForceHeater
    ) -> None:
        """Post quiet mode."""
        return await self._device_control.post_device_force_heater(long_id, force_heater)

    @auth_required
    async def post_device_holiday_timer(
        self, long_id: str, holiday_timer: HolidayTimer
    ) -> None:
        """Post quiet mode."""
        return await self._device_control.post_device_holiday_timer(long_id, holiday_timer)

    @auth_required
    async def post_device_request_defrost(self, long_id: str) -> None:
        """Post quiet mode."""
        return await self._device_control.post_device_request_defrost(long_id)

    @auth_required
    async def post_device_set_powerful_time(
        self, long_id: str, powerful_time: PowerfulTime
    ) -> None:
        """Post powerful time."""
        return await self._device_control.post_device_set_powerful_time(long_id, powerful_time)

    async def get_device_consumption(
        self, long_id: str, aggregation: DateType, date_input: str
    ) -> Consumption:
        """Get device consumption."""
        return await self._consumption_manager.get_device_consumption(long_id, aggregation, date_input)
