"""The lifetime and today sensors, the history backfill and the rebuild button.

The Panasonic cloud is a stand-in: MONTH and DAY queries answer from the
dicts below, YEAR queries (the backfill) from `Cloud` of test_lifetime.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aioaquarea
from aioaquarea.decorators import auth_required
from aioaquarea.statistics import Consumption, ConsumptionReport, DateType
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.aquarea import coordinator as coordinator_module, lifetime
from custom_components.aquarea.const import CONF_CONSUMPTION_INTERVAL, DOMAIN
from custom_components.aquarea.diagnostics import async_get_config_entry_diagnostics
from custom_components.aquarea.lifetime import add_months

from .conftest import DEVICE_ID
from .test_lifetime import Cloud, monthly

BUDAPEST = ZoneInfo("Europe/Budapest")
TOTAL = "sensor.heat_pump_lifetime_consumption"
HEATING = "sensor.heat_pump_heating_lifetime_consumption"
TODAY = "sensor.heat_pump_consumption_today"
BUTTON = "button.heat_pump_rebuild_lifetime_consumption"
REBUILD = (Platform.BUTTON, "rebuild_lifetime_consumption", BUTTON.split(".")[1])

# 26 months from 2024-08 to 2026-09: 2600 kWh heating, 260 tank.
HISTORY = monthly("202408", "202609")
BASE_HEAT = 2600.0
BASE_TOTAL = 2860.0

type Days = dict[str, dict[str, float]]


class World:
    """What the stand-in cloud answers to MONTH, DAY and YEAR queries."""

    def __init__(self, history: dict | None = None, register: str = "20240801") -> None:
        """Initialize the stand-in."""
        self.months: Days = {}  # {"202610": {"20261019": kWh}}
        self.hours: dict[str, dict[int, float]] = {}  # {"20261108": {hour: kWh}}
        self.year = Cloud(history if history is not None else HISTORY, register)
        self.in_flight = 0
        self.max_in_flight = 0
        self.hold: asyncio.Event | None = None
        self.hold_after: int | None = None

    def get_device_consumption(
        self, long_id: str, date_type: DateType, date_str: str
    ) -> list[Consumption]:
        """Answer a MONTH or DAY query."""
        if date_type == DateType.MONTH:
            # Like the real cloud: a bucket for every day of the month.
            days = self.months.get(date_str[:6], {})
            first = date(int(date_str[:4]), int(date_str[4:6]), 1)
            return [
                Consumption(
                    {
                        "dataTime": f"{date_str[:6]}{n:02d}",
                        "heatConsumption": days.get(f"{date_str[:6]}{n:02d}", 0.0),
                    }
                )
                for n in range(1, (add_months(first, 1) - first).days + 1)
            ]
        if date_type == DateType.DAY:
            hours = self.hours.get(date_str)
            if hours is None:
                return []
            return [
                Consumption(
                    {
                        "dataTime": f"{date_str} {hour:02d}",
                        "heatConsumption": hours.get(hour, 0.0),
                        "outdoorTemp": 5.0,
                    }
                )
                for hour in range(24)
            ]
        return []

    async def get_device_consumption_report(
        self, long_id: str, date_type: DateType, date_str: str, **kwargs: object
    ) -> ConsumptionReport | None:
        """Answer a YEAR query."""
        assert date_type == DateType.YEAR
        end = date(int(date_str[:4]), int(date_str[4:6]), 1)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if self.hold is not None and (
                self.hold_after is None or len(self.year.calls) >= self.hold_after
            ):
                await self.hold.wait()
            return await self.year.fetch(end)
        finally:
            self.in_flight -= 1


@pytest.fixture
def world(mock_aquarea_client: AsyncMock) -> World:
    """Return the stand-in cloud, wired to the mocked client."""
    world = World()
    mock_aquarea_client.get_device_consumption.side_effect = (
        world.get_device_consumption
    )
    mock_aquarea_client.get_device_consumption_report.side_effect = (
        world.get_device_consumption_report
    )
    return world


def store_key(entry: MockConfigEntry) -> str:
    """Return the storage key of an entry's lifetime state."""
    return f"aquarea.lifetime.{entry.entry_id}"


async def setup_entry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    when: datetime,
    *,
    zone: str = "Europe/Budapest",
    enable: tuple[str, ...] = (),
    wait: bool = True,
) -> None:
    """Set up the entry at a moment, in a time zone."""
    freezer.move_to(when)
    await hass.config.async_set_time_zone(zone)
    registry = er.async_get(hass)
    for platform, unique_suffix, object_id in enable:  # type: ignore[misc]
        registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{DEVICE_ID}_{unique_suffix}",
            suggested_object_id=object_id,
            disabled_by=None,
        )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=wait)


