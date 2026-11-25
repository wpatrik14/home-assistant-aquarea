"""The adapter's `uncontrollable` flag as a diagnostic binary sensor.

It is informational only: it never makes another entity unavailable, and only
a real True counts as on.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

BINARY_SENSOR_LOGGER = "custom_components.aquarea.binary_sensor"
ENTITY_ID = "binary_sensor.heat_pump_uncontrollable_adapter_flag"
OTHER_ENTITIES = ("climate.heat_pump_house", "switch.heat_pump_force_dhw")

from custom_components.aquarea.binary_sensor import _strict_flag_state  # noqa: E402


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("flag", "state"), [(True, STATE_ON), (False, STATE_OFF)], ids=["true", "false"]
)
async def test_state_follows_flag(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    flag: bool,
    state: str,
) -> None:
    """The sensor is on by default and shows the flag."""
    mock_device.uncontrollable = flag
    await _setup(hass, mock_config_entry)

    entity_state = hass.states.get(ENTITY_ID)
    assert entity_state is not None
    assert entity_state.state == state
    assert entity_state.attributes["raw_value"] is flag
    entry = er.async_get(hass).async_get(ENTITY_ID)
    assert entry is not None
    assert entry.disabled_by is None
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC


async def test_no_entity_when_not_reported(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """A device that does not report the flag gets no entity."""
    mock_device.uncontrollable = None
    await _setup(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID) is None


async def test_a_non_boolean_value_reads_as_off(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The mock device returns a MagicMock for an unset flag: that is not True."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == STATE_OFF


async def test_other_entities_stay_available_while_on(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Nothing is gated on the flag."""
    mock_device.uncontrollable = True
    await _setup(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == STATE_ON
    for entity_id in OTHER_ENTITIES:
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state != STATE_UNAVAILABLE


async def test_warns_once_per_transition_to_on(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warning is logged when the flag turns on, not on every poll."""

    async def poll() -> None:
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    def warnings() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == BINARY_SENSOR_LOGGER
            and r.levelno == logging.WARNING
            and "uncontrollable" in r.getMessage()
        ]

    mock_device.uncontrollable = False
    await _setup(hass, mock_config_entry)
    assert warnings() == []

    mock_device.uncontrollable = True
    await poll()
    await poll()
    assert len(warnings()) == 1

    mock_device.uncontrollable = False
    await poll()
    mock_device.uncontrollable = True
    await poll()
    assert len(warnings()) == 2


async def test_warns_when_already_on_at_startup(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A flag that is already set when the entry loads is reported too."""
    mock_device.uncontrollable = True
    await _setup(hass, mock_config_entry)

    assert "reports that the device is uncontrollable" in caplog.text


def test_strict_flag_state() -> None:
    """Only a real True is on; None is unknown."""
    assert _strict_flag_state(None) is None
    assert _strict_flag_state(True) is True
    assert _strict_flag_state(1) is False
