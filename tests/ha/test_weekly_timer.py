"""The read-only weekly schedule binary sensor and the coordinator's read of it.

The schedule is only read while the entity is enabled, at most hourly, and
whatever goes wrong with that read never affects the other entities.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any
from unittest.mock import AsyncMock

import aioaquarea
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID

ENTITY_ID = "binary_sensor.heat_pump_weekly_schedule"
OTHER_ENTITIES = ("climate.heat_pump_house", "switch.heat_pump_force_dhw")
COORDINATOR_LOGGER = "custom_components.aquarea.coordinator"

_SLOT = {
    "startTime": "16:45",
    "mode": 3,
    "zone1OnOff": 1,
    "zone1Heat": 31,
    "zone1Cool": 18,
    "tankOnOff": 1,
    "tankTemperature": 45,
}


def _timer(operation: int) -> aioaquarea.WeeklyTimer:
    """A weekly schedule with two slots on Monday and one on Wednesday."""
    empty = [{}, {}, {}, {}]
    days: list[dict[str, Any]] = [
        {"patternList": [_SLOT, {**_SLOT, "startTime": "19:00"}, *empty[:4]]},
        {"patternList": [{}] * 6},
        {"patternList": [_SLOT, *[{}] * 5]},
        {"patternList": [{}] * 6},
        {"patternList": [{}] * 6},
        {"patternList": [{}] * 6},
        {"patternList": [{}] * 6},
    ]
    return aioaquarea.WeeklyTimer.from_dict(
        {
            "weeklyTimer": {"operation": operation, "timer": days},
            "status": {
                "zoneStatus": [
                    {
                        "zoneId": 1,
                        "heatMin": 20,
                        "heatMax": 55,
                        "coolMin": 5,
                        "coolMax": 20,
                    }
                ],
                "tankStatus": {"heatMin": 40, "heatMax": 75},
            },
        }
    )


def _enable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DEVICE_ID}_weekly_timer",
        config_entry=entry,
        suggested_object_id=ENTITY_ID.split(".", 1)[1],
        disabled_by=None,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _warnings(caplog: Any) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == COORDINATOR_LOGGER
        and r.levelno == logging.WARNING
        and "weekly schedule" in r.getMessage()
    ]


async def test_on_with_attributes(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An enabled schedule shows on, with its days and number of changes."""
    mock_aquarea_client.get_weekly_timer.return_value = _timer(1)
    _enable(hass, mock_config_entry)
    await _setup(hass, mock_config_entry)
    # Nothing was read before the entity subscribed.
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE

    await _advance(hass, freezer, timedelta(seconds=61))

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_ON
    assert state.attributes["scheduled_days"] == ["Monday", "Wednesday"]
    assert state.attributes["slot_count"] == 3
    mock_aquarea_client.get_weekly_timer.assert_awaited_once()


async def test_off(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A switched-off schedule shows off."""
    mock_aquarea_client.get_weekly_timer.return_value = _timer(0)
    _enable(hass, mock_config_entry)
    await _setup(hass, mock_config_entry)
    await _advance(hass, freezer, timedelta(seconds=61))

    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_read_at_most_hourly(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The schedule is not read on every poll, but again after an hour."""
    mock_aquarea_client.get_weekly_timer.return_value = _timer(1)
    _enable(hass, mock_config_entry)
    await _setup(hass, mock_config_entry)
    await _advance(hass, freezer, timedelta(seconds=61))
    assert mock_aquarea_client.get_weekly_timer.await_count == 1

    await _advance(hass, freezer, timedelta(seconds=61))
    assert mock_aquarea_client.get_weekly_timer.await_count == 1

    mock_aquarea_client.get_weekly_timer.return_value = _timer(0)
    await _advance(hass, freezer, timedelta(hours=1))
    assert mock_aquarea_client.get_weekly_timer.await_count == 2
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


@pytest.mark.parametrize(
    "error",
    [
        aioaquarea.ApiError("1234", "boom"),
        aioaquarea.AuthenticationError(
            aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS, "bad"
        ),
    ],
    ids=["api_error", "invalid_credentials"],
)
async def test_failed_read_does_not_affect_other_entities(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """A failing read leaves the schedule unavailable and nothing else."""
    mock_aquarea_client.get_weekly_timer.side_effect = error
    _enable(hass, mock_config_entry)
    await _setup(hass, mock_config_entry)
    await _advance(hass, freezer, timedelta(seconds=61))

    mock_aquarea_client.get_weekly_timer.assert_awaited_once()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    for entity_id in OTHER_ENTITIES:
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(_warnings(caplog)) == 1


async def test_failures_warn_once_then_recover(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first failure warns, later ones do not, and success resets that."""
    get = mock_aquarea_client.get_weekly_timer
    get.side_effect = aioaquarea.ApiError("1234", "boom")
    _enable(hass, mock_config_entry)
    await _setup(hass, mock_config_entry)
    await _advance(hass, freezer, timedelta(seconds=61))
    await _advance(hass, freezer, timedelta(hours=1))
    assert get.await_count == 2
    assert len(_warnings(caplog)) == 1

    get.side_effect = None
    get.return_value = _timer(1)
    await _advance(hass, freezer, timedelta(hours=1))
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    get.side_effect = aioaquarea.ApiError("1234", "boom again")
    await _advance(hass, freezer, timedelta(hours=1))
    assert len(_warnings(caplog)) == 2
    # The cached schedule is still shown while it is fresh enough.
    assert hass.states.get(ENTITY_ID).state == STATE_ON


async def test_unavailable_when_the_schedule_is_stale(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Three hours without a successful read make the sensor unavailable."""
    get = mock_aquarea_client.get_weekly_timer
    get.return_value = _timer(1)
    _enable(hass, mock_config_entry)
    await _setup(hass, mock_config_entry)
    await _advance(hass, freezer, timedelta(seconds=61))
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    get.side_effect = aioaquarea.ApiError("1234", "boom")
    await _advance(hass, freezer, timedelta(hours=1))
    await _advance(hass, freezer, timedelta(hours=1))
    assert hass.states.get(ENTITY_ID).state == STATE_ON
    await _advance(hass, freezer, timedelta(hours=1, minutes=1))
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def test_not_read_while_the_entity_is_disabled(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Nobody asked for the schedule, so the cloud is never asked for it."""
    await _setup(hass, mock_config_entry)
    await _advance(hass, freezer, timedelta(seconds=61))
    await _advance(hass, freezer, timedelta(hours=2))

    mock_aquarea_client.get_weekly_timer.assert_not_awaited()
    assert hass.states.get(ENTITY_ID) is None


async def test_unsubscribe_is_idempotent(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Calling the unsubscribe callback twice only counts once."""
    await _setup(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data[DEVICE_ID]

    unsubscribe = coordinator.subscribe_weekly_timer()
    other = coordinator.subscribe_weekly_timer()
    assert coordinator._weekly_timer_subscribers == 2  # noqa: SLF001
    unsubscribe()
    unsubscribe()
    assert coordinator._weekly_timer_subscribers == 1  # noqa: SLF001
    other()
    assert coordinator._weekly_timer_subscribers == 0  # noqa: SLF001
