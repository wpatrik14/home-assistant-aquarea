"""Unit tests of the lifetime and today totals (lifetime.py), without entities.

The tracker is driven directly with a stand-in for the Panasonic cloud that
answers YEAR windows like the real one (12 months ending at the month asked,
plus the 12 before them), and a stand-in for the storage.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import json
from typing import Any
from zoneinfo import ZoneInfo

import aioaquarea
from aioaquarea.statistics import Consumption, ConsumptionReport
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest

from custom_components.aquarea import lifetime
from custom_components.aquarea.lifetime import (
    KINDS,
    LifetimeSyncError,
    LifetimeTracker,
    add_months,
    local_day_bounds,
    month_key,
    validate_state,
)

TOTAL = aioaquarea.ConsumptionType.TOTAL
HEAT = aioaquarea.ConsumptionType.HEAT
TANK = aioaquarea.ConsumptionType.WATER_TANK
OPEN = date(2026, 10, 1)
TODAY = date(2026, 10, 20)

BUDAPEST = ZoneInfo("Europe/Budapest")
NEW_YORK = ZoneInfo("America/New_York")
KOLKATA = ZoneInfo("Asia/Kolkata")


class FakeStore:
    """Stands in for LifetimeStore: counts the saves."""

    def __init__(self) -> None:
        """Initialize the stand-in."""
        self.saves = 0
        self.flushes = 0

    def schedule_save(self) -> None:
        """Count a delayed save."""
        self.saves += 1

    async def async_flush(self) -> None:
        """Count a write."""
        self.flushes += 1


class Cloud:
    """A cloud with energy per month that answers YEAR windows."""

    def __init__(
        self,
        months: dict[str, tuple[float, float, float]],
        register: str | None = "20240801",
        script: list[Any] | None = None,
        empty: bool = False,
    ) -> None:
        """Initialize the stand-in."""
        self.months = months
        self.register = register
        # Per call, in order: an exception to raise, None to answer, or
        # anything else (ignored) to answer normally.
        self.script = list(script or [])
        self.calls: list[str] = []
        self.empty = empty

    async def fetch(self, end: date) -> ConsumptionReport | None:
        """Answer a YEAR window like the cloud."""
        self.calls.append(month_key(end))
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            if step is None:
                return None

        if self.empty:
            # raise_on_error: "no data" is a report without buckets.
            return ConsumptionReport(history=[], device_register_time=self.register)

        def bucket(offset: int) -> Consumption:
            """Bucket."""
            key = month_key(add_months(end, -offset))
            heat, cool, tank = self.months.get(key, (0.0, 0.0, 0.0))
            return Consumption(
                {
                    "dataTime": key,
                    "heatConsumption": heat,
                    "coolConsumption": cool,
                    "tankConsumption": tank,
                    "outdoorTemp": 10.0,
                }
            )

        return ConsumptionReport(
            history=[bucket(offset) for offset in range(11, -1, -1)],
            last_year_history=[bucket(offset) for offset in range(23, 11, -1)],
            device_register_time=self.register,
        )


def monthly(first: str, last: str, heat: float = 100.0, tank: float = 10.0) -> dict:
    """Return the same energy for every month from ``first`` to ``last``."""
    months = {}
    month = lifetime.parse_month(first)
    while month_key(month) <= last:
        months[month_key(month)] = (heat, 0.0, tank)
        month = add_months(month, 1)
    return months


def totals(heat: float = 0.0, cool: float = 0.0, tank: float = 0.0) -> dict:
    """Return totals of the three kinds."""
    return {"heat": heat, "cool": cool, "tank": tank}


def new_tracker(store: FakeStore | None = None) -> LifetimeTracker:
    """Return a tracker with no history."""
    return LifetimeTracker(lifetime.fresh_state(), store or FakeStore())  # type: ignore[arg-type]


def started(heat: float = 0.0, tank: float = 0.0) -> LifetimeTracker:
    """A tracker that has seen October 2026 and not read the history."""
    tracker = new_tracker()
    tracker.observe_month(OPEN, totals(heat, tank=tank), TODAY)
    return tracker


async def sync(tracker: LifetimeTracker, cloud: Cloud) -> None:
    """Run one sync of a tracker against a cloud."""
    await tracker.async_sync(cloud.fetch, asyncio.Lock())


def restart(tracker: LifetimeTracker) -> LifetimeTracker:
    """Return a tracker built from the stored form of another one."""
    raw = json.loads(json.dumps(tracker.state))
    state = validate_state(raw)
    assert state is not None
    return LifetimeTracker(state, FakeStore())  # type: ignore[arg-type]


# --- validate_state --------------------------------------------------------


def test_afresh_state_is_valid() -> None:
    """Afresh state is valid."""
    assert validate_state(lifetime.fresh_state()) is not None


def test_a_state_with_history_round_trips() -> None:
    """A state with history round trips."""
    tracker = started(heat=3.0)
    state = json.loads(json.dumps(tracker.state))
    assert validate_state(state) == tracker.state


@pytest.mark.parametrize(
    "damage",
    [
        lambda s: s.update(months="x"),
        lambda s: s.update(complete="yes"),
        lambda s: s.update(windows=-1),
        lambda s: s.update(cursor="2026-10"),
        lambda s: s.update(retry_at="tomorrow"),
        lambda s: s["open"].update(heat=float("nan")),
        lambda s: s["open"].update(heat=-1.0),
        lambda s: s["open"].update(heat=60_000.0),
        lambda s: s["months"].update({"202609": totals(heat=60_000.0)}),
        lambda s: s["months"].update({"202609": {"heat": 1.0}}),
        # A finished month at or after the open month cannot happen.
        lambda s: s["months"].update({"202610": totals(heat=1.0)}),
        lambda s: s["pending"].update({"202611": "2026-12-01T00:00:00+00:00"}),
        # A started backfill without a cursor, a completed one without a month.
        lambda s: s.update(cursor=None),
        lambda s: s["hours"].update({"not a time": totals()}),
    ],
)
def test_damaged_state_is_rejected(damage: Any) -> None:
    """Damaged state is rejected."""
    tracker = started(heat=3.0)
    state = deepcopy(tracker.state)
    damage(state)
    assert validate_state(state) is None


def test_a_completed_backfill_needs_an_open_month() -> None:
    """A completed backfill needs an open month."""
    state = lifetime.fresh_state()
    state["complete"] = True
    assert validate_state(state) is None


def test_a_ledger_below_the_floor_is_not_corruption() -> None:
    """A ledger below the floor is not corruption."""
    tracker = started()
    tracker.state["complete"] = True
    tracker.state["floor"] = totals(heat=500.0)
    assert validate_state(deepcopy(tracker.state)) is not None
    assert tracker.ledger_below_floor
    # The published value stays at the floor.
    assert tracker.lifetime_value(HEAT) == 500.0


# --- backfill ---------------------------------------------------------------


async def test_the_backfill_stops_at_the_registration_month() -> None:
    """Registered 2024-08: the window of 2024-10..2026-09, then 2022-10..2024-09."""
    cloud = Cloud(monthly("202408", "202609"))
    tracker = started(heat=10.0, tank=5.0)
    assert tracker.lifetime_value(TOTAL) is None

    await sync(tracker, cloud)

    assert cloud.calls == ["202609", "202409"]
    assert tracker.complete
    assert tracker.state["ended_by"] == "register"
    assert tracker.state["register_month"] == "202408"
    assert tracker.months_included == 26
    assert tracker.earliest_month == date(2024, 8, 1)
    # 26 months of 100 heating and 10 tank, and the open month.
    assert tracker.lifetime_value(HEAT) == 2610.0
    assert tracker.lifetime_value(TANK) == 265.0
    assert tracker.lifetime_value(TOTAL) == 2875.0


async def test_the_backfill_goes_on_while_there_was_energy_before_the_registration() -> (
    None
):
    """A unit registered again: energy before the registration month is real."""
    months = monthly("202208", "202609")
    cloud = Cloud(months, register="20240801")
    tracker = started()
    await sync(tracker, cloud)
    # The 2024-09 window reaches 2024-08, but 2022-10..2024-07 held energy,
    # as did 2020-10..2022-09; the 2018-10..2020-09 window did not.
    assert cloud.calls == ["202609", "202409", "202209", "202009"]
    assert tracker.state["ended_by"] == "register"
    assert tracker.months_included == 50


async def test_without_a_registration_month_two_empty_windows_end_it() -> None:
    """Without a registration month two empty windows end it."""
    cloud = Cloud(monthly("202504", "202609"), register=None)
    tracker = started()
    await sync(tracker, cloud)
    # 2026-09 holds energy; 2024-09 and 2022-09 are two empty windows.
    assert cloud.calls == ["202609", "202409", "202209"]
    assert tracker.state["ended_by"] == "empty"
    assert tracker.months_included == 18


async def test_a_report_without_buckets_is_an_empty_window() -> None:
    """With raise_on_error the library answers "no data" with an empty report."""
    cloud = Cloud({}, empty=True)
    tracker = started(heat=1.0)
    await sync(tracker, cloud)
    # Windows of 24 months from 2026-09 down to the one reaching 2024-08.
    assert cloud.calls == ["202609", "202409"]
    assert tracker.state["ended_by"] == "register"
    assert tracker.months_included == 0
    assert tracker.lifetime_value(HEAT) == 1.0


async def test_a_report_with_neither_buckets_nor_registration_is_a_glitch() -> None:
    """Two such answers cannot end the history as "empty": they are failures."""
    tracker = started()
    for _ in range(2):
        with pytest.raises(LifetimeSyncError, match="No consumption data"):
            await sync(tracker, Cloud({}, register=None, empty=True))
    assert not tracker.complete
    assert tracker.state["windows"] == 0
    assert tracker.state["none_responses"] == 2


async def test_the_backfill_is_capped_at_fifteen_years() -> None:
    """The backfill is capped at fifteen years."""
    cloud = Cloud(monthly("200001", "202609"), register=None)
    tracker = started()
    await sync(tracker, cloud)
    assert len(cloud.calls) == lifetime.MAX_WINDOWS == 7  # 14 years
    assert tracker.state["ended_by"] == "cap"
    assert tracker.complete


async def test_a_window_that_makes_no_progress_is_a_failure() -> None:
    """The window must hold months older than the cursor."""
    cloud = Cloud({})

    async def stuck(end: date) -> ConsumptionReport:
        """Stuck."""
        return ConsumptionReport(
            history=[Consumption({"dataTime": "202610", "heatConsumption": 1.0})]
        )

    tracker = started()
    with pytest.raises(LifetimeSyncError, match="Malformed"):
        await tracker.async_sync(stuck, asyncio.Lock())
    assert not tracker.complete
    assert tracker.state["cursor"] == "202609"
    assert not cloud.calls


async def test_the_cursor_follows_the_months_the_cloud_returned() -> None:
    """A window of 12 months moves the cursor to before its own earliest month."""
    calls: list[date] = []

    async def short(end: date) -> ConsumptionReport:
        """Short."""
        calls.append(end)
        if len(calls) == 2:
            raise ConnectionError
        keys = [month_key(add_months(end, -n)) for n in range(11, -1, -1)]
        return ConsumptionReport(
            history=[
                Consumption({"dataTime": k, "heatConsumption": 1.0, "outdoorTemp": 1})
                for k in keys
            ],
            device_register_time="20200101",
        )

    tracker = started()
    with pytest.raises(ConnectionError):
        await tracker.async_sync(short, asyncio.Lock())
    # The first window held 2025-10..2026-09: the next one ends before it.
    assert calls == [date(2026, 9, 1), date(2025, 9, 1)]
    assert tracker.state["cursor"] == "202509"


async def test_an_aborted_backfill_resumes_without_asking_again() -> None:
    """An aborted backfill resumes without asking again."""
    tracker = started(heat=10.0)
    blocked = Cloud(
        monthly("202408", "202609"),
        script=["first window ok", aioaquarea.ApiError("HTTP_403", "blocked")],
    )
    with pytest.raises(aioaquarea.ApiError):
        await sync(tracker, blocked)
    assert blocked.calls == ["202609", "202409"]
    assert not tracker.complete
    assert tracker.state["windows"] == 1
    assert tracker.state["cursor"] == "202409"
    assert tracker.months_included == 24

    # A restart: the stored state resumes at the second window only.
    tracker = restart(tracker)
    resumed = Cloud(monthly("202408", "202609"))
    await sync(tracker, resumed)
    assert resumed.calls == ["202409"]
    assert tracker.complete
    assert tracker.lifetime_value(HEAT) == 2610.0


async def test_none_is_a_failure_once_the_registration_month_is_known() -> None:
    """None is a failure once the registration month is known."""
    tracker = started()
    cloud = Cloud(monthly("202408", "202609"), script=["ok", None])
    with pytest.raises(LifetimeSyncError):
        await sync(tracker, cloud)
    assert tracker.state["register_month"] == "202408"
    assert not tracker.complete
    assert tracker.state["none_responses"] == 1


async def test_none_ends_the_history_after_five_answers_over_three_days(
    freezer: FrozenDateTimeFactory,
) -> None:
    """With some history read, the old library's endless None is the start of it."""
    freezer.move_to(datetime(2026, 10, 20, 12, tzinfo=UTC))
    tracker = started()
    months = monthly("202408", "202609")
    with pytest.raises(LifetimeSyncError):
        await sync(tracker, Cloud(months, script=["ok", None]))
    assert tracker.state["windows"] == 1
    for _ in range(3):
        freezer.tick(timedelta(hours=18))
        with pytest.raises(LifetimeSyncError):
            await sync(tracker, Cloud(months, script=[None]))
    assert tracker.state["none_responses"] == 4
    # The 5th answer, 3 days after the first: taken as the start of the history.
    freezer.tick(timedelta(hours=18))
    await sync(tracker, Cloud(months, script=[None]))
    assert tracker.complete
    assert tracker.state["ended_by"] == "no_data"
    assert tracker.lifetime_value(HEAT) == 2400.0


