"""Aquarea Switch Sensors."""
import asyncio
import logging

import aioaquarea

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AquareaBaseEntity
from .const import DEVICES, DOMAIN
from .coordinator import AquareaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SWITCH_DELAY = 10.0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea binary sensors from config entry."""

    data: dict[str, AquareaDataUpdateCoordinator] = hass.data[DOMAIN][
        config_entry.entry_id
    ][DEVICES]

    entities: list[SwitchEntity] = []

    for coordinator in data.values():
        if coordinator.device.has_tank:
            entities.append(AquareaForceDHWSwitch(coordinator))
        entities.append(AquareaForceHeaterSwitch(coordinator))
        entities.append(AquareaHolidayTimerSwitch(coordinator))

    async_add_entities(entities)


class AquareaForceDHWSwitch(AquareaBaseEntity, SwitchEntity):
    """Representation of an Aquarea switch."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)

        self._attr_translation_key = "force_dhw"
        self._attr_unique_id = f"{super().unique_id}_force_dhw"
        self._optimistic_is_on: bool | None = None

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:water-boiler" if self.is_on else "mdi:water-boiler-off"

    @property
    def is_on(self) -> bool:
        """If force DHW mode is enabled."""
        if self._optimistic_is_on is not None:
            return self._optimistic_is_on
        return self.coordinator.device.force_dhw is aioaquarea.ForceDHW.ON

    async def _schedule_refresh(self, delay: float = 10.0) -> None:
        """Schedule a single coordinator refresh after a short delay."""
        await asyncio.sleep(delay)
        self._optimistic_is_on = None
        try:
            await self.coordinator.async_request_refresh(force_fetch=True)
        except aioaquarea.errors.RequestFailedError:
            _LOGGER.exception(
                "Delayed refresh failed for device %s",
                getattr(self.coordinator.device, "device_id", "unknown"),
            )

    async def async_turn_on(self) -> None:
        """Turn on Force DHW."""
        self._optimistic_is_on = True
        self.async_write_ha_state()
        await self.coordinator.device.set_force_dhw(aioaquarea.ForceDHW.ON)
        self.hass.async_create_task(self._schedule_refresh(SWITCH_DELAY))

    async def async_turn_off(self) -> None:
        """Turn off Force DHW."""
        self._optimistic_is_on = False
        self.async_write_ha_state()
        await self.coordinator.device.set_force_dhw(aioaquarea.ForceDHW.OFF)
        self.hass.async_create_task(self._schedule_refresh(SWITCH_DELAY))


class AquareaForceHeaterSwitch(AquareaBaseEntity, SwitchEntity):
    """Representation of an Aquarea switch."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)

        self._attr_translation_key = "force_heater"
        self._attr_unique_id = f"{super().unique_id}_force_heater"
        self._optimistic_is_on: bool | None = None

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:hvac" if self.is_on else "mdi:hvac-off"

    @property
    def is_on(self) -> bool:
        """If force heater mode is enabled."""
        if self._optimistic_is_on is not None:
            return self._optimistic_is_on
        return self.coordinator.device.force_heater is aioaquarea.ForceHeater.ON

    async def _schedule_refresh(self, delay: float = 10.0) -> None:
        """Schedule a single coordinator refresh after a short delay."""
        await asyncio.sleep(delay)
        self._optimistic_is_on = None
        try:
            await self.coordinator.async_request_refresh(force_fetch=True)
        except aioaquarea.errors.RequestFailedError:
            _LOGGER.exception(
                "Delayed refresh failed for device %s",
                getattr(self.coordinator.device, "device_id", "unknown"),
            )

    async def async_turn_on(self) -> None:
        """Turn on Force heater."""
        self._optimistic_is_on = True
        self.async_write_ha_state()
        await self.coordinator.device.set_force_heater(aioaquarea.ForceHeater.ON)
        self.hass.async_create_task(self._schedule_refresh(SWITCH_DELAY))

    async def async_turn_off(self) -> None:
        """Turn off Force heater."""
        self._optimistic_is_on = False
        self.async_write_ha_state()
        await self.coordinator.device.set_force_heater(aioaquarea.ForceHeater.OFF)
        self.hass.async_create_task(self._schedule_refresh(SWITCH_DELAY))

class AquareaHolidayTimerSwitch(AquareaBaseEntity, SwitchEntity):
    """Representation of an Aquarea switch."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)

        self._attr_translation_key = "holiday_timer"
        self._attr_unique_id = f"{super().unique_id}_holiday_timer"
        self._optimistic_is_on: bool | None = None

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:timer-check" if self.is_on else "mdi:timer-off"

    @property
    def is_on(self) -> bool:
        """If the holiday timer mode is enabled."""
        if self._optimistic_is_on is not None:
            return self._optimistic_is_on
        return self.coordinator.device.holiday_timer is aioaquarea.HolidayTimer.ON

    async def _schedule_refresh(self, delay: float = 10.0) -> None:
        """Schedule a single coordinator refresh after a short delay."""
        await asyncio.sleep(delay)
        self._optimistic_is_on = None
        try:
            await self.coordinator.async_request_refresh(force_fetch=True)
        except aioaquarea.errors.RequestFailedError:
            _LOGGER.exception(
                "Delayed refresh failed for device %s",
                getattr(self.coordinator.device, "device_id", "unknown"),
            )

    async def async_turn_on(self) -> None:
        """Turn on Holiday Timer."""
        self._optimistic_is_on = True
        self.async_write_ha_state()
        await self.coordinator.device.set_holiday_timer(aioaquarea.HolidayTimer.ON)
        self.hass.async_create_task(self._schedule_refresh(SWITCH_DELAY))

    async def async_turn_off(self) -> None:
        """Turn off Holiday Timer."""
        self._optimistic_is_on = False
        self.async_write_ha_state()
        await self.coordinator.device.set_holiday_timer(aioaquarea.HolidayTimer.OFF)
        self.hass.async_create_task(self._schedule_refresh(SWITCH_DELAY))
