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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import WEEKLY_TIMER_MAX_AGE
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


def _strict_flag_state(raw: Any) -> bool | None:
    """Turn a real boolean flag into on/off, anything else counts as off.

    ``None`` means the device does not report the flag. Only an actual ``True``
    is on, so a value of an unexpected type never reads as set.
    """
    if raw is None:
        return None
    return raw is True


@dataclass(frozen=True, kw_only=True)
class AquareaFlagBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a diagnostic binary sensor backed by a raw device flag."""

    value_fn: Callable[[aioaquarea.Device], int | bool | None]
    exists_fn: Callable[[aioaquarea.Device], bool] | None = None
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    entity_registry_enabled_default: bool = False
    state_fn: Callable[[Any], bool | None] = _flag_state
    warn_when_on: str | None = None

    def exists(self, device: aioaquarea.Device) -> bool:
        """Return true if the device has this flag, so the entity is created."""
        if self.exists_fn is not None:
            return self.exists_fn(device)
        return self.value_fn(device) is not None


FLAG_BINARY_SENSORS: tuple[AquareaFlagBinarySensorDescription, ...] = (
    AquareaFlagBinarySensorDescription(
        key="uncontrollable",
        translation_key="uncontrollable",
        value_fn=lambda device: device.uncontrollable,
        state_fn=_strict_flag_state,
        entity_registry_enabled_default=True,
        warn_when_on=(
            "The adapter of %s reports that the device is uncontrollable "
            "(uncontrollableTaw1Flg). Commands may be ignored; the entities "
            "stay available"
        ),
    ),
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
        entities.append(AquareaWeeklyTimerBinarySensor(coordinator))
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
        self._was_on: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Note the first state, so a flag that is already set is reported."""
        await super().async_added_to_hass()
        self._warn_on_transition()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._warn_on_transition()
        super()._handle_coordinator_update()

    def _warn_on_transition(self) -> None:
        """Log a warning once each time the flag turns on, if the sensor asks."""
        is_on = self.is_on
        message = self.entity_description.warn_when_on
        if message is not None and is_on is True and self._was_on is not True:
            _LOGGER.warning(message, self.coordinator.device_info.name)
        self._was_on = is_on

    @property
    def _raw(self) -> int | bool | None:
        """Return the raw flag as the device reports it."""
        return self.entity_description.value_fn(self.coordinator.device)

    @property
    def is_on(self) -> bool | None:
        """Return true if the flag is set."""
        return self.entity_description.state_fn(self._raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw value, as its meaning is not documented."""
        raw = self._raw
        return {"raw_value": raw if isinstance(raw, int) else None}


class AquareaWeeklyTimerBinarySensor(AquareaBaseEntity, BinarySensorEntity):
    """Whether the device's weekly schedule is switched on (read only)."""

    _attr_translation_key = "weekly_timer"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{super().unique_id}_weekly_timer"

    async def async_added_to_hass(self) -> None:
        """Ask the coordinator to read the schedule while this entity is enabled."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.subscribe_weekly_timer())

    @property
    def available(self) -> bool:
        """Return true while the schedule was read recently."""
        fetched_at = self.coordinator.weekly_timer_fetched_at
        return (
            super().available
            and fetched_at is not None
            and dt_util.now() - fetched_at <= WEEKLY_TIMER_MAX_AGE
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the weekly schedule is switched on."""
        timer = self.coordinator.weekly_timer
        return None if timer is None else timer.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the days with a scheduled change and the number of changes."""
        timer = self.coordinator.weekly_timer
        days = [] if timer is None else timer.days
        return {
            "scheduled_days": [day.name for day in days if day.active_slots],
            "slot_count": sum(len(day.active_slots) for day in days),
        }
