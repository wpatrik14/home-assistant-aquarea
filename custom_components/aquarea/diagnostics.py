"""Diagnostics support for the Aquarea Smart Cloud integration."""

from __future__ import annotations

from enum import Enum
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.loader import async_get_integration

from .const import CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import AquareaConfigEntry, AquareaDataUpdateCoordinator

# The account, the entry's identifiers and every user-given or device-unique
# name. "name" also covers the zone names.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    "title",
    "unique_id",
    "device_id",
    "long_id",
    "name",
}

LIBRARY_PACKAGE = "aioaquarea-ng"


def _plain(value: Any) -> Any:
    """Return a JSON-safe form of a value: enums by name, unknown types as None."""
    if isinstance(value, Enum):
        return value.name
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return None


def _attrs(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """Read the named attributes of obj; a missing one comes back as None."""
    return {name: _plain(getattr(obj, name, None)) for name in names}


def _library_version() -> str | None:
    """Return the installed aioaquarea-ng version (blocking: reads metadata)."""
    try:
        return package_version(LIBRARY_PACKAGE)
    except PackageNotFoundError:
        return None


async def _versions(hass: HomeAssistant) -> dict[str, str | None]:
    """Return the integration, Home Assistant and library versions."""
    integration = await async_get_integration(hass, DOMAIN)
    return {
        "integration": str(integration.version) if integration.version else None,
        "home_assistant": HA_VERSION,
        "library": await hass.async_add_executor_job(_library_version),
    }


def _device_diagnostics(coordinator: AquareaDataUpdateCoordinator) -> dict[str, Any]:
    """Describe one device's coordinator and last known state."""
    device = coordinator.device
    info = coordinator.device_info

    last_exception = coordinator.last_exception
    update_interval = coordinator.update_interval
    fetched_at = coordinator.hourly_consumption_fetched_at
    # Never coordinator.name: it contains the account's user name.
    coordinator_block = {
        "last_update_success": coordinator.last_update_success,
        "last_exception": (
            type(last_exception).__name__ if last_exception is not None else None
        ),
        "update_interval": (
            update_interval.total_seconds() if update_interval is not None else None
        ),
        "consumption_interval": coordinator.consumption_interval,
        "month_buckets": len(coordinator.month_consumption or []),
        "hourly_days_cached": len(coordinator._day_consumption),  # noqa: SLF001
        "hourly_consumption_fetched_at": (
            fetched_at.isoformat() if fetched_at is not None else None
        ),
    }

    device_info_block = _attrs(
        info,
        (
            "model",
            "firmware_version",
            "has_tank",
            "status_data_mode",
            "connection_status",
            "dhw_flag",
            "device_id",
            "long_id",
            "name",
        ),
    )

    status = _attrs(
        device,
        (
            "mode",
            "operation_status",
            "temperature_outdoor",
            "device_mode_status",
            "current_direction",
            "pump_duty",
            "quiet_mode",
            "force_dhw",
            "force_heater",
            "holiday_timer",
            "powerful_time",
            "special_status",
            "support_special_status",
            "water_pressure",
            "cool_mode",
            "bivalent",
            "bivalent_actual",
            "external_heater",
            "electric_anode",
            "control_box",
            "uncontrollable",
            "service_type",
            "multi_od_connection",
            "model_series_selection",
            "stand_alone",
        ),
    )
    # The library exposes the first active fault only.
    error = getattr(device, "current_error", None)
    status["faults"] = (
        [
            {
                "code": _plain(getattr(error, "error_code", None)),
                "message": _plain(getattr(error, "error_message", None)),
            }
        ]
        if error is not None
        else []
    )

    tank = getattr(device, "tank", None)
    tank_block = (
        _attrs(
            tank,
            (
                "operation_status",
                "temperature",
                "target_temperature",
                "heat_min",
                "heat_max",
            ),
        )
        if tank is not None
        else None
    )

    zones = [
        _attrs(
            zone,
            (
                "zone_id",
                "name",
                "temperature",
                "operation_status",
                "heat_target_temperature",
                "heat_min",
                "heat_max",
                "cool_target_temperature",
                "cool_min",
                "cool_max",
                "zone_type_id",
                "zone_sensor_id",
                "sensor_mode",
                "supports_set_temperature",
                "supports_special_status",
                "cool_mode",
            ),
        )
        for zone in (getattr(device, "zones", None) or {}).values()
    ]

    return {
        "coordinator": coordinator_block,
        "device_info": device_info_block,
        "status": status,
        "tank": tank_block,
        "zones": zones,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AquareaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "versions": await _versions(hass),
        "devices": [
            async_redact_data(_device_diagnostics(coordinator), TO_REDACT)
            for coordinator in entry.runtime_data.values()
        ],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: AquareaConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one device of the config entry."""
    coordinators = [
        coordinator
        for device_id, coordinator in entry.runtime_data.items()
        if (DOMAIN, device_id) in device.identifiers
    ]
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "versions": await _versions(hass),
        "devices": [
            async_redact_data(_device_diagnostics(coordinator), TO_REDACT)
            for coordinator in coordinators
        ],
    }
