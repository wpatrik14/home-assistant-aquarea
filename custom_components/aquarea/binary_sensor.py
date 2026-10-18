"""Binary sensors for the Aquarea integration."""

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

import aioaquarea
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AquareaConfigEntry, AquareaDataUpdateCoordinator
from .entity import AquareaBaseEntity

_LOGGER = logging.getLogger(__name__)


def _flag_state(raw: int | bool | None) -> bool | None:
    """Turn a raw 0/1/2... flag reported by the device into on/off.

    ``None`` means the device does not report the flag. The meaning of values
    above 1 is not known, so any non-zero value counts as on and the raw value
    is kept as an attribute.
    """
    if raw is None:
        return None
    return raw != 0


@dataclass(frozen=True, kw_only=True)
class AquareaFlagBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a diagnostic binary sensor backed by a raw device flag."""

    value_fn: Callable[[aioaquarea.Device], int | bool | None]
    exists_fn: Callable[[aioaquarea.Device], bool] | None = None
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    entity_registry_enabled_default: bool = False

    def exists(self, device: aioaquarea.Device) -> bool:
        """Return true if the device has this flag, so the entity is created."""
        if self.exists_fn is not None:
            return self.exists_fn(device)
        return self.value_fn(device) is not None


FLAG_BINARY_SENSORS: tuple[AquareaFlagBinarySensorDescription, ...] = (
    AquareaFlagBinarySensorDescription(
        key="bivalent",
        translation_key="bivalent",
        value_fn=lambda device: device.bivalent,
    ),
    AquareaFlagBinarySensorDescription(
        key="bivalent_actual",
        translation_key="bivalent_actual",
        value_fn=lambda device: device.bivalent_actual,
    ),
    AquareaFlagBinarySensorDescription(
        key="external_heater",
        translation_key="external_heater",
        value_fn=lambda device: device.external_heater,
    ),
    AquareaFlagBinarySensorDescription(
        key="electric_anode",
        translation_key="electric_anode",
        value_fn=lambda device: device.electric_anode,
        exists_fn=lambda device: (
            device.has_tank is True and device.electric_anode is not None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AquareaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea binary sensors from config entry."""

    data: dict[str, AquareaDataUpdateCoordinator] = config_entry.runtime_data

    entities: list[BinarySensorEntity] = []

    for coordinator in data.values():
        entities.append(AquareaStatusBinarySensor(coordinator))
        entities.append(AquareaDefrostBinarySensor(coordinator))
        entities.extend(
            AquareaFlagBinarySensor(coordinator, description)
            for description in FLAG_BINARY_SENSORS
            if description.exists(coordinator.device)
        )

    async_add_entities(entities)


class AquareaStatusBinarySensor(AquareaBaseEntity, BinarySensorEntity):
    """Representation of a Aquarea sensor that indicates if the device is on error."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{super().unique_id}_status"
        self._attr_translation_key = "status"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self.coordinator.device.is_on_error


class AquareaDefrostBinarySensor(AquareaBaseEntity, BinarySensorEntity):
    """Representation of a Aquarea sensor that indicates if the device is on defrost mode."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{super().unique_id}_defrost"
        self._attr_translation_key = "defrost"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:snowflake-melt" if self.is_on else "mdi:snowflake-off"

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return (
            self.coordinator.device.device_mode_status
            is aioaquarea.DeviceModeStatus.DEFROST
        )


class AquareaFlagBinarySensor(AquareaBaseEntity, BinarySensorEntity):
    """A diagnostic binary sensor showing a raw flag reported by the device."""

    entity_description: AquareaFlagBinarySensorDescription

    def __init__(
        self,
        coordinator: AquareaDataUpdateCoordinator,
        description: AquareaFlagBinarySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self.entity_description = description
        self._attr_unique_id = f"{super().unique_id}_{description.key}"

    @property
    def _raw(self) -> int | bool | None:
        """Return the raw flag as the device reports it."""
        return self.entity_description.value_fn(self.coordinator.device)

    @property
    def is_on(self) -> bool | None:
        """Return true if the flag is set."""
        return _flag_state(self._raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw value, as its meaning is not documented."""
        return {"raw_value": self._raw}
