"""Climate entity to control a zone for a Panasonic Aquarea Device."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aioaquarea import (
    DeviceAction,
    ExtendedOperationMode,
    OperationStatus,
    SpecialStatus,
    UpdateOperationMode,
    DeviceDirection,
)
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AquareaBaseEntity
from .const import DEVICES, DOMAIN
from .coordinator import AquareaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SPECIAL_STATUS_LOOKUP: dict[str, SpecialStatus | None] = {
    PRESET_ECO: SpecialStatus.ECO,
    PRESET_COMFORT: SpecialStatus.COMFORT,
    PRESET_NONE: None,
}
SPECIAL_STATUS_REVERSE_LOOKUP = {v: k for k, v in SPECIAL_STATUS_LOOKUP.items()}


def _compare_states(state1, state2) -> bool:
    """Compare two states, handling different types of enums."""
    if state1 is None or state2 is None:
        return state1 is state2

    # If both are enums, compare their values
    if hasattr(state1, "value") and hasattr(state2, "value"):
        return state1.value == state2.value
    
    # If one is an enum and the other is not, try comparing value to the other state
    if hasattr(state1, "value"):
        return state1.value == state2
    if hasattr(state2, "value"):
        return state1 == state2.value

    # Otherwise, direct comparison
    return state1 == state2


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea climate entities from config entry."""
    data: dict[str, AquareaDataUpdateCoordinator] = hass.data[DOMAIN][
        config_entry.entry_id
    ][DEVICES]
    async_add_entities(
        [
            HeatPumpClimate(coordinator, zone_id)
            for coordinator in data.values()
            for zone_id in coordinator.device.zones
        ]
    )


def get_hvac_mode_from_ext_op_mode(
    mode: ExtendedOperationMode, zone_status: OperationStatus
) -> HVACMode:
    """Convert extended operation mode to HVAC mode."""
    if zone_status == OperationStatus.OFF:
        return HVACMode.OFF
    if mode == ExtendedOperationMode.HEAT:
        return HVACMode.HEAT
    if mode == ExtendedOperationMode.COOL:
        return HVACMode.COOL
    if mode in (ExtendedOperationMode.AUTO_COOL, ExtendedOperationMode.AUTO_HEAT):
        return HVACMode.AUTO
    return HVACMode.OFF


def get_hvac_action_from_ext_action(action: DeviceAction) -> HVACAction:
    """Convert device action to HVAC action."""
    if action == DeviceAction.HEATING:
        return HVACAction.HEATING
    if action == DeviceAction.COOLING:
        return HVACAction.COOLING
    if action == DeviceAction.IDLE:
        return HVACAction.IDLE
    return HVACAction.IDLE


def get_hvac_action_from_device_direction(
    direction: DeviceDirection, hvac_mode: HVACMode
) -> HVACAction:
    """Convert device direction to HVAC action."""
    if direction == DeviceDirection.PUMP:
        if hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if hvac_mode == HVACMode.COOL:
            return HVACAction.COOLING
    return HVACAction.IDLE


def get_update_operation_mode_from_hvac_mode(mode: HVACMode) -> UpdateOperationMode:
    """Convert HVAC mode to update operation mode."""
    if mode == HVACMode.HEAT:
        return UpdateOperationMode.HEAT
    if mode == HVACMode.COOL:
        return UpdateOperationMode.COOL
    if mode == HVACMode.AUTO:
        return UpdateOperationMode.AUTO
    return UpdateOperationMode.OFF


async def _optimistic_update_and_poll(
    self,
    set_func,
    expected_state_attr: str,
    expected_state_value,
    log_prefix: str,
    state_check_func=None,
    *args,
    **kwargs,
):
    """Optimistically update and poll for state change."""
    _LOGGER.debug(f"Optimistically setting {expected_state_attr} to {expected_state_value}")
    setattr(self, expected_state_attr, expected_state_value)
    self.async_write_ha_state()  # Push optimistic update immediately

    await set_func(*args, **kwargs)

    max_retries = 10
    retry_delay = 1  # seconds

    for i in range(max_retries):
        await self.coordinator.async_refresh()  # Trigger a refresh
        current_state = state_check_func(self) if state_check_func else getattr(self, expected_state_attr)
        _LOGGER.debug(
            f"Polling attempt {i+1}/{max_retries}. Current {expected_state_attr}: {current_state}, Expected: {expected_state_value}"
        )

        if _compare_states(current_state, expected_state_value):
            _LOGGER.debug(f"{log_prefix} matched after {i+1} retries. Exiting polling loop.")
            break

        if i < max_retries - 1:
            await asyncio.sleep(retry_delay)
    else:
        _LOGGER.warning(
            f"{log_prefix} did not match expected {expected_state_value} after {max_retries} retries. Final {expected_state_attr}: {current_state}"
        )

    _LOGGER.debug(f"{log_prefix} completed. Final {expected_state_attr}: {current_state}")


