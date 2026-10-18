"""The diagnostic binary sensors backed by raw device flags.

They are off the registry by default (the meaning of the raw values is not
verified), exist only when the device reports the flag, and show any non-zero
value as on with the raw value as an attribute.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea.binary_sensor import (
    AquareaFlagBinarySensorDescription,
    _flag_state,
)
from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID

# entity ID, device attribute / unique ID suffix
FLAGS = [
    pytest.param("binary_sensor.heat_pump_bivalent", "bivalent", id="bivalent"),
    pytest.param(
        "binary_sensor.heat_pump_bivalent_active_reported",
        "bivalent_actual",
        id="bivalent_actual",
    ),
    pytest.param(
        "binary_sensor.heat_pump_external_heater",
        "external_heater",
        id="external_heater",
    ),
]
FLAG_ARGS = ("entity_id", "attribute")
ALL_ATTRIBUTES = ("bivalent", "bivalent_actual", "external_heater")


def _report_nothing(device: MagicMock) -> None:
    """The mock device returns MagicMocks for flags nobody set: clear them."""
    for attribute in ALL_ATTRIBUTES:
        setattr(device, attribute, None)


def _enable(
    hass: HomeAssistant, entry: MockConfigEntry, entity_id: str, attribute: str
) -> None:
    """Register the entity as enabled, as if the user had switched it on."""
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DEVICE_ID}_{attribute}",
        config_entry=entry,
        suggested_object_id=entity_id.split(".", 1)[1],
        disabled_by=None,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("raw", "state"), [(0, STATE_OFF), (1, STATE_ON), (2, STATE_ON)]
)
@pytest.mark.parametrize(FLAG_ARGS, FLAGS)
async def test_state_follows_raw_value(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    entity_id: str,
    attribute: str,
    raw: int,
    state: str,
) -> None:
    """Zero is off, any other value is on, and the raw value is an attribute."""
    _report_nothing(mock_device)
    setattr(mock_device, attribute, raw)
    _enable(hass, mock_config_entry, entity_id, attribute)
    await _setup(hass, mock_config_entry)

    entity_state = hass.states.get(entity_id)
    assert entity_state is not None
    assert entity_state.state == state
    assert entity_state.attributes["raw_value"] == raw


@pytest.mark.parametrize(FLAG_ARGS, FLAGS)
async def test_no_entity_when_not_reported(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    entity_id: str,
    attribute: str,
) -> None:
    """A device that does not report the flag gets no entity at all."""
    _report_nothing(mock_device)
    _enable(hass, mock_config_entry, entity_id, attribute)
    await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id) is None


@pytest.mark.parametrize(FLAG_ARGS, FLAGS)
async def test_disabled_by_default_and_diagnostic(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    entity_id: str,
    attribute: str,
) -> None:
    """The entity is registered but disabled, in the diagnostic category."""
    _report_nothing(mock_device)
    setattr(mock_device, attribute, 1)
    await _setup(hass, mock_config_entry)

    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC
    assert hass.states.get(entity_id) is None


def test_flag_state_of_an_unreported_flag_is_unknown() -> None:
    """None stays unknown rather than off."""
    assert _flag_state(None) is None


def test_description_exists_fn_overrides_the_value_check() -> None:
    """A description can add its own condition on top of the reported value."""
    device = MagicMock()
    present = AquareaFlagBinarySensorDescription(key="a", value_fn=lambda _d: 1)
    gated = AquareaFlagBinarySensorDescription(
        key="b", value_fn=lambda _d: 1, exists_fn=lambda _d: False
    )
    assert present.exists(device) is True
    assert gated.exists(device) is False
