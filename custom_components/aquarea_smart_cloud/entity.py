"""Base entity for the Aquarea Smart Cloud integration."""

from collections.abc import Coroutine
from typing import Any

import aioaquarea
import aiohttp
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import AquareaDataUpdateCoordinator


class AquareaBaseEntity(CoordinatorEntity[AquareaDataUpdateCoordinator]):
    """Common base for Aquarea entities."""

    coordinator: AquareaDataUpdateCoordinator
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize entity."""
        super().__init__(coordinator)

        self._attr_unique_id = self.coordinator.device_info.device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_info.device_id)},
            manufacturer="Panasonic",
            model=self.coordinator.device_info.model,
            name=self.coordinator.device_info.name,
            sw_version=self.coordinator.device_info.firmware_version,
        )

    async def _async_send_command[T](self, command: Coroutine[Any, Any, T]) -> T:
        """Await a cloud command, turning its failures into translated errors.

        Authentication failures, API/HTTP errors and timeouts raise a
        `HomeAssistantError` whose message is translated (`exceptions.*` in
        strings.json); the original error stays attached as its cause. Any
        other exception is a bug and propagates unchanged. Re-authentication
        itself is the coordinator's job, not a command's.
        """
        try:
            return await command
        except aioaquarea.AuthenticationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_authenticated",
                translation_placeholders={"error": str(err)},
            ) from err
        except (TimeoutError, aioaquarea.ClientError, aiohttp.ClientError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err

    def _start_delayed_refresh(self, refresh: Coroutine[Any, Any, None]) -> None:
        """Run a delayed post-command refresh as a background task of the entry.

        Home Assistant cancels an entry's background tasks when it unloads, so
        a refresh still sleeping when the entry is reloaded or removed does
        not outlive it.
        """
        self.coordinator.entry.async_create_background_task(
            self.hass, refresh, name=f"{DOMAIN} delayed refresh {self.entity_id}"
        )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
