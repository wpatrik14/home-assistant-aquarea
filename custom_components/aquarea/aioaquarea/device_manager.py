import logging
from typing import Optional, TYPE_CHECKING

import aiohttp

from .data import (
    DeviceInfo,
    DeviceModeStatus,
    DeviceStatus,
    DeviceZoneInfo,
    DeviceZoneStatus,
    ForceDHW,
    ForceHeater,
    HolidayTimer,
    OperationMode,
    OperationStatus,
    ExtendedOperationMode,
    FaultError,
    PowerfulTime,
    QuietMode,
    SensorMode,
    StatusDataMode,
    TankStatus,
)
from .auth import PanasonicSettings, CCAppVersion, PanasonicRequestHeader
from .const import BASE_PATH_ACC
from .api_client import AquareaAPIClient # Moved outside TYPE_CHECKING

if TYPE_CHECKING:
    # from .core import Client as AquareaClient # No longer needed here
    pass # Keep TYPE_CHECKING block if other type-checking-only imports are added later

_LOGGER = logging.getLogger(__name__)

# Forward declaration for type hinting to avoid circular imports
class AquareaClient: # Renamed Client to AquareaClient
    _api_client: AquareaAPIClient # Use direct type hint as AquareaAPIClient is imported

class DeviceManager:
    def __init__(self, client: AquareaClient, settings: PanasonicSettings, app_version: CCAppVersion, logger: logging.Logger): # Use direct type hint
        self._client = client
        self._settings = settings
        self._app_version = app_version
        self._logger = logger
        self._groups = None
        self._devices: list[DeviceInfo] | None = None
        self._unknown_devices: list[DeviceInfo] = []
        self._cache_devices = {}
        self._device_indexer = {}

    async def get_devices(self) -> list[DeviceInfo]:
        """Get list of devices and its configuration, without status."""
        if self._devices is None:
            self._devices = []
            self._unknown_devices = []
            # Assuming self._client.request can be called directly or passed through
            # and BASE_PATH_ACC is accessible.
            # This part needs to be carefully integrated with the actual Client class.
            groups_response = await self._client._api_client.request( # Changed to _api_client.request
                "GET",
                external_url=f"{BASE_PATH_ACC}/device/group",
                headers=await PanasonicRequestHeader.get(self._settings, self._app_version)
            )
            self._groups = await groups_response.json()

            if self._groups is not None and 'groupList' in self._groups:
                for group in self._groups['groupList']:
                    device_list = group.get('deviceList', [])
                    if not device_list:
                        device_list = group.get('deviceIdList', [])

                    for device_raw in device_list:
                        if device_raw:
                            device_id = device_raw.get("deviceGuid")
                            device_name = device_raw.get("deviceName", "Unknown Device")
                            operation_mode = OperationMode(device_raw.get("operationMode", 0)) # Default to 0 if not found
                            has_tank = "tankStatus" in device_raw # Check for presence of tankStatus key
                            firmware_version = "Unknown" # Mock data as it's not in the new structure

                            zones: list[DeviceZoneInfo] = []
                            for zone_record in device_raw.get("zoneStatus", []):
                                # Mock data for fields not present in the new zoneStatus structure
                                zone_id = zone_record.get("zoneId")
                                if zone_id is not None:
                                    zone = DeviceZoneInfo(
                                        zone_id,
                                        f"Zone {zone_id}", # Mock zone name
                                        "Unknown", # Mock zone type
                                        False, # Mock cool_mode
                                        SensorMode.DIRECT, # Mock heat_sensor
                                        SensorMode.DIRECT, # Mock cool_sensor
                                        SensorMode.DIRECT, # Mock cool_sensor
                                    )
                                    zones.append(zone)

                            device_info = DeviceInfo(
                                device_id,
                                device_name,
                                device_id,
                                operation_mode,
                                has_tank,
                                firmware_version,
                                zones,
                                0
                            )
                            self._device_indexer[device_id] = device_id
                            self._devices.append(device_info)
        return self._devices + self._unknown_devices

    async def get_device_status(self, device_info: DeviceInfo) -> DeviceStatus:
        """Retrives device status."""
        json_response = None
        if (device_info.status_data_mode == StatusDataMode.LIVE 
            or (device_info.device_id in self._cache_devices and self._cache_devices[device_info.device_id] <= 0)):
            try:
                payload = {
                    "apiName": f"/remote/v1/api/devices?gwid={device_info.device_id}&deviceDirect=1",
                    "requestMethod": "GET"
                }
                response = await self._client._api_client.request( # Changed to _api_client.request
                    "POST", # Method is POST for the transfer API
                    url="remote/v1/app/common/transfer", # Specific URL for transfer API
                    json=payload, # Pass payload as json
                    throw_on_error=True
                )
                json_response = await response.json() # Get JSON from response
                device_info.status_data_mode = StatusDataMode.LIVE
            except Exception as e:
                self._logger.warning("Failed to get live status for device {} switching to cached data.".format(device_info.device_id))
                device_info.status_data_mode = StatusDataMode.CACHED
                self._cache_devices[device_info.device_id] = 10
        
        if json_response is None: # If live data failed or not requested, try cached
            payload = {
                "apiName": f"/remote/v1/api/devices?gwid={device_info.device_id}&deviceDirect=0",
                "requestMethod": "GET"
            }
            response = await self._client._api_client.request( # Changed to _api_client.request
                "POST", # Method is POST for the transfer API
                url="remote/v1/app/common/transfer", # Specific URL for transfer API
                json=payload, # Pass payload as json
                throw_on_error=True
            )
            json_response = await response.json() # Get JSON from response
            # Ensure the key exists before decrementing
            self._cache_devices[device_info.device_id] = self._cache_devices.get(device_info.device_id, 10) - 1

        device = json_response.get("status")
        operation_mode_value = device.get("operationMode")

        device_status = DeviceStatus(
            long_id=device_info.device_id, # Use device_info.long_id here
            operation_status=OperationStatus(device.get("specialStatus")),
            device_status=DeviceModeStatus(device.get("deiceStatus")),
            temperature_outdoor=device.get("outdoorNow"),
            operation_mode=ExtendedOperationMode.OFF
            if operation_mode_value == 99
            else ExtendedOperationMode(operation_mode_value),
            fault_status=[
                FaultError(fault_status["errorMessage"], fault_status["errorCode"])
                for fault_status in device.get("faultStatus", [])
            ],
            direction=device.get("direction"),
            pump_duty=device.get("pumpDuty"),
            tank_status=[
                TankStatus(
                    OperationStatus(tank_status["operationStatus"]),
                    tank_status["temparatureNow"],
                    tank_status["heatMax"],
                    tank_status["heatMin"],
                    tank_status["heatSet"],
                )
                for tank_status in device.get("tankStatus", [])
                if isinstance(tank_status, dict)
            ],
            zones=[
                DeviceZoneStatus(
                    zone_id=zone_status.get("zoneId"),
                    temperature=zone_status.get("temparatureNow"),
                    operation_status=OperationStatus(zone_status.get("operationStatus")),
                    heat_max=zone_status.get("heatMax"),
                    heat_min=zone_status.get("heatMin"),
                    heat_set=zone_status.get("heatSet"),
                    cool_max=zone_status.get("coolMax"),
                    cool_min=zone_status.get("coolMin"),
                    cool_set=zone_status.get("coolSet"),
                    comfort_cool=zone_status.get("comfortCool"),
                    comfort_heat=zone_status.get("comfortHeat"),
                    eco_cool=zone_status.get("ecoCool"),
                    eco_heat=zone_status.get("ecoHeat"),
                )
                for zone_status in device.get("zoneStatus", [])
                if isinstance(zone_status, dict)
            ],
            quiet_mode=QuietMode(device.get("quietMode", 0)),
            force_dhw=ForceDHW(device.get("forceDHW", 0)),
            force_heater=ForceHeater(device.get("forceHeater", 0)),
            holiday_timer=HolidayTimer(device.get("holidayTimer", 0)),
            powerful_time=PowerfulTime(device.get("powerful", 0)),
            special_status=None, # Simplified to None
        )

        return device_status
