from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from .const import (
    AQUAREA_SERVICE_A2W_STATUS_DISPLAY,
    AQUAREA_SERVICE_DEVICES,
)
from .data import (
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    OperationStatus,
    PowerfulTime,
    QuietMode,
    UpdateOperationMode,
    ZoneTemperatureSetUpdate,
    SpecialStatus,
)
from .auth import PanasonicRequestHeader

if TYPE_CHECKING:
    from .api_client import AquareaAPIClient


class AquareaDeviceControl:
    """Handles device control operations."""

    def __init__(self, api_client: AquareaAPIClient, base_url: str):
        self._api_client = api_client
        self._base_url = base_url

    async def post_device_operation_status(
        self, long_device_id: str, new_operation_status: OperationStatus
    ) -> None:
        """Post device operation status."""
        data = {
            "status": [
                {
                    "deviceGuid": long_device_id,
                    "operationStatus": new_operation_status.value,
                }
            ]
        }

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_device_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_tank_temperature(
        self, long_device_id: str, new_temperature: int
    ) -> None:
        """Post device tank temperature."""
        data = {
            "status": [
                {
                    "deviceGuid": long_device_id,
                    "tankStatus": [
                        {
                            "heatSet": new_temperature,
                        }
                    ],
                }
            ]
        }

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_device_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_tank_operation_status(
        self,
        long_device_id: str,
        new_operation_status: OperationStatus,
        new_device_operation_status: OperationStatus = OperationStatus.ON, # This parameter might become obsolete or need re-evaluation based on the new structure
    ) -> None:
        """Post device tank operation status."""
        data = {
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {
                "gwid": long_device_id,
                "zoneStatus": [
                    {
                        "zoneId": 1,
                        "operationStatus": OperationStatus.OFF.value # Assuming default off for zones
                    },
                    {
                        "zoneId": 2,
                        "operationStatus": OperationStatus.OFF.value # Assuming default off for zones
                    }
                ],
                "tankStatus": {
                    "operationStatus": new_operation_status.value
                }
            }
        }

        await self._api_client.request(
            "POST",
            AQUAREA_SERVICE_DEVICES, # The path is now part of apiName in the body
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_operation_update(
        self,
        long_id: str,
        mode: UpdateOperationMode,
        zones: dict[int, OperationStatus],
        operation_status: OperationStatus,
    ) -> None:
        """Post device operation update."""
        data = {
            "status": [
                {
                    "deviceGuid": long_id,
                    "operationMode": mode.value,
                    "operationStatus": operation_status.value,
                    "zoneStatus": [
                        {
                            "zoneId": zone_id,
                            "operationStatus": zones[zone_id].value,
                        }
                        for zone_id in zones
                    ],
                }
            ]
        }

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_set_special_status(
        self,
        long_id: str,
        special_status: SpecialStatus | None,
        zones: list[ZoneTemperatureSetUpdate],
    ) -> None:
        """Post device operation update."""
        data = {
            "status": [
                {
                    "deviceGuid": long_id,
                    "specialStatus": special_status.value if special_status else 0,
                    "zoneStatus": [
                        {
                            "zoneId": zone.zone_id,
                            "heatSet": zone.heat_set,
                            **(
                                {"coolSet": zone.cool_set}
                                if zone.cool_set is not None
                                else {}
                            ),
                        }
                        for zone in zones
                    ],
                }
            ]
        }

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_zone_heat_temperature(
        self, long_id: str, zone_id: int, temperature: int
    ) -> None:
        """Post device zone heat temperature."""
        return await self._post_device_zone_temperature(
            long_id, zone_id, temperature, "heatSet"
        )

    async def post_device_zone_cool_temperature(
        self, long_id: str, zone_id: int, temperature: int
    ) -> None:
        """Post device zone cool temperature."""
        return await self._post_device_zone_temperature(
            long_id, zone_id, temperature, "coolSet"
        )

    async def _post_device_zone_temperature(
        self, long_id: str, zone_id: int, temperature: int, key: str
    ) -> None:
        """Post device zone temperature."""
        data = {
            "apiName": "/remote/v1/api/devices",
            "requestMethod": "POST",
            "bodyParam": {
                "gwid": long_id,
                "operationMode": 3,
                "zoneStatus": [
                    {
                        "zoneId": zone_id,
                        "coolSet": temperature,
                    }
                ]
            }
        }

        response = await self._api_client.request(
            "POST",
            "/remote/v1/app/common/transfer",
            headers={},
            json=data,
        )

    async def post_device_set_quiet_mode(self, long_id: str, mode: QuietMode) -> None:
        """Post quiet mode."""
        data = {"status": [{"deviceGuid": long_id, "quietMode": mode.value}]}

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_force_dhw(self, long_id: str, force_dhw: ForceDHW) -> None:
        """Post quiet mode."""
        data = {"status": [{"deviceGuid": long_id, "forceDHW": force_dhw.value}]}

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_force_heater(
        self, long_id: str, force_heater: ForceHeater
    ) -> None:
        """Post quiet mode."""
        data = {"status": [{"deviceGuid": long_id, "forceHeater": force_heater.value}]}

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_holiday_timer(
        self, long_id: str, holiday_timer: HolidayTimer
    ) -> None:
        """Post quiet mode."""
        data = {
            "status": [{"deviceGuid": long_id, "holidayTimer": holiday_timer.value}]
        }

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_request_defrost(self, long_id: str) -> None:
        """Post quiet mode."""
        data = {"status": [{"deviceGuid": long_id, "forcedefrost": 1}]}

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )

    async def post_device_set_powerful_time(
        self, long_id: str, powerful_time: PowerfulTime
    ) -> None:
        """Post powerful time."""
        data = {
            "status": [
                {
                    "deviceGuid": long_id,
                    "powerfulRequest": powerful_time.value,
                }
            ]
        }

        await self._api_client.request(
            "POST",
            f"{AQUAREA_SERVICE_DEVICES}/{long_id}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                content_type="application/json",
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
            json=data,
        )
