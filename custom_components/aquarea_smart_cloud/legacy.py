"""Adopting the config entry of the old `aquarea` domain.

Before version 3.0.0 the integration was `aquarea`, which collided with the
heishamon integration (same folder name). Its config entries stay in Home
Assistant's storage after the folder is gone, so the new domain finds them and
moves the account, the devices, the entities (ids, history and customisations
are kept) and the lifetime totals to a new entry.
"""

import inspect
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import CONF_LEGACY_ENTRY, DOMAIN, LEGACY_DOMAIN
from .lifetime import STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

_MOVES_DEVICES = (
    "new_config_entry_id"
    in inspect.signature(dr.DeviceRegistry.async_update_device).parameters
)


def find_legacy_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Entries of the old domain that are ours, not heishamon's.

    Ours carry the Panasonic ID and its password; entries already adopted by a
    new entry (a flow that created it but has not run its setup yet) are left out.
    """
    adopted = {
        entry.data.get(CONF_LEGACY_ENTRY)
        for entry in hass.config_entries.async_entries(DOMAIN)
    }
    return [
        entry
        for entry in hass.config_entries.async_entries(LEGACY_DOMAIN)
        if entry.data.get(CONF_USERNAME)
        and entry.data.get(CONF_PASSWORD)
        and entry.entry_id not in adopted
    ]


def _move_entities(hass: HomeAssistant, old: ConfigEntry, new: ConfigEntry) -> None:
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, old.entry_id):
        if registry.async_get_entity_id(entity.domain, DOMAIN, entity.unique_id):
            _LOGGER.warning(
                "Not moving %s: the new domain already has this entity",
                entity.entity_id,
            )
            continue
        try:
            registry.async_update_entity_platform(
                entity.entity_id, DOMAIN, new_config_entry_id=new.entry_id
            )
        except ValueError as err:
            _LOGGER.warning("Not moving %s: %s", entity.entity_id, err)


def _move_devices(hass: HomeAssistant, old: ConfigEntry, new: ConfigEntry) -> None:
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, old.entry_id):
        identifiers = {
            (DOMAIN if domain == LEGACY_DOMAIN else domain, value)
            for domain, value in device.identifiers
        }
        # A device belongs to one config entry in recent Home Assistant; the
        # add/remove pair is only for releases that predate `new_config_entry_id`.
        entry_change: dict[str, Any] = (
            {"new_config_entry_id": new.entry_id}
            if _MOVES_DEVICES
            else {
                "add_config_entry_id": new.entry_id,
                "remove_config_entry_id": old.entry_id,
            }
        )
        try:
            registry.async_update_device(
                device.id, new_identifiers=identifiers, **entry_change
            )
        except ValueError as err:
            _LOGGER.warning("Not moving device %s: %s", device.id, err)


async def _move_lifetime_store(
    hass: HomeAssistant, old: ConfigEntry, new: ConfigEntry
) -> None:
    old_store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{LEGACY_DOMAIN}.lifetime.{old.entry_id}"
    )
    new_store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}.lifetime.{new.entry_id}"
    )
    data = await old_store.async_load()
    if data is not None and await new_store.async_load() is None:
        await new_store.async_save(data)
    await old_store.async_remove()


async def async_adopt_legacy_entry(hass: HomeAssistant, new: ConfigEntry) -> None:
    """Move everything of the old entry to `new`, then remove the old entry.

    Safe to run again after an interruption: what has moved is not touched, and
    the marker in the entry's data goes last.
    """
    old = hass.config_entries.async_get_entry(new.data[CONF_LEGACY_ENTRY])
    if old is not None and old.domain == LEGACY_DOMAIN:
        if old.state is ConfigEntryState.LOADED:
            # The old folder is still installed and running: its entities can
            # only move once they are unloaded.
            await hass.config_entries.async_unload(old.entry_id)
        _move_entities(hass, old, new)
        _move_devices(hass, old, new)
        await _move_lifetime_store(hass, old, new)
        await hass.config_entries.async_remove(old.entry_id)
        _LOGGER.info("Moved the %s entry %s to %s", LEGACY_DOMAIN, old.title, DOMAIN)
    hass.config_entries.async_update_entry(
        new, data={k: v for k, v in new.data.items() if k != CONF_LEGACY_ENTRY}
    )
