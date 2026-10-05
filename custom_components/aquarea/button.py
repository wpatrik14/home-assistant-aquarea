"""Buttons for Aquarea integration."""

import logging

import aioaquarea
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AquareaConfigEntry, AquareaDataUpdateCoordinator
from .entity import AquareaBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AquareaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea buttons from config entry."""

    data: dict[str, AquareaDataUpdateCoordinator] = config_entry.runtime_data

    entities: list[ButtonEntity] = []

    entities.extend(
        [AquareaDefrostButton(coordinator) for coordinator in data.values()]
    )

    async_add_entities(entities)


class AquareaDefrostButton(AquareaBaseEntity, ButtonEntity):
    """Representation of a Aquarea button that request the device to start the defrost process."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{super().unique_id}_request_defrost"
        self._attr_translation_key = "request_defrost"
        self._attr_icon = "mdi:snowflake-melt"

    async def async_press(self) -> None:
        """Request to start the defrost process."""
        _LOGGER.debug(
            "Requesting defrost for device %s",
            self.coordinator.device.device_id,
        )
        if (
            self.coordinator.device.device_mode_status
            is not aioaquarea.DeviceModeStatus.DEFROST
        ):
            await self.coordinator.device.request_defrost()
