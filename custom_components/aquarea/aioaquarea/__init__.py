"""Asynchronous library to control Panasonic Aquarea devices"""
from __future__ import annotations

from typing import Tuple

from .core import AquareaClient as Client # Import AquareaClient and alias it as Client
from .data import (
    Device,
    DeviceAction,
    DeviceInfo,
    DeviceStatus,
    DeviceModeStatus,
    ExtendedOperationMode,
    OperationStatus,
    QuietMode,
    ForceDHW,
    ForceHeater,
    Tank,
    UpdateOperationMode,
    HolidayTimer,
    PowerfulTime,
    SpecialStatus
)
from .errors import (
    ApiError,
    AuthenticationError,
    AuthenticationErrorCodes,
    ClientError,
    DataNotAvailableError,
    RequestFailedError,
)
from .statistics import Consumption, ConsumptionType, DateType

from .const import AquareaEnvironment

__all__: Tuple[str, ...] = (
    "Client", # Keep "Client" in __all__ as it's aliased
    "Device",
    "ClientError",
    "RequestFailedError",
    "AuthenticationError",
    "AuthenticationErrorCodes",
    "ApiError",
    "DeviceInfo",
    "DeviceStatus",
    "DeviceAction",
    "Tank",
    "OperationStatus",
    "ExtendedOperationMode",
    "UpdateOperationMode",
    "QuietMode",
    "ForceDHW",
    "DateType",
    "Consumption",
    "ConsumptionType",
    "DataNotAvailableError",
    "DeviceModeStatus",
    "ForceHeater",
    "HolidayTimer",
    "PowerfulTime",
    "AquareaEnvironment",
    "SpecialStatus",
)
