"""Select entities for Aquarea integration."""
import asyncio
import logging

from aioaquarea import PowerfulTime, QuietMode

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AquareaBaseEntity
from .const import DEVICES, DOMAIN
from .coordinator import AquareaDataUpdateCoordinator

SELECT_DELAY = 10.0

_LOGGER = logging.getLogger(__name__)

QUIET_MODE_LOOKUP = {
    "level1" : QuietMode.LEVEL1,
    "level2" : QuietMode.LEVEL2,
    "level3" : QuietMode.LEVEL3,
    "off" : QuietMode.OFF
}

QUIET_MODE_REVERSE_LOOKUP = {v: k for k, v in QUIET_MODE_LOOKUP.items()}

POWERFUL_TIME_LOOKUP = {
    "on-30m" : PowerfulTime.ON_30MIN,
    "on-60m" : PowerfulTime.ON_60MIN,
    "on-90m" : PowerfulTime.ON_90MIN,
    "off" : PowerfulTime.OFF
}

POWERFUL_TIME_REVERSE_LOOKUP = {v: k for k, v in POWERFUL_TIME_LOOKUP.items()}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea select entities from config entry."""

    data: dict[str, AquareaDataUpdateCoordinator] = hass.data[DOMAIN][
        config_entry.entry_id
    ][DEVICES]

    entities: list[SelectEntity] = []

    entities.extend([AquareaQuietModeSelect(coordinator) for coordinator in data.values()])
    entities.extend([AquareaPowerfulTimeSelect(coordinator) for coordinator in data.values()])

    async_add_entities(entities)

class AquareaQuietModeSelect(AquareaBaseEntity, SelectEntity):
    """Representation of an Aquarea select entity to configure the device's quiet mode."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{super().unique_id}_quiet_mode"
        self._attr_translation_key = "quiet_mode"
        self._attr_options = list(QUIET_MODE_LOOKUP.keys())
        self._attr_icon = "mdi:volume-off"
        self._optimistic_option: str | None = None

    @property
    def current_option(self) -> str:
        """The current select option."""
        if self._optimistic_option is not None:
            return self._optimistic_option
        return QUIET_MODE_REVERSE_LOOKUP.get(self.coordinator.device.quiet_mode)

    async def _schedule_refresh(self, delay: float = SELECT_DELAY) -> None:
        """Clear optimistic state and request a coordinator refresh after a short delay."""
        await asyncio.sleep(delay)
        self._optimistic_option = None
        await self.coordinator.async_request_refresh()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if(quiet_mode := QUIET_MODE_LOOKUP.get(option)) is None:
            return

        if quiet_mode is self.coordinator.device.quiet_mode:
            return

        _LOGGER.debug(
            "Setting Quiet Mode of device %s, from %s to %s",
            self.coordinator.device.device_id,
            str(self.coordinator.device.quiet_mode),
            str(quiet_mode)
        )
        self._optimistic_option = option
        self.async_write_ha_state()
        await self.coordinator.device.set_quiet_mode(quiet_mode)
        self.hass.async_create_task(self._schedule_refresh())

class AquareaPowerfulTimeSelect(AquareaBaseEntity, SelectEntity):
    """Representation of an Aquarea select entity to configure the device's powerful time."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{super().unique_id}_powerful_time"
        self._attr_translation_key = "powerful_time"
        self._attr_options = list(POWERFUL_TIME_LOOKUP.keys())
        self._optimistic_option: str | None = None

    @property
    def icon(self) -> str:
        """Return the icon."""
        if self._optimistic_option is not None:
            return "mdi:fire-off" if self._optimistic_option == "off" else "mdi:fire"
        return "mdi:fire-off" if self.coordinator.device.powerful_time is PowerfulTime.OFF else "mdi:fire"

    @property
    def current_option(self) -> str:
        """The current select option."""
        if self._optimistic_option is not None:
            return self._optimistic_option
        return POWERFUL_TIME_REVERSE_LOOKUP.get(self.coordinator.device.powerful_time)

    async def _schedule_refresh(self, delay: float = SELECT_DELAY) -> None:
        """Clear optimistic state and request a coordinator refresh after a short delay."""
        await asyncio.sleep(delay)
        self._optimistic_option = None
        await self.coordinator.async_request_refresh()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if(powerful_time := POWERFUL_TIME_LOOKUP.get(option)) is None:
            return

        if powerful_time is self.coordinator.device.powerful_time:
            return

        _LOGGER.debug(
            "Setting Powerful Time of device %s, from %s to %s",
            self.coordinator.device.device_id,
            str(self.coordinator.device.powerful_time),
            str(powerful_time)
        )
        self._optimistic_option = option
        self.async_write_ha_state()
        await self.coordinator.device.set_powerful_time(powerful_time)
        self.hass.async_create_task(self._schedule_refresh())