async def at(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    """Move the clock and let the coordinator poll."""
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def value(hass: HomeAssistant, entity_id: str) -> float | str:
    """Return the state of an entity as a number (or its text)."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return (
        float(state.state)
        if state.state not in ("unknown", "unavailable")
        else state.state
    )


def coordinator(entry: MockConfigEntry) -> Callable[[], object]:
    """Return the coordinator of the mocked device."""
    return entry.runtime_data[DEVICE_ID]


NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


# --- first start, restart, reload -------------------------------------------


async def test_setup_is_not_blocked_and_the_value_is_unknown_until_the_history_is_read(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Setup is not blocked and the value is unknown until the history is read."""
    world.months["202610"] = {"20261019": 2.0, "20261020": 1.0}
    world.hold = asyncio.Event()
    await setup_entry(hass, mock_config_entry, freezer, NOW, wait=False)

    # The setup is done while the history is still being read.
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert coordinator(mock_config_entry).lifetime_sync_running
    assert value(hass, TOTAL) == "unknown"
    assert hass.states.get(TOTAL).attributes["history_complete"] is False

    world.hold.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert world.year.calls == ["202609", "202409"]
    assert value(hass, HEATING) == BASE_HEAT + 3.0
    assert value(hass, TOTAL) == BASE_TOTAL + 3.0
    assert hass.states.get(TOTAL).attributes["history_complete"] is True


async def test_the_history_is_not_read_again_after_a_restart_reload_or_options_change(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The history is not read again after a restart reload or options change."""
    world.months["202610"] = {"20261020": 1.0}
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert world.year.calls == ["202609", "202409"]
    assert value(hass, HEATING) == BASE_HEAT + 1.0

    # Reload.
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # An options change reloads the entry too.
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CONSUMPTION_INTERVAL: 30}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    # A restart: unload, and set up again from the stored file.
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    stored = hass_storage[store_key(mock_config_entry)]["data"]["devices"][DEVICE_ID]
    assert stored["complete"] is True
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert world.year.calls == ["202609", "202409"]  # not one request more
    assert value(hass, HEATING) == BASE_HEAT + 1.0
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_a_failed_refresh_does_not_read_the_history_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A failed refresh does not read the history again."""
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    calls = list(world.year.calls)
    mock_aquarea_client.get_device.side_effect = aioaquarea.RequestFailedError("down")
    await at(hass, freezer, NOW + timedelta(minutes=1))
    assert not coordinator(mock_config_entry).last_update_success
    mock_aquarea_client.get_device.side_effect = None
    await at(hass, freezer, NOW + timedelta(minutes=2))
    assert world.year.calls == calls
    assert value(hass, HEATING) == BASE_HEAT


async def test_corrupt_stored_data_is_read_again_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt stored data is read again once."""
    hass_storage[store_key(mock_config_entry)] = {
        "version": 1,
        "minor_version": 1,
        "key": store_key(mock_config_entry),
        "data": {"devices": {DEVICE_ID: {"complete": True, "months": "garbage"}}},
    }
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert world.year.calls == ["202609", "202409"]
    assert value(hass, HEATING) == BASE_HEAT
    assert "Invalid stored lifetime consumption" in caplog.text
    # And it is healthy afterwards.
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert world.year.calls == ["202609", "202409"]


async def test_missing_stored_data_is_read_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Missing stored data is read again."""
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    del hass_storage[store_key(mock_config_entry)]
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert world.year.calls == ["202609", "202409"] * 2


async def test_removing_the_entry_deletes_the_stored_history(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Removing the entry deletes the stored history."""
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    assert store_key(mock_config_entry) in hass_storage  # kept on reload
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(mock_config_entry) not in hass_storage


async def test_an_unreadable_storage_file_is_read_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable storage file is read again."""
    hass_storage[store_key(mock_config_entry)] = {
        "version": 1,
        "minor_version": 1,
        "key": store_key(mock_config_entry),
        "data": ["not", "a", "dict"],
    }
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert "Invalid lifetime consumption storage" in caplog.text
    assert world.year.calls == ["202609", "202409"]


# --- failures ------------------------------------------------------------------


async def test_a_403_in_the_middle_aborts_and_retries_later(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 403 in the middle aborts and retries later."""
    blocked = aioaquarea.AuthenticationError(
        aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
        "Request rejected with status 403",
    )
    world.year.script = ["first window ok", blocked]
    await setup_entry(hass, mock_config_entry, freezer, NOW)

    assert world.year.calls == ["202609", "202409"]
    tracker = coordinator(mock_config_entry).lifetime
    assert not tracker.complete
    assert tracker.retry_at == NOW + timedelta(hours=6)
    assert value(hass, TOTAL) == "unknown"
    # The polling is not affected: no reauth, no repair, entities available.
    assert not hass.config_entries.flow.async_progress()
    assert coordinator(mock_config_entry).last_update_success
    assert value(hass, "sensor.heat_pump_heating_accumulated_consumption") == 0.0

    # Nothing is sent for 6 hours, however often the integration polls.
    await at(hass, freezer, NOW + timedelta(hours=3))
    await at(hass, freezer, NOW + timedelta(hours=5, minutes=59))
    assert world.year.calls == ["202609", "202409"]

    # Then it resumes at the window it stopped at.
    await at(hass, freezer, NOW + timedelta(hours=6, minutes=1))
    assert world.year.calls == ["202609", "202409", "202409"]
    assert tracker.complete
    assert tracker.state["failures"] == 0
    assert value(hass, HEATING) == BASE_HEAT


async def test_other_failures_retry_after_an_hour(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Other failures retry after an hour."""
    world.year.script = [ConnectionError("down")]
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    tracker = coordinator(mock_config_entry).lifetime
    assert tracker.retry_at == NOW + timedelta(hours=1)
    await at(hass, freezer, NOW + timedelta(hours=1, minutes=1))
    assert tracker.complete


@pytest.mark.parametrize(
    ("error", "blocks"),
    [
        (
            aioaquarea.AuthenticationError(
                aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
                "Request rejected with status 403",
            ),
            True,
        ),
        (aioaquarea.ApiError("HTTP_429", "Unexpected HTTP status 429"), True),
        (aioaquarea.ApiError("HTTP_503", "Unexpected HTTP status 503"), True),
        (
            aioaquarea.AuthenticationError(
                aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED, "Token expires"
            ),
            False,
        ),
        (
            aioaquarea.AuthenticationError(
                aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
                "Request rejected with status 401",
            ),
            False,
        ),
        (aioaquarea.AgreementNotAcceptedError(), False),
        (aioaquarea.ApiError("HTTP_404", "Unexpected HTTP status 404"), False),
        (aioaquarea.ApiError("some_code", "something else"), False),
        (ConnectionError("down"), False),
    ],
    ids=[
        "403",
        "429",
        "503",
        "token-expires",
        "401",
        "agreement-not-accepted",
        "404",
        "other-api-error",
        "network",
    ],
)
async def test_only_a_refusal_blocks_every_device(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_device_info: MagicMock,
    mock_config_entry: MockConfigEntry,
    error: BaseException,
    blocks: bool,
) -> None:
    """403, 429 and 5xx make every device wait; the rest only the device it hit."""
    second = MagicMock(spec=aioaquarea.DeviceInfo)
    second.device_id = "second-device-id"
    second.long_id = "second-long-id"
    second.name = "Second pump"
    second.model = "WH-TEST"
    second.firmware_version = "1.0.0"
    second.has_tank = True
    mock_aquarea_client.get_devices.return_value = [mock_device_info, second]
    world.year.script = [error]
    await setup_entry(hass, mock_config_entry, freezer, NOW)

    trackers = [coord.lifetime for coord in mock_config_entry.runtime_data.values()]
    failed = [t for t in trackers if t.state["failures"]]
    assert len(failed) >= 1
    blocked_until = hass.data[DOMAIN].get(coordinator_module.LIFETIME_BLOCKED_UNTIL)
    if blocks:
        assert blocked_until == NOW + timedelta(hours=6)
        # Nothing else was sent: the other device waited out the block.
        assert world.year.calls == ["202609"]
        assert all(not t.complete for t in trackers)
        assert all(t.retry_at == NOW + timedelta(hours=6) for t in trackers)
    else:
        assert blocked_until is None
        # Only the device that was refused waits, for an hour; the other one
        # read its whole history.
        assert len(failed) == 1
        assert failed[0].retry_at == NOW + timedelta(hours=1)
        assert [t for t in trackers if t.complete]
    # The poll keeps working either way: no reauth flow, no repair.
    assert not hass.config_entries.flow.async_progress()


async def test_the_devices_never_read_the_history_at_the_same_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_device_info: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The devices never read the history at the same time."""
    second = MagicMock(spec=aioaquarea.DeviceInfo)
    second.device_id = "second-device-id"
    second.long_id = "second-long-id"
    second.name = "Second pump"
    second.model = "WH-TEST"
    second.firmware_version = "1.0.0"
    second.has_tank = True
    mock_aquarea_client.get_devices.return_value = [mock_device_info, second]
    await setup_entry(hass, mock_config_entry, freezer, NOW)

    assert world.max_in_flight == 1
    assert len(world.year.calls) == 4
    for tracker in (c.lifetime for c in mock_config_entry.runtime_data.values()):
        assert tracker.complete


async def test_unloading_during_the_backfill_stops_it_and_it_resumes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Unloading during the backfill stops it and it resumes."""
    world.hold = asyncio.Event()
    world.hold_after = 1  # the second window waits forever
    await setup_entry(hass, mock_config_entry, freezer, NOW, wait=False)
    await hass.async_block_till_done()
    assert world.year.calls == ["202609"]
    tracker = coordinator(mock_config_entry).lifetime
    while not (tracker.state["windows"] == 1 and world.in_flight == 1):
        await asyncio.sleep(0)
    task = coordinator(mock_config_entry)._lifetime_task  # noqa: SLF001
    assert task is not None and not task.done()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    # Stopped before the state was written, and the state has the progress.
    assert task.done()
    stored = hass_storage[store_key(mock_config_entry)]["data"]["devices"][DEVICE_ID]
    assert stored["complete"] is False
    assert stored["cursor"] == "202409"
    assert stored["windows"] == 1
    assert len(stored["months"]) == 24

    world.hold = None
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # The first window was not asked for again.
    assert world.year.calls == ["202609", "202409"]
    assert value(hass, HEATING) == BASE_HEAT


# --- growing with every refresh -------------------------------------------------


async def test_new_energy_is_added_at_every_refresh_without_double_counting(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """New energy is added at every refresh without double counting."""
    world.months["202610"] = {"20261019": 2.0, "20261020": 1.0}
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    readings = [value(hass, HEATING)]
    assert readings[-1] == BASE_HEAT + 3.0

    # A day revised upwards between two refreshes: replaced, not added.
    world.months["202610"]["20261020"] = 1.5
    await at(hass, freezer, NOW + timedelta(minutes=16))
    readings.append(value(hass, HEATING))
    assert readings[-1] == BASE_HEAT + 3.5

    # A refresh with no new data.
    await at(hass, freezer, NOW + timedelta(minutes=32))
    readings.append(value(hass, HEATING))
    assert readings[-1] == BASE_HEAT + 3.5

    # Late in the cloud's day, then the day that crosses UTC midnight: the
    # old day is revised once more and the new one starts.
    world.months["202610"]["20261020"] = 2.0
    await at(hass, freezer, datetime(2026, 10, 20, 23, 50, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == BASE_HEAT + 4.0

    world.months["202610"]["20261020"] = 2.1
    world.months["202610"]["20261021"] = 0.2
    await at(hass, freezer, datetime(2026, 10, 21, 0, 5, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 4.3)

    # A partial list (fewer days than before): the value does not go back.
    world.months["202610"] = {"20261021": 0.2}
    await at(hass, freezer, datetime(2026, 10, 21, 0, 25, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 4.3)
    world.months["202610"] = {"20261019": 2.0, "20261020": 2.1, "20261021": 0.5}
    await at(hass, freezer, datetime(2026, 10, 21, 0, 45, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 4.6)

    assert readings == sorted(readings)
    assert world.year.calls == ["202609", "202409"]


async def test_the_month_end_loses_and_doubles_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The coordinator shows October until about 04:00 UTC on the 1st."""
    world.months["202610"] = {"20261030": 5.0, "20261031": 1.0}
    await setup_entry(
        hass, mock_config_entry, freezer, datetime(2026, 10, 31, 22, tzinfo=UTC)
    )
    readings = [value(hass, HEATING)]
    assert readings[-1] == BASE_HEAT + 6.0

    # After the cloud's midnight the hourly records already have the new
    # month's first hour while October is still shown: it is added.
    world.hours["20261101"] = {0: 0.25}
    world.months["202610"]["20261031"] = 1.2
    await at(hass, freezer, datetime(2026, 11, 1, 0, 40, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 6.2 + 0.25)

    # The final fetch of October, then November is shown with its own
    # figures, which include that hour: nothing is counted twice.
    world.months["202610"]["20261031"] = 1.3
    await at(hass, freezer, datetime(2026, 11, 1, 3, 5, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 6.3 + 0.25)
    world.months["202611"] = {"20261101": 0.3}
    await at(hass, freezer, datetime(2026, 11, 1, 4, 10, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 6.3 + 0.3)
    world.months["202611"] = {"20261101": 0.3, "20261102": 0.4}
    await at(hass, freezer, datetime(2026, 11, 2, 12, tzinfo=UTC))
    readings.append(value(hass, HEATING))
    assert readings[-1] == pytest.approx(BASE_HEAT + 6.3 + 0.7)
    assert readings == sorted(readings)

    tracker = coordinator(mock_config_entry).lifetime
    assert tracker.state["months"]["202610"]["heat"] == pytest.approx(6.3)
    assert tracker.state["open_month"] == "202611"

    # A restart in November neither loses nor repeats October.
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert value(hass, HEATING) == pytest.approx(BASE_HEAT + 7.0)


async def test_the_late_hours_of_a_finished_month_are_added_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The late hours of a finished month are added once."""
    world.months["202610"] = {"20261031": 1.0}
    await setup_entry(
        hass, mock_config_entry, freezer, datetime(2026, 10, 31, 22, tzinfo=UTC)
    )
    await at(hass, freezer, datetime(2026, 11, 1, 3, 5, tzinfo=UTC))
    world.months["202611"] = {"20261101": 0.3}
    await at(hass, freezer, datetime(2026, 11, 1, 4, 10, tzinfo=UTC))
    assert value(hass, HEATING) == pytest.approx(BASE_HEAT + 1.3)

    # The cloud published more for October's last day after the final fetch.
    # Looked up 24 hours after the month ended, from the YEAR window.
    world.year.months["202610"] = (1.4, 0.0, 0.0)
    calls = len(world.year.calls)
    await at(hass, freezer, datetime(2026, 11, 1, 23, tzinfo=UTC))
    assert len(world.year.calls) == calls
    assert value(hass, HEATING) == pytest.approx(BASE_HEAT + 1.3)
    await at(hass, freezer, datetime(2026, 11, 2, 1, tzinfo=UTC))
    assert len(world.year.calls) == calls + 1
    assert value(hass, HEATING) == pytest.approx(BASE_HEAT + 1.4 + 0.3)
    # Once only.
    await at(hass, freezer, datetime(2026, 11, 3, 1, tzinfo=UTC))
    assert len(world.year.calls) == calls + 1


# --- defaults, rebuild, restore -----------------------------------------------------


async def test_default_entities(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Default entities."""
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    registry = er.async_get(hass)

    def entry(platform: str, suffix: str) -> er.RegistryEntry:
        """Entry."""
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{DEVICE_ID}_{suffix}"
        )
        assert entity_id is not None, suffix
        return registry.async_get(entity_id)  # type: ignore[return-value]

    # Lifetime mirrors the accumulated sensors: only cooling is disabled.
    for suffix, disabled in (
        ("heating_lifetime_energy_consumption", False),
        ("tank_lifetime_energy_consumption", False),
        ("lifetime_energy_consumption", False),
        ("cooling_lifetime_energy_consumption", True),
        ("heating_accumulated_energy_consumption", False),
        ("cooling_accumulated_energy_consumption", True),
        # Today: only the total.
        ("today_energy_consumption", False),
        ("heating_today_energy_consumption", True),
        ("tank_today_energy_consumption", True),
        ("cooling_today_energy_consumption", True),
    ):
        assert (entry(Platform.SENSOR, suffix).disabled_by is not None) is disabled, (
            suffix
        )

    rebuild = entry(Platform.BUTTON, "rebuild_lifetime_consumption")
    assert rebuild.disabled_by is not None
    assert rebuild.entity_category is er.EntityCategory.CONFIG
    # Same unique ids as before for the existing sensors.
    assert entry(Platform.SENSOR, "accumulated_energy_consumption")

    state = hass.states.get(TOTAL)
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert set(state.attributes) >= {"history_complete"}
    assert "history_retry_at" not in state.attributes


async def test_rebuild_keeps_the_value_and_reads_the_history_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Rebuild keeps the value and reads the history again."""
    world.months["202610"] = {"20261020": 1.0}
    await setup_entry(
        hass,
        mock_config_entry,
        freezer,
        NOW,
        enable=(REBUILD,),  # type: ignore[arg-type]
    )
    published = value(hass, HEATING)
    assert published == BASE_HEAT + 1.0

    # The cloud now has older history too: the figure rises once.
    world.year.months = monthly("202308", "202609")
    readings = [published]
    world.hold = asyncio.Event()
    await hass.services.async_call(
        "button", "press", {"entity_id": BUTTON}, blocking=True
    )
    await hass.async_block_till_done()
    # While it runs the last value stays (not unknown, not a partial one).
    readings.append(value(hass, HEATING))
    # A second press while it runs is refused.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "button", "press", {"entity_id": BUTTON}, blocking=True
        )
    world.hold.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    readings.append(value(hass, HEATING))

    assert readings == [published, published, BASE_HEAT + 1200.0 + 1.0]
    assert world.year.calls[2:] == ["202609", "202409", "202209"]


async def test_rebuild_during_a_back_off_sends_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Rebuild during a back off sends nothing."""
    world.year.script = [
        aioaquarea.AuthenticationError(
            aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
            "Request rejected with status 403",
        )
    ]
    await setup_entry(
        hass,
        mock_config_entry,
        freezer,
        NOW,
        enable=(REBUILD,),  # type: ignore[arg-type]
    )
    assert world.year.calls == ["202609"]
    await hass.services.async_call(
        "button", "press", {"entity_id": BUTTON}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert world.year.calls == ["202609"]
    tracker = coordinator(mock_config_entry).lifetime
    assert tracker.retry_at == NOW + timedelta(hours=6)
    await at(hass, freezer, NOW + timedelta(hours=6, minutes=1))
    assert tracker.complete


async def test_the_restored_value_is_a_floor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If the stored months were lost, the value never goes below what was shown."""
    mock_restore_cache(hass, [State(HEATING, "9999.5")])
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert value(hass, HEATING) == 9999.5
    world.months["202610"] = {"20261020": 1.0}
    await at(hass, freezer, NOW + timedelta(minutes=16))
    assert value(hass, HEATING) == 9999.5


async def test_the_restored_value_does_not_make_an_unknown_value_known(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The restored value does not make an unknown value known."""
    mock_restore_cache(hass, [State(HEATING, "unknown"), State(TOTAL, "oops")])
    world.hold = asyncio.Event()
    await setup_entry(hass, mock_config_entry, freezer, NOW, wait=False)
    assert value(hass, HEATING) == "unknown"
    assert value(hass, TOTAL) == "unknown"
    world.hold.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert value(hass, HEATING) == BASE_HEAT


async def test_diagnostics_have_counts_and_no_energy_figures(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Diagnostics have counts and no energy figures."""

    world.months["202610"] = {"20261020": 12.3456}
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    block = result["devices"][0]["lifetime_consumption"]
    assert block["history_complete"] is True
    assert block["months_with_energy"] == 26
    assert block["earliest_month"] == "2024-08"
    assert block["open_month"] == "202610"
    assert block["history_windows_read"] == 2
    assert "12.3456" not in repr(result)
    assert str(BASE_HEAT) not in repr(block)


# --- today -------------------------------------------------------------------------


async def test_the_today_sensor_follows_the_local_day(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Budapest is UTC+1 on 2026-11-08: its day starts at 23:00 UTC on the 7th."""
    world.hours["20261106"] = {}
    world.hours["20261107"] = {20: 1.0, 22: 0.5, 23: 2.0}  # 23Z is local Nov 8, 00:00
    world.hours["20261108"] = {0: 1.0, 1: 1.0, 9: 0.5}
    # 22Z is local 23:00 on the 7th: not today.
    await setup_entry(
        hass, mock_config_entry, freezer, datetime(2026, 11, 8, 10, 5, tzinfo=UTC)
    )
    assert value(hass, TODAY) == 4.5
    state = hass.states.get(TODAY)
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["last_reset"].startswith("2026-11-08T00:00:00+01:00")

    # A late hour for yesterday (local) does not leak into today.
    world.hours["20261107"][22] = 0.9
    await at(hass, freezer, datetime(2026, 11, 8, 10, 25, tzinfo=UTC))
    assert value(hass, TODAY) == 4.5
    # A revised hour replaces its value.
    world.hours["20261108"][9] = 0.75
    await at(hass, freezer, datetime(2026, 11, 8, 10, 45, tzinfo=UTC))
    assert value(hass, TODAY) == 4.75
    assert value(hass, TODAY) == 4.75

    # Local midnight (23:00 UTC). The day's last hour (22:00 UTC, local 23:00)
    # is published at about 00:05 local, after midnight: the sensor still shows
    # the finished day, complete, with its own last_reset, until 03:00.
    world.hours["20261108"][22] = 0.6
    world.hours["20261108"][23] = 0.4  # local 00:00 of the 9th: the next day
    await at(hass, freezer, datetime(2026, 11, 8, 23, 20, tzinfo=UTC))
    assert value(hass, TODAY) == 5.35
    assert (
        hass.states.get(TODAY)
        .attributes["last_reset"]
        .startswith("2026-11-08T00:00:00+01:00")
    )
    await at(hass, freezer, datetime(2026, 11, 9, 1, 50, tzinfo=UTC))
    assert value(hass, TODAY) == 5.35

    # From 03:00 local the new day is shown: only its own hours, a new
    # last_reset.
    world.hours["20261109"] = {1: 0.25}
    await at(hass, freezer, datetime(2026, 11, 9, 2, 5, tzinfo=UTC))
    assert value(hass, TODAY) == 0.65
    assert (
        hass.states.get(TODAY)
        .attributes["last_reset"]
        .startswith("2026-11-09T00:00:00+01:00")
    )


async def test_the_today_value_survives_a_restart(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The today value survives a restart."""
    world.hours["20261108"] = {1: 1.0, 2: 2.0}
    await setup_entry(
        hass, mock_config_entry, freezer, datetime(2026, 11, 8, 10, 5, tzinfo=UTC)
    )
    assert value(hass, TODAY) == 3.0
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    # The cloud does not answer after the restart: the stored hours remain.
    mock_aquarea_client.get_device_consumption.side_effect = None
    mock_aquarea_client.get_device_consumption.return_value = []
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert value(hass, TODAY) == 3.0


async def test_the_today_total_is_unknown_without_any_hour(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The today total is unknown without any hour."""
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert value(hass, TODAY) == "unknown"


async def test_the_recorder_never_sees_the_sum_go_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A downward revision, a partial list and a rebuild leave the sum alone.

    The Energy dashboard's sum of a total_increasing sensor drops when the
    state does (the recorder reads it as a new meter); the sensor never goes
    below what it published, so the sum only ever grows by the real energy.
    """
    world.months["202610"] = {"20261020": 5.0}
    period = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    await setup_entry(hass, mock_config_entry, freezer, period, enable=(REBUILD,))  # type: ignore[arg-type]
    changes: dict[datetime, Callable[[], None]] = {
        datetime(2026, 10, 20, 10, 20, tzinfo=UTC): lambda: world.months.update(
            {"202610": {"20261020": 4.0}}
        ),
        datetime(2026, 10, 20, 10, 40, tzinfo=UTC): lambda: world.months.update(
            {"202610": {"20261020": 6.0}}
        ),
        datetime(2026, 10, 20, 11, 0, tzinfo=UTC): lambda: setattr(
            world.year, "months", monthly("202308", "202609")
        ),
    }
    while period < datetime(2026, 10, 20, 12, 0, tzinfo=UTC):
        if period in changes:
            changes[period]()
        if period == datetime(2026, 10, 20, 11, 0, tzinfo=UTC):
            await hass.services.async_call(
                "button", "press", {"entity_id": BUTTON}, blocking=True
            )
        await at(hass, freezer, period + timedelta(seconds=30))
        await async_wait_recording_done(hass)
        freezer.move_to(period + timedelta(minutes=5, seconds=5))
        do_adhoc_statistics(hass, start=period)
        await async_wait_recording_done(hass)
        period += timedelta(minutes=5)

    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 10, 20, 9, tzinfo=UTC),
        None,
        {HEATING},
        "5minute",
        None,
        {"sum", "state"},
    )
    sums = [row["sum"] for row in rows[HEATING]]
    states = [row["state"] for row in rows[HEATING]]
    assert sums == sorted(sums)
    assert states == sorted(states)
    # The rebuild found 12 more months of 100 kWh: that rise is the only jump.
    assert states[-1] == pytest.approx(BASE_HEAT + 1200.0 + 6.0)
    assert sums[-1] - sums[0] == pytest.approx(states[-1] - states[0])


async def test_the_library_is_asked_to_raise_on_error_when_it_can(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aioaquarea-ng releases with raise_on_error get it; older ones are not asked."""
    monkeypatch.setattr(coordinator_module, "LIBRARY_RAISES_ON_ERROR", False)
    monkeypatch.setattr(coordinator_module, "LIBRARY_RETRY_LOGIN", False)
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    kwargs = [
        c.kwargs
        for c in mock_aquarea_client.get_device_consumption_report.call_args_list
    ]
    assert kwargs == [{}, {}]

    monkeypatch.setattr(coordinator_module, "LIBRARY_RAISES_ON_ERROR", True)
    monkeypatch.setattr(coordinator_module, "LIBRARY_RETRY_LOGIN", True)
    mock_aquarea_client.get_device_consumption_report.reset_mock()
    coord = coordinator(mock_config_entry)
    await coord.async_rebuild_lifetime()
    await hass.async_block_till_done(wait_background_tasks=True)
    kwargs = [
        c.kwargs
        for c in mock_aquarea_client.get_device_consumption_report.call_args_list
    ]
    assert kwargs == [{"raise_on_error": True, "retry_login": False}] * 2


async def test_an_error_in_the_lifetime_update_does_not_fail_the_poll(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The totals are extra: a bug in them must not make the entities unavailable."""
    with patch.object(
        lifetime.LifetimeTracker, "observe_month", side_effect=RuntimeError("boom")
    ):
        await setup_entry(hass, mock_config_entry, freezer, NOW)
        await at(hass, freezer, NOW + timedelta(minutes=16))
    assert "Failed to update the lifetime consumption" in caplog.text
    assert coordinator(mock_config_entry).last_update_success
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_an_unreadable_storage_file_does_not_stop_the_setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A file Home Assistant cannot read counts as no stored data."""
    with patch(
        "custom_components.aquarea.lifetime.Store.async_load",
        side_effect=HomeAssistantError("corrupt"),
    ):
        await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert "Unreadable lifetime consumption storage" in caplog.text
    assert world.year.calls == ["202609", "202409"]


def last_reset(hass: HomeAssistant, entity_id: str) -> str:
    """Return the last_reset attribute of an entity."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.attributes["last_reset"]


async def test_the_last_hour_published_after_midnight_stays_in_the_finished_day(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The hour published after midnight is in the finished day's last state.

    This pins the states the sensor writes (value and last_reset); what the
    recorder does with a total sensor's last_reset is Home Assistant's own.
    """
    world.hours["20261108"] = {21: 1.0}
    await setup_entry(
        hass, mock_config_entry, freezer, datetime(2026, 11, 8, 22, 40, tzinfo=UTC)
    )
    states = [(value(hass, TODAY), last_reset(hass, TODAY))]
    assert states[-1][0] == 1.0
    # 22:00-23:00 UTC is the day's last hour; it is published at 23:05 UTC.
    world.hours["20261108"][22] = 0.5
    for when in (
        datetime(2026, 11, 8, 23, 6, tzinfo=UTC),
        datetime(2026, 11, 9, 0, 30, tzinfo=UTC),
        datetime(2026, 11, 9, 1, 50, tzinfo=UTC),
    ):
        await at(hass, freezer, when)
        states.append((value(hass, TODAY), last_reset(hass, TODAY)))
    assert [state[0] for state in states] == [1.0, 1.5, 1.5, 1.5]
    # One last_reset for all of them: the recorder never sees the new day
    # start before the finished one is complete.
    assert {state[1][:10] for state in states} == {"2026-11-08"}


async def test_a_long_consumption_interval_keeps_the_finished_day_longer(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Interval 6 h: the settle time is 8 h, not 3 h."""
    world.hours["20261108"] = {21: 1.0}
    await setup_entry(
        hass, mock_config_entry, freezer, datetime(2026, 11, 8, 22, 40, tzinfo=UTC)
    )
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CONSUMPTION_INTERVAL: 360}
    )
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator(mock_config_entry).consumption_interval == 360

    # 03:35 local: past the 3 h of the default interval, inside the 8 h.
    await at(hass, freezer, datetime(2026, 11, 9, 2, 35, tzinfo=UTC))
    assert last_reset(hass, TODAY).startswith("2026-11-08T00:00:00+01:00")
    # 08:05 local: the new day.
    await at(hass, freezer, datetime(2026, 11, 9, 7, 5, tzinfo=UTC))
    assert last_reset(hass, TODAY).startswith("2026-11-09T00:00:00+01:00")


@pytest.mark.parametrize(
    ("start", "before", "after", "reset_before", "reset_after"),
    [
        # Clocks go back 03:00 CEST -> 02:00 CET at 01:00 UTC; the day of 25
        # hours starts at 22:00 UTC and changes at 03:00 on the wall clock,
        # which is 02:00 UTC.
        (
            datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 50, tzinfo=UTC),
            datetime(2026, 10, 25, 2, 10, tzinfo=UTC),
            "2026-10-24T00:00:00+02:00",
            "2026-10-25T00:00:00+02:00",
        ),
        # Clocks go forward 02:00 CET -> 03:00 CEST at 01:00 UTC; the day of
        # 23 hours starts at 23:00 UTC and changes at 03:00 on the wall clock,
        # which is 01:00 UTC (there is no 02:xx that day).
        (
            datetime(2026, 3, 28, 12, 0, tzinfo=UTC),
            datetime(2026, 3, 29, 0, 50, tzinfo=UTC),
            datetime(2026, 3, 29, 1, 10, tzinfo=UTC),
            "2026-03-28T00:00:00+01:00",
            "2026-03-29T00:00:00+01:00",
        ),
    ],
    ids=["fall-back", "spring-forward"],
)
async def test_the_day_switches_at_three_on_the_wall_clock_on_clock_change_days(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    start: datetime,
    before: datetime,
    after: datetime,
    reset_before: str,
    reset_after: str,
) -> None:
    """The switch is at 03:00 on the wall clock, whatever the length of the day."""
    await setup_entry(hass, mock_config_entry, freezer, start)
    for day in (start.date(), start.date() + timedelta(days=1)):
        world.hours[day.strftime("%Y%m%d")] = {1: 1.0, 5: 0.5, 12: 2.0}
    await at(hass, freezer, before)
    assert last_reset(hass, TODAY).startswith(reset_before)
    await at(hass, freezer, after)
    assert last_reset(hass, TODAY).startswith(reset_after)


# --- implausible data, locks, stopping ------------------------------------------------


async def test_a_glitch_in_the_history_does_not_make_the_stored_state_invalid(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The next start accepts what this one wrote: no history read again."""
    world.year.months["202501"] = (60_000.0, 0.0, 0.0)
    world.hours["20261020"] = {3: 65_535.0, 4: 0.5}
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert world.year.calls == ["202609", "202409"]
    assert value(hass, HEATING) == BASE_HEAT - 100.0

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert world.year.calls == ["202609", "202409"]
    assert value(hass, HEATING) == BASE_HEAT - 100.0


async def test_two_config_entries_never_read_the_history_at_the_same_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_device_info: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The lock is shared across entries, not just devices."""
    second = MagicMock(spec=aioaquarea.DeviceInfo)
    second.device_id = "second-device-id"
    second.long_id = "second-long-id"
    second.name = "Second pump"
    second.model = "WH-TEST"
    second.firmware_version = "1.0.0"
    second.has_tank = True
    mock_aquarea_client.get_devices.side_effect = [[mock_device_info], [second]]
    other = MockConfigEntry(
        domain=DOMAIN,
        title="other@example.com",
        unique_id="other@example.com",
        data={"username": "other@example.com", "password": "x"},
    )
    other.add_to_hass(hass)
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert other.state is ConfigEntryState.LOADED
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert world.max_in_flight == 1
    assert len(world.year.calls) == 4


class _RefusingClient:
    """A client with the library's real auth_required decorator, refused by a 403."""

    logger = logging.getLogger("refusing client")
    is_refresh_login_enabled = True
    is_logged = True

    def __init__(self) -> None:
        self.requests = 0
        self.logins = 0

    async def login(self) -> None:
        self.logins += 1
        raise aioaquarea.AuthenticationError(
            aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
            "Request rejected with status 403",
        )

    @auth_required
    async def get_device_consumption_report(
        self, long_id: str, date_type: DateType, date_str: str, **kwargs: object
    ) -> ConsumptionReport:
        self.requests += 1
        raise aioaquarea.AuthenticationError(
            aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
            "Request rejected with status 403",
        )


async def test_a_403_adds_no_request_and_the_other_device_waits_out_the_block(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_device_info: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Through the library's real decorator: one request is sent, in all.

    The decorator logs in again and repeats the request unless the call says
    retry_login=False (releases that have it); a refused login is no worse
    than one request plus one login attempt. The second device's request is
    refused inside the lock, before it is sent, by the block the first found.
    """
    refusing = _RefusingClient()
    mock_aquarea_client.get_device_consumption_report = (
        refusing.get_device_consumption_report
    )
    second = MagicMock(spec=aioaquarea.DeviceInfo)
    second.device_id = "second-device-id"
    second.long_id = "second-long-id"
    second.name = "Second pump"
    second.model = "WH-TEST"
    second.firmware_version = "1.0.0"
    second.has_tank = True
    mock_aquarea_client.get_devices.return_value = [mock_device_info, second]
    await setup_entry(hass, mock_config_entry, freezer, NOW)

    assert refusing.requests == 1
    assert refusing.logins == (0 if coordinator_module.LIBRARY_RETRY_LOGIN else 1)
    for coord in mock_config_entry.runtime_data.values():
        assert not coord.lifetime.complete
        assert coord.lifetime.retry_at >= NOW + timedelta(hours=6)
    # The poll keeps working, and nothing is sent while the block lasts.
    await at(hass, freezer, NOW + timedelta(hours=3))
    await at(hass, freezer, NOW + timedelta(hours=5, minutes=59))
    assert refusing.requests == 1
    assert not hass.config_entries.flow.async_progress()


async def test_a_device_does_not_send_while_the_login_is_refused_or_missing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Without a login the request is not sent: the poll logs in first."""
    mock_aquarea_client.is_logged = False
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    assert world.year.calls == []
    tracker = coordinator(mock_config_entry).lifetime
    assert tracker.state["failures"] == 1
    assert tracker.retry_at == NOW + timedelta(hours=1)  # not the 6 h of a block

    mock_aquarea_client.is_logged = True
    await at(hass, freezer, NOW + timedelta(hours=1, minutes=1))
    assert tracker.complete


async def test_a_refresh_during_the_unload_cannot_start_or_save_anything(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """After the stop, no new read and no save: no orphan file after a delete."""
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    coord = coordinator(mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    calls = list(world.year.calls)

    # A refresh that was already running reaches the end of its update.
    world.months["202610"] = {"20261020": 77.0}
    world.hours["20261020"] = {9: 1.0}
    coord.lifetime.state["complete"] = False  # something that wants a sync
    await coord._async_update_lifetime(NOW)  # noqa: SLF001
    coord._maybe_start_lifetime_sync(NOW + timedelta(hours=9))  # noqa: SLF001
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coord.lifetime_sync_running
    assert world.year.calls == calls

    # Deleting the entry removes the file, and nothing writes it again.
    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, NOW + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert store_key(mock_config_entry) not in hass_storage


async def test_a_failed_unload_lets_the_lifetime_sync_carry_on(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """If the platforms do not unload the entry stays loaded and keeps syncing."""
    world.year.script = [ConnectionError("down")]
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    coord = coordinator(mock_config_entry)
    assert not coord.lifetime.complete

    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        assert not await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.FAILED_UNLOAD
    assert not coord._lifetime_stopped  # noqa: SLF001

    # The history is read after the back-off, as if nothing had happened.
    await at(hass, freezer, NOW + timedelta(hours=1, minutes=1))
    assert coord.lifetime.complete
    await coord.async_shutdown()


async def test_the_start_gate_waits_for_the_login_back_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No history read is started while the poll's login is backing off."""
    world.year.script = [ConnectionError("down")]
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    coord = coordinator(mock_config_entry)
    tracker = coord.lifetime
    assert world.year.calls == ["202609"]

    later = NOW + timedelta(hours=2)
    freezer.move_to(later)
    coord._login_retry_at = later + timedelta(hours=1)  # noqa: SLF001
    coord._maybe_start_lifetime_sync(later)  # noqa: SLF001
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not coord.lifetime_sync_running
    assert world.year.calls == ["202609"]

    coord._login_retry_at = None  # noqa: SLF001
    coord._maybe_start_lifetime_sync(later)  # noqa: SLF001
    await hass.async_block_till_done(wait_background_tasks=True)
    assert tracker.complete


async def test_the_fetch_gate_stops_a_run_when_the_login_starts_backing_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    world: World,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A run that was started refuses its request when the login backs off."""
    world.year.script = [ConnectionError("down")]
    await setup_entry(hass, mock_config_entry, freezer, NOW)
    coord = coordinator(mock_config_entry)
    tracker = coord.lifetime
    assert tracker.state["failures"] == 1

    later = NOW + timedelta(hours=2)
    freezer.move_to(later)
    coord._login_retry_at = later + timedelta(hours=1)  # noqa: SLF001
    await coord._async_lifetime_sync()  # noqa: SLF001
    # Not a request in sight, and a failure of the milder kind: one hour,
    # then two, not a block of all devices.
    assert world.year.calls == ["202609"]
    assert tracker.state["failures"] == 2
    assert tracker.retry_at == later + timedelta(hours=2)
    assert hass.data[DOMAIN].get(coordinator_module.LIFETIME_BLOCKED_UNTIL) is None
