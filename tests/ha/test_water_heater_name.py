"""The water heater's name comes from the translations.

Home Assistant builds the entity_id of a *new* entity from the name in the
language of the instance when that language has native entity IDs (Hungarian,
German, ...), like for every other translated entity of this integration.
Existing entities keep their entity_id through the entity registry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import DEVICE_ID

ENGLISH_ID = "water_heater.heat_pump_tank"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_tank_english_name_and_entity_id_are_unchanged(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """In English the name, entity_id and unique_id are what the hard-coded name gave."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(ENGLISH_ID)
    assert state is not None
    assert state.attributes["friendly_name"].endswith("Tank")
    entry = er.async_get(hass).async_get(ENGLISH_ID)
    assert entry.unique_id == f"{DEVICE_ID}_tank"
    assert entry.translation_key == "tank"


async def test_tank_new_install_in_hungarian(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A new Hungarian install gets the translated name and a native entity_id."""
    hass.config.language = "hu"
    await _setup(hass, mock_config_entry)

    state = hass.states.get("water_heater.heat_pump_tartaly")
    assert state is not None
    assert state.attributes["friendly_name"].endswith("Tartály")
    entry = er.async_get(hass).async_get("water_heater.heat_pump_tartaly")
    assert entry.unique_id == f"{DEVICE_ID}_tank"


async def test_tank_existing_entity_keeps_its_entity_id(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An entity registered before the change keeps its entity_id in any language."""
    mock_config_entry.add_to_hass(hass)
    entity_registry.async_get_or_create(
        "water_heater",
        "aquarea",
        f"{DEVICE_ID}_tank",
        config_entry=mock_config_entry,
        suggested_object_id="heat_pump_tank",
    )
    hass.config.language = "hu"
    await _setup(hass, mock_config_entry)

    assert hass.states.get(ENGLISH_ID) is not None
    assert hass.states.get("water_heater.heat_pump_tartaly") is None
    assert hass.states.get(ENGLISH_ID).attributes["friendly_name"].endswith("Tartály")