class HeatPumpClimate(AquareaBaseEntity, ClimateEntity):
    """The ClimateEntity that controls one zone of the Aquarea heat pump. Some settings are shared between zones. The entity, the library and the API will keep a consistent state between zones. """
    zone_id: int

    def __init__(self, coordinator: AquareaDataUpdateCoordinator, zone_id: int) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_name = self.coordinator.device.zones.get(zone_id).name
        self._attr_unique_id = f"{super().unique_id}_climate_{zone_id}"
        self._attr_hvac_mode = HVACMode.OFF  # Initialize with a default value
        self._attr_hvac_action = HVACAction.IDLE  # Initialize with a default value
        self._attr_current_temperature = None  # Initialize with a default value
        self._attr_max_temp = None  # Initialize with a default value
        self._attr_min_temp = None  # Initialize with a default value
        self._attr_target_temperature = None  # Initialize with a default value
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        )
        if self.coordinator.device.support_special_status:
            self._attr_supported_features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = list(SPECIAL_STATUS_LOOKUP.keys())
            self._attr_preset_mode = SPECIAL_STATUS_REVERSE_LOOKUP.get(
                self.coordinator.device.special_status
            )
        self._attr_precision = PRECISION_WHOLE
        self._attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.COOL, HVACMode.AUTO]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "Coordinator update for device %s, zone %s",
            self.coordinator.device.device_id,
            self._zone_id,
        )
        device = self.coordinator.device
        zone = device.zones.get(self._zone_id)
        self._attr_hvac_mode = get_hvac_mode_from_ext_op_mode(
            device.mode, zone.operation_status
        )
        self._attr_hvac_action = get_hvac_action_from_device_direction(
            device.current_direction, self._attr_hvac_mode
        )
        self._attr_icon = (
            "mdi:hvac-off" if device.mode == ExtendedOperationMode.OFF else "mdi:hvac"
        )
        self._attr_current_temperature = zone.temperature
        if device.support_special_status:
            self._attr_preset_mode = SPECIAL_STATUS_REVERSE_LOOKUP.get(
                device.special_status
            )
        # If the device doesn't allow to set the temperature directly
        # We set the max and min to the current temperature.
        # This is a workaround to make the UI work.
        self._attr_max_temp = zone.temperature
        self._attr_min_temp = zone.temperature
        if zone.supports_set_temperature and device.mode != ExtendedOperationMode.OFF:
            self._attr_max_temp = (
                zone.cool_max if device.mode in (ExtendedOperationMode.COOL, ExtendedOperationMode.AUTO_COOL) else zone.heat_max
            )
            self._attr_min_temp = (
                zone.cool_min if device.mode in (ExtendedOperationMode.COOL, ExtendedOperationMode.AUTO_COOL) else zone.heat_min
            )
        self._attr_target_temperature = (
            zone.cool_target_temperature if device.mode in (
                ExtendedOperationMode.COOL, ExtendedOperationMode.AUTO_COOL,
            ) else zone.heat_target_temperature
        )
        self._attr_target_temperature_step = 1
        super()._handle_coordinator_update()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode not in self.hvac_modes:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")
        _LOGGER.debug(
            "Setting operation mode of %s to %s",
            self.coordinator.device.device_id,
            hvac_mode,
        )
        await _optimistic_update_and_poll(
            self,
            self.coordinator.device.set_mode,
            "_attr_hvac_mode",
            hvac_mode,
            "Target HVAC mode",
            get_update_operation_mode_from_hvac_mode(hvac_mode).value,
            self._zone_id,
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature if supported by the zone."""
        zone = self.coordinator.device.zones.get(self._zone_id)
        temperature: float | None = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if temperature is not None and zone.supports_set_temperature:
            _LOGGER.debug(
                "Setting temperature of device:zone == %s:%s to %s",
                self.coordinator.device.device_id,
                zone.name,
                str(temperature),
            )
            await _optimistic_update_and_poll(
                self,
                self.coordinator.device.set_temperature,
                "_attr_target_temperature",
                temperature,
                "Target temperature",
                int(temperature),
                zone.zone_id,
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new target preset mode."""
        if preset_mode not in self.preset_modes:
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        _LOGGER.debug(
            "Setting preset mode of device %s to %s",
            self.coordinator.device.device_id,
            preset_mode,
        )
        await _optimistic_update_and_poll(
            self,
            self.coordinator.device.set_special_status,
            "_attr_preset_mode",
            preset_mode,
            "Target preset mode",
            SPECIAL_STATUS_LOOKUP[preset_mode],
        )

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        _LOGGER.debug(
            "Turning on device %s",
            self.coordinator.device.device_id,
        )
        await self.coordinator.device.turn_on()
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        _LOGGER.debug(
            "Turning off device %s",
            self.coordinator.device.device_id,
        )
        await self.coordinator.device.turn_off()
        self.hass.async_create_task(self.coordinator.async_request_refresh())