async def test_none_for_the_first_window_never_ends_the_history(
    freezer: FrozenDateTimeFactory,
) -> None:
    """With nothing read, there is no history to end: it stays a failure."""
    freezer.move_to(datetime(2026, 10, 20, 12, tzinfo=UTC))
    tracker = started()
    for _ in range(8):
        with pytest.raises(LifetimeSyncError):
            await sync(tracker, Cloud({}, register=None, script=[None]))
        freezer.tick(timedelta(hours=24))
    assert not tracker.complete
    assert tracker.state["none_responses"] == 8


async def test_five_quick_nones_are_not_enough(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Five answers within hours are an outage, not the start of the history."""
    tracker = started()
    months = monthly("202408", "202609")
    with pytest.raises(LifetimeSyncError):
        await sync(tracker, Cloud(months, script=["ok", None]))
    for _ in range(6):
        with pytest.raises(LifetimeSyncError):
            await sync(tracker, Cloud(months, script=[None]))
    assert not tracker.complete
    assert tracker.state["none_responses"] == 7


async def test_a_good_window_resets_the_none_count() -> None:
    """A good window resets the none count."""
    tracker = started()
    with pytest.raises(LifetimeSyncError):
        await sync(tracker, Cloud(monthly("202408", "202609"), script=[None]))
    assert tracker.state["none_responses"] == 1
    await sync(tracker, Cloud(monthly("202408", "202609")))
    assert tracker.state["none_responses"] == 0


async def test_the_month_before_the_open_one_is_looked_up_again(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Read within 24 h of its end, September may lack its last hours."""
    freezer.move_to(datetime(2026, 10, 1, 6, tzinfo=UTC))
    tracker = new_tracker()
    tracker.observe_month(OPEN, totals(), date(2026, 10, 1))
    await sync(tracker, Cloud(monthly("202408", "202609")))
    assert list(tracker.state["pending"]) == ["202609"]
    assert not tracker.sync_due(datetime(2026, 10, 1, 7, tzinfo=UTC))
    assert tracker.sync_due(datetime(2026, 10, 2, 1, tzinfo=UTC))


async def test_no_backfill_request_after_it_completed() -> None:
    """No backfill request after it completed."""
    tracker = started()
    await sync(tracker, Cloud(monthly("202408", "202609")))
    assert not tracker.sync_due(datetime(2026, 11, 20, tzinfo=UTC))
    assert not restart(tracker).sync_due(datetime(2026, 11, 20, tzinfo=UTC))


# --- the open month and the finished months ------------------------------------


async def test_the_open_month_is_replaced_and_the_value_never_goes_back() -> None:
    """The open month is replaced and the value never goes back."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    assert tracker.lifetime_value(HEAT) == 10.0
    # Revised upwards: replaced, not added.
    tracker.observe_month(OPEN, totals(heat=12.0), TODAY)
    assert tracker.lifetime_value(HEAT) == 12.0
    # Revised downwards (or a partial list): the open month follows the
    # cloud, the published value does not.
    tracker.observe_month(OPEN, totals(heat=11.0), TODAY)
    assert tracker.state["open"]["heat"] == 11.0
    assert tracker.lifetime_value(HEAT) == 12.0
    tracker.observe_month(OPEN, totals(heat=12.5), TODAY)
    assert tracker.lifetime_value(HEAT) == 12.5


async def test_a_refresh_without_new_data_changes_nothing() -> None:
    """A refresh without new data changes nothing."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    saves = tracker._store.saves  # noqa: SLF001
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    assert tracker.lifetime_value(HEAT) == 10.0
    assert tracker._store.saves == saves  # noqa: SLF001


async def test_the_open_month_is_folded_once_across_restarts() -> None:
    """The open month is folded once across restarts."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    november = date(2026, 11, 1)
    tracker.observe_month(november, totals(heat=1.0), date(2026, 11, 1))
    assert tracker.state["months"] == {"202610": totals(heat=10.0)}
    assert tracker.state["open_month"] == "202611"
    assert tracker.lifetime_value(HEAT) == 11.0
    assert tracker._flush_soon  # noqa: SLF001 - a fold is written at once

    for _ in range(3):
        tracker = restart(tracker)
        tracker.observe_month(november, totals(heat=1.5), date(2026, 11, 2))
        assert tracker.state["months"] == {"202610": totals(heat=10.0)}
        assert tracker.lifetime_value(HEAT) == 11.5


async def test_a_month_before_the_open_one_is_a_stale_cache() -> None:
    """A month before the open one is a stale cache."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(date(2026, 11, 1), totals(heat=1.0), date(2026, 11, 2))
    tracker.observe_month(OPEN, totals(heat=99.0), date(2026, 11, 2))
    assert tracker.state["open_month"] == "202611"
    assert tracker.state["months"] == {}
    assert tracker.lifetime_value(HEAT) == 1.0


async def test_skipped_months_are_looked_up_later(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Home Assistant was off over two month ends."""
    freezer.move_to(datetime(2026, 12, 5, 12, tzinfo=UTC))
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(date(2026, 9, 1), totals(heat=10.0), date(2026, 9, 20))
    tracker.observe_month(date(2026, 12, 1), totals(heat=2.0), date(2026, 12, 5))
    assert sorted(tracker.state["pending"]) == ["202609", "202610", "202611"]
    assert list(tracker.state["months"]) == ["202609"]
    assert tracker.sync_due(datetime(2026, 12, 5, 12, tzinfo=UTC))

    cloud = Cloud({"202609": (10.5, 0.0, 0.0), "202610": (30.0, 0.0, 0.0)})
    await sync(tracker, cloud)
    assert cloud.calls == ["202611"]
    assert not tracker.state["pending"]
    assert tracker.state["months"]["202609"]["heat"] == 10.5  # the larger figure
    assert tracker.state["months"]["202610"]["heat"] == 30.0
    assert tracker.lifetime_value(HEAT) == 42.5


async def test_a_finished_month_keeps_the_larger_figure(
    freezer: FrozenDateTimeFactory,
) -> None:
    """A finished month keeps the larger figure."""
    freezer.move_to(datetime(2026, 11, 5, 12, tzinfo=UTC))
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    tracker.observe_month(date(2026, 11, 1), totals(), date(2026, 11, 1))
    # The cloud now says less for October (a revision down): not taken.
    await sync(tracker, Cloud({"202610": (9.0, 0.0, 0.0)}))
    assert tracker.state["months"]["202610"]["heat"] == 10.0
    assert tracker.lifetime_value(HEAT) == 10.0


async def test_the_new_month_s_first_hours_count_before_the_month_is_shown() -> None:
    """Between the cloud's midnight and the coordinator moving on."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), date(2026, 10, 31))
    tracker.observe_hours(
        {
            datetime(2026, 10, 31, 23, tzinfo=UTC): totals(heat=0.5),
            datetime(2026, 11, 1, 0, tzinfo=UTC): totals(heat=0.25),
            datetime(2026, 11, 1, 1, tzinfo=UTC): totals(heat=0.25),
        },
        datetime(2026, 11, 1, 2, tzinfo=UTC),
    )
    # Only the hours from November count on top of October.
    assert tracker.lifetime_value(HEAT) == 10.5
    # November is shown, with its own total of those hours: not counted twice.
    tracker.observe_month(date(2026, 11, 1), totals(heat=0.5), date(2026, 11, 1))
    assert tracker.lifetime_value(HEAT) == 10.5
    tracker.observe_month(date(2026, 11, 1), totals(heat=0.75), date(2026, 11, 1))
    assert tracker.lifetime_value(HEAT) == 10.75


async def test_a_future_open_month_is_repaired_without_a_new_backfill() -> None:
    """A future open month is repaired without a new backfill."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    tracker.observe_month(
        date(2026, 9, 1), totals(), date(2026, 9, 1)
    )  # stale: ignored
    tracker.state["open_month"] = "203001"  # damaged
    tracker.state["months"]["202609"] = totals(heat=1.0)
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    assert tracker.state["open_month"] == "202610"
    assert tracker.complete
    assert tracker.state["months"] == {"202609": totals(heat=1.0)}


# --- the floor, the rebuild, the back-off ------------------------------------


async def test_the_floor_survives_a_rebuild_and_the_value_never_dips(
    freezer: FrozenDateTimeFactory,
) -> None:
    """The floor survives a rebuild and the value never dips."""
    freezer.move_to(datetime(2026, 10, 20, 12, tzinfo=UTC))
    tracker = started(heat=10.0)
    await sync(tracker, Cloud(monthly("202408", "202609")))
    published = tracker.lifetime_value(HEAT)
    assert published == 2610.0

    await tracker.async_rebuild(
        OPEN, totals(heat=10.0), datetime.now(UTC), date(2026, 10, 20)
    )
    assert not tracker.complete
    # While the history is read again the last value stays.
    assert tracker.lifetime_value(HEAT) == published
    # A rebuild that finds less history cannot lower it either.
    await sync(tracker, Cloud(monthly("202608", "202609", heat=1.0)))
    assert tracker.complete
    assert tracker.lifetime_value(HEAT) == published
    # And one that finds more raises it.
    await tracker.async_rebuild(
        OPEN, totals(heat=10.0), datetime.now(UTC), date(2026, 10, 20)
    )
    await sync(tracker, Cloud(monthly("202308", "202609")))
    assert tracker.lifetime_value(HEAT) == 3810.0


async def test_a_rebuild_keeps_a_back_off_that_is_still_running(
    freezer: FrozenDateTimeFactory,
) -> None:
    """A rebuild keeps a back off that is still running."""
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    freezer.move_to(now)
    tracker = started()
    tracker.note_failure(now, access=True)
    retry_at = tracker.retry_at
    assert retry_at == now + timedelta(hours=6)

    await tracker.async_rebuild(OPEN, totals(), now, date(2026, 10, 20))
    assert tracker.retry_at == retry_at
    assert tracker.state["failures"] == 1
    assert not tracker.sync_due(now + timedelta(hours=5))
    assert tracker.sync_due(now + timedelta(hours=6, minutes=1))

    # A back-off that has passed is not kept.
    await tracker.async_rebuild(
        OPEN, totals(), now + timedelta(hours=7), date(2026, 10, 20)
    )
    assert tracker.retry_at is None


def test_the_retry_delays_double() -> None:
    """The retry delays double."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    tracker = new_tracker()
    delays = []
    for _ in range(5):
        tracker.note_failure(now, access=False)
        delays.append(tracker.retry_at - now)
    assert delays == [timedelta(hours=h) for h in (1, 2, 4, 8, 12)]
    tracker = new_tracker()
    delays = []
    for _ in range(3):
        tracker.note_failure(now, access=True)
        delays.append(tracker.retry_at - now)
    assert delays == [timedelta(hours=h) for h in (6, 12, 12)]
    tracker.note_success()
    assert tracker.retry_at is None and tracker.state["failures"] == 0


def test_a_retry_time_far_in_the_future_is_clamped(
    freezer: FrozenDateTimeFactory,
) -> None:
    """A retry time far in the future is clamped."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    freezer.move_to(now)
    tracker = started()
    tracker.state["retry_at"] = (now + timedelta(days=400)).isoformat()
    tracker = restart(tracker)
    assert tracker.retry_at == now + timedelta(hours=12)
    assert not tracker.sync_due(now)
    assert tracker.sync_due(now + timedelta(hours=12, minutes=1))


# --- today ----------------------------------------------------------------------


def hours(first: datetime, count: int, heat: float = 1.0) -> dict:
    """Return ``count`` consecutive hourly records of 1 kWh heating."""
    return {first + timedelta(hours=n): totals(heat=heat) for n in range(count)}


async def set_zone(hass: HomeAssistant, zone: str) -> None:
    """Set the Home Assistant time zone."""
    await hass.config.async_set_time_zone(zone)


async def test_today_follows_the_local_day_not_the_cloud_s(hass: HomeAssistant) -> None:
    """Today follows the local day not the cloud s."""
    await set_zone(hass, "Europe/Budapest")
    tracker = new_tracker()
    # Budapest in November is UTC+1: the local 2026-11-08 starts at 23:00Z on the 7th.
    tracker.observe_hours(
        hours(datetime(2026, 11, 7, 20, tzinfo=UTC), 8),  # 20:00Z..03:00Z
        datetime(2026, 11, 8, 4, tzinfo=BUDAPEST),
    )
    now = datetime(2026, 11, 8, 4, tzinfo=BUDAPEST)
    # Hours starting at 23:00Z, 00:00Z, 01:00Z, 02:00Z, 03:00Z are the local 8th.
    assert tracker.today_value(TOTAL, now) == 5.0
    # The same records are the previous local day for the 7th.
    assert tracker.today_value(TOTAL, datetime(2026, 11, 7, 12, tzinfo=BUDAPEST)) == 3.0


async def test_today_resets_at_local_midnight(hass: HomeAssistant) -> None:
    """Today resets at local midnight."""
    await set_zone(hass, "Europe/Budapest")
    before = datetime(2026, 11, 7, 23, 59, tzinfo=BUDAPEST)
    after = datetime(2026, 11, 8, 0, 0, tzinfo=BUDAPEST)
    assert local_day_bounds(before)[0] == datetime(2026, 11, 7, tzinfo=BUDAPEST)
    assert local_day_bounds(after)[0] == datetime(2026, 11, 8, tzinfo=BUDAPEST)
    tracker = new_tracker()
    tracker.observe_hours(
        {datetime(2026, 11, 7, 22, tzinfo=UTC): totals(heat=2.0)}, after
    )
    assert tracker.today_value(TOTAL, before) == 2.0
    assert tracker.today_value(TOTAL, after) == 0.0


async def test_an_hour_published_late_for_yesterday_does_not_leak_into_today(
    hass: HomeAssistant,
) -> None:
    """An hour published late for yesterday does not leak into today."""
    await set_zone(hass, "Europe/Budapest")
    tracker = new_tracker()
    tracker.observe_hours(
        {datetime(2026, 11, 8, 0, tzinfo=UTC): totals(heat=1.0)},  # 01:00 on the 8th
        datetime(2026, 11, 8, 2, tzinfo=BUDAPEST),
    )
    now = datetime(2026, 11, 8, 2, tzinfo=BUDAPEST)
    assert tracker.today_value(TOTAL, now) == 1.0
    # At 00:10 the cloud publishes the 23:00-24:00 hour of the 7th.
    tracker.observe_hours(
        {datetime(2026, 11, 7, 22, tzinfo=UTC): totals(heat=0.7)},
        datetime(2026, 11, 8, 3, tzinfo=BUDAPEST),
    )
    assert tracker.today_value(TOTAL, now) == 1.0
    assert tracker.today_value(TOTAL, datetime(2026, 11, 7, 12, tzinfo=BUDAPEST)) == 0.7


async def test_a_revised_hour_replaces_its_value(hass: HomeAssistant) -> None:
    """A revised hour replaces its value."""
    await set_zone(hass, "Europe/Budapest")
    now = datetime(2026, 11, 8, 10, tzinfo=BUDAPEST)
    tracker = new_tracker()
    hour = datetime(2026, 11, 8, 6, tzinfo=UTC)
    tracker.observe_hours({hour: totals(heat=1.0)}, now)
    tracker.observe_hours({hour: totals(heat=1.4)}, now)
    assert tracker.today_value(TOTAL, now) == 1.4
    tracker.observe_hours({hour: totals(heat=1.4)}, now)  # re-fetched: unchanged
    assert tracker.today_value(TOTAL, now) == 1.4
    tracker.observe_hours({hour: totals(heat=1.2)}, now)  # revised down
    assert tracker.today_value(TOTAL, now) == 1.2


async def test_today_kinds(hass: HomeAssistant) -> None:
    """Today kinds."""
    await set_zone(hass, "Europe/Budapest")
    now = datetime(2026, 11, 8, 10, tzinfo=BUDAPEST)
    tracker = new_tracker()
    tracker.observe_hours(
        {datetime(2026, 11, 8, 6, tzinfo=UTC): totals(heat=1.0, cool=0.5, tank=2.0)},
        now,
    )
    assert tracker.today_value(HEAT, now) == 1.0
    assert tracker.today_value(TANK, now) == 2.0
    assert tracker.today_value(aioaquarea.ConsumptionType.COOL, now) == 0.5
    assert tracker.today_value(TOTAL, now) == 3.5


async def test_today_is_unknown_without_any_hour(hass: HomeAssistant) -> None:
    """Today is unknown without any hour."""
    assert new_tracker().today_value(TOTAL, datetime.now(UTC)) is None


async def test_the_day_the_clocks_go_forward_has_23_hours(hass: HomeAssistant) -> None:
    """The day the clocks go forward has 23 hours."""
    await set_zone(hass, "Europe/Budapest")
    start, end = local_day_bounds(datetime(2026, 3, 29, 12, tzinfo=BUDAPEST))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=23)
    tracker = new_tracker()
    # Every hour from 2026-03-28 22:00Z to 2026-03-30 03:00Z, 1 kWh each.
    tracker.observe_hours(
        hours(datetime(2026, 3, 28, 22, tzinfo=UTC), 30),
        datetime(2026, 3, 30, 5, tzinfo=BUDAPEST),
    )
    # CET until 02:00 (23:00Z), CEST after: the local 29th starts 23:00Z on the
    # 28th and ends 22:00Z on the 29th.
    assert (
        tracker.today_value(TOTAL, datetime(2026, 3, 29, 12, tzinfo=BUDAPEST)) == 23.0
    )


async def test_the_day_the_clocks_go_back_has_25_hours(hass: HomeAssistant) -> None:
    """The day the clocks go back has 25 hours."""
    await set_zone(hass, "Europe/Budapest")
    start, end = local_day_bounds(datetime(2026, 10, 25, 12, tzinfo=BUDAPEST))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)
    tracker = new_tracker()
    tracker.observe_hours(
        hours(datetime(2026, 10, 24, 20, tzinfo=UTC), 32),
        datetime(2026, 10, 26, 5, tzinfo=BUDAPEST),
    )
    assert (
        tracker.today_value(TOTAL, datetime(2026, 10, 25, 12, tzinfo=BUDAPEST)) == 25.0
    )
    # The neighbours have 24.
    assert (
        tracker.today_value(TOTAL, datetime(2026, 10, 24, 12, tzinfo=BUDAPEST)) == 2.0
    )


@pytest.mark.parametrize(
    ("zone", "tzinfo", "utc_start"),
    [
        ("America/New_York", NEW_YORK, datetime(2026, 11, 8, 5, tzinfo=UTC)),
        ("Asia/Kolkata", KOLKATA, datetime(2026, 11, 7, 18, 30, tzinfo=UTC)),
    ],
)
async def test_the_local_day_in_other_zones(
    hass: HomeAssistant, zone: str, tzinfo: ZoneInfo, utc_start: datetime
) -> None:
    """New York is UTC-5 on 2026-11-08 (the fall-back day is the 1st); Kolkata +5:30."""
    await set_zone(hass, zone)
    now = datetime(2026, 11, 8, 12, tzinfo=tzinfo)
    start, end = local_day_bounds(now)
    assert start == utc_start
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=24)
    tracker = new_tracker()
    # Whole hours; Kolkata's half hour offset puts the day start between them,
    # so its first (partial) UTC hour belongs to the previous day by its start.
    tracker.observe_hours(hours(datetime(2026, 11, 7, 0, tzinfo=UTC), 72), now)
    expected = sum(
        1
        for n in range(72)
        if start <= datetime(2026, 11, 7, tzinfo=UTC) + timedelta(hours=n) < end
    )
    assert tracker.today_value(TOTAL, now) == float(expected)


async def test_the_new_york_fall_back_day(hass: HomeAssistant) -> None:
    """The new york fall back day."""
    await set_zone(hass, "America/New_York")
    start, end = local_day_bounds(datetime(2026, 11, 1, 12, tzinfo=NEW_YORK))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)


async def test_old_hours_are_dropped_after_a_few_days() -> None:
    """Old hours are dropped after a few days."""
    tracker = new_tracker()
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    tracker.observe_hours(
        {
            datetime(2026, 11, 1, 0, tzinfo=UTC): totals(heat=1.0),
            datetime(2026, 11, 8, 0, tzinfo=UTC): totals(heat=1.0),
        },
        now,
    )
    assert list(tracker.state["hours"]) == ["2026-11-08T00:00:00+00:00"]


def test_every_kind_is_covered() -> None:
    """Every kind is covered."""
    assert set(lifetime.KINDS_BY_TYPE[TOTAL]) == set(KINDS)


def test_odd_bucket_values_count_as_zero() -> None:
    """Text, infinity and negative values in a bucket are 0 kWh."""
    bucket = Consumption(
        {"heatConsumption": "x", "coolConsumption": float("inf"), "tankConsumption": -1}
    )
    assert lifetime.bucket_totals(bucket) == totals()


def test_the_month_total_reads_both_date_formats_and_skips_junk() -> None:
    """Days after today, and labels that are not dates, are not counted."""
    records = [
        Consumption({"dataTime": "20261001", "heatConsumption": 1.0}),
        Consumption({"dataTime": "2026-10-02", "heatConsumption": 2.0}),
        Consumption({"dataTime": "20261025", "heatConsumption": 4.0}),
        Consumption({"dataTime": "junk", "heatConsumption": 8.0}),
        Consumption({"heatConsumption": 16.0}),
    ]
    assert lifetime.month_totals(records, TODAY)["heat"] == 3.0


async def test_a_due_month_with_no_data_is_a_failure(
    freezer: FrozenDateTimeFactory,
) -> None:
    """A lookup of a finished month that gets None fails and stays pending."""
    freezer.move_to(datetime(2026, 12, 5, tzinfo=UTC))
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(date(2026, 9, 1), totals(heat=1.0), date(2026, 9, 20))
    tracker.observe_month(date(2026, 11, 1), totals(), date(2026, 11, 2))
    with pytest.raises(LifetimeSyncError):
        await sync(tracker, Cloud({}, script=[None]))
    assert "202609" in tracker.state["pending"]


async def test_repairing_a_future_open_month_drops_the_months_after_the_shown_one() -> (
    None
):
    """Months at or after the shown one cannot be right and are dropped."""
    tracker = new_tracker()
    tracker.state["complete"] = True
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    tracker.state["open_month"] = "203001"
    tracker.state["months"] = {
        "202609": totals(heat=1.0),
        "202611": totals(heat=2.0),
    }
    tracker.state["pending"] = {"202612": "2027-01-01T00:00:00+00:00"}
    tracker.observe_month(OPEN, totals(heat=10.0), TODAY)
    assert tracker.state["months"] == {"202609": totals(heat=1.0)}
    assert not tracker.state["pending"]


# --- implausible values, schema changes ----------------------------------------


async def test_a_glitch_month_in_a_window_is_not_stored_and_the_state_stays_valid() -> (
    None
):
    """A 60,000 kWh month would make the state look corrupt at the next start."""
    months = monthly("202408", "202609")
    months["202501"] = (60_000.0, 0.0, 0.0)
    tracker = started()
    await sync(tracker, Cloud(months))
    assert "202501" not in tracker.state["months"]
    assert validate_state(json.loads(json.dumps(tracker.state))) is not None
    assert restart(tracker).complete
    assert tracker.lifetime_value(HEAT) == 2500.0


async def test_a_glitch_hour_is_not_stored_and_the_state_stays_valid() -> None:
    """65,535 kWh in an hour is a cloud placeholder, not energy."""
    tracker = new_tracker()
    now = datetime(2026, 11, 8, 5, tzinfo=UTC)
    tracker.observe_hours(
        {
            datetime(2026, 11, 8, 3, tzinfo=UTC): totals(heat=65_535.0),
            datetime(2026, 11, 8, 4, tzinfo=UTC): totals(heat=0.5),
        },
        now,
    )
    assert validate_state(json.loads(json.dumps(tracker.state))) is not None
    assert tracker.state["hours"]["2026-11-08T03:00:00+00:00"] == totals()
    assert tracker.state["hours"]["2026-11-08T04:00:00+00:00"] == totals(heat=0.5)


def test_a_glitch_day_does_not_count_in_the_month() -> None:
    """A day above the plausibility limit is skipped; the others count."""
    records = [
        Consumption({"dataTime": "20261001", "heatConsumption": 1.0}),
        Consumption({"dataTime": "20261002", "heatConsumption": 60_000.0}),
    ]
    month = lifetime.month_totals(records, TODAY)
    assert month["heat"] == 1.0
    tracker = new_tracker()
    tracker.observe_month(OPEN, month, TODAY)
    assert validate_state(json.loads(json.dumps(tracker.state))) is not None


def test_observed_totals_above_the_limits_are_zeroed() -> None:
    """The tracker does not trust its callers: nothing implausible is stored."""
    tracker = new_tracker()
    tracker.observe_month(OPEN, totals(heat=60_000.0, tank=3.0), TODAY)
    assert tracker.state["open"] == totals(tank=3.0)


async def test_today_is_rounded_to_three_decimals(hass: HomeAssistant) -> None:
    """Float sums do not leak into the state."""
    await set_zone(hass, "UTC")
    tracker = new_tracker()
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    tracker.observe_hours(
        {
            datetime(2026, 11, 8, 1, tzinfo=UTC): totals(heat=0.1),
            datetime(2026, 11, 8, 2, tzinfo=UTC): totals(heat=0.2),
        },
        now,
    )
    assert tracker.today_value(HEAT, now) == 0.3


def test_a_state_from_an_older_version_loads_in_a_newer_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key added later takes its default: nobody reads the history again."""
    tracker = started(heat=3.0)
    tracker.state["complete"] = True
    tracker.state["floor"] = totals(heat=1234.5)
    tracker.state["months"] = {"202609": totals(heat=100.0)}
    stored = json.loads(json.dumps(tracker.state))

    original = lifetime.fresh_state
    monkeypatch.setattr(lifetime, "fresh_state", lambda: {**original(), "added": 7})
    loaded = validate_state(stored)
    assert loaded is not None
    assert loaded["added"] == 7
    assert loaded["complete"] is True
    assert loaded["floor"] == totals(heat=1234.5)
    assert loaded["months"] == {"202609": totals(heat=100.0)}
    # And a wrong type in a key the old version did not write is still invalid.
    assert validate_state({**stored, "complete": "yes"}) is None
