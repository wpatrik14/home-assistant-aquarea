"""Lifetime and today's energy totals, built from the cloud's consumption history.

Lifetime
--------
The lifetime figure of a device, per kind (heating, cooling, tank), is

    sum of the finished months (persisted) + the open month + boundary hours

The open month is the month the coordinator shows: its cached MONTH query (one
bucket per day), refetched every consumption interval and replacing the
previous list as a whole. The tracker replaces the month's totals with every
fetch, so a day the cloud revises or publishes late is replaced by its new
value, never added again; newly published energy reaches the figure with the
next fetch. Between the cloud's midnight and the moment the coordinator moves
on to the next month (about four hours, see ``coordinator._monthly_month_due``)
the hours of the new month, which the hourly fetch already has, are added as
boundary hours, so the figure keeps growing. When the coordinator shows the
next month, the open month is folded into the finished months exactly once: a
month is a key of ``months`` or the open month, never both, so a restart
cannot count it twice. A folded month is looked up again once
(``pending``), ``LIFETIME_RECONCILE_DELAY`` after it ended, for hours the cloud
published late; a finished month keeps the larger of the figures seen.

The published value never goes back: it is the larger of the ledger above
and the largest value published before (``floor``).

The history before the first start is read by a backfill: the YEAR query
returns 12 monthly buckets ending at the month sent plus the 12 before them,
so one request covers 24 months. The backfill walks back from the month
before the shown one. The response says in which month the unit was
registered; it stops at the window that reaches that month (and holds no
energy before it), or, while the month is unknown, after
``LIFETIME_EMPTY_WINDOWS_STOP`` windows in a row without energy, or at the
``LIFETIME_MAX_YEARS_BACK`` years cap. "No data" answers are failures to retry
(the library returns None for network errors too). Every window is stored as
soon as it is read, with the cursor, so an aborted backfill resumes where it
stopped. Restarts, reloads and options changes never start it again: only
missing or invalid stored data, or the rebuild button, do.

The figure is unknown until the first backfill has completed. The Energy
dashboard counts a rise of a ``total_increasing`` sensor as consumption, so a
partial figure followed by the complete one would show the whole history as
one spike; Home Assistant ignores the states before the first number.

Today
-----
The hourly records the coordinator fetches (UTC hour labels) are kept for a
few days. "Today" is the sum of the hours that start within the local calendar
day, from the local midnight to the next one (23 or 25 hours on the days the
clocks change). An hour is a key of the stored map, so a revised hour replaces
its old value, and an hour published after midnight for the previous day
belongs to the previous day by its own timestamp and never to today.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, date, datetime, timedelta
import logging
import math
import re
from typing import Any

import aioaquarea
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    LIFETIME_EMPTY_WINDOWS_STOP,
    LIFETIME_HOURS_KEPT,
    LIFETIME_MAX_MONTH_KWH,
    LIFETIME_MAX_YEARS_BACK,
    LIFETIME_NO_DATA_MIN_RESPONSES,
    LIFETIME_NO_DATA_MIN_SPAN,
    LIFETIME_RECONCILE_DELAY,
    LIFETIME_REQUEST_DELAY,
    LIFETIME_RETRY_AUTH_INITIAL,
    LIFETIME_RETRY_INITIAL,
    LIFETIME_RETRY_MAX,
    LIFETIME_SAVE_DELAY,
    LIFETIME_START_DELAY,
)
from .statistics import hourly_consumption

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

KINDS = ("heat", "cool", "tank")
KINDS_BY_TYPE: dict[aioaquarea.ConsumptionType, tuple[str, ...]] = {
    aioaquarea.ConsumptionType.HEAT: ("heat",),
    aioaquarea.ConsumptionType.COOL: ("cool",),
    aioaquarea.ConsumptionType.WATER_TANK: ("tank",),
    aioaquarea.ConsumptionType.TOTAL: KINDS,
}

# One YEAR query returns this many months: 12 plus the 12 before them.
WINDOW_MONTHS = 24
MAX_WINDOWS = -(-LIFETIME_MAX_YEARS_BACK * 12 // WINDOW_MONTHS)
# Looking up finished months is bounded per sync, as a safety net.
MAX_RECONCILE_REQUESTS = 4
# A ledger this much below the published floor is logged once.
LEDGER_WARN_RATIO = 0.10

_EPSILON = 1e-6
_MONTH_KEY = re.compile(r"\d{4}(0[1-9]|1[0-2])")

type Totals = dict[str, float]
type FetchYear = Callable[[date], Awaitable[aioaquarea.ConsumptionReport | None]]


class LifetimeSyncError(Exception):
    """A sync step failed in a way that is retried later (no usable response)."""


# --- month arithmetic ------------------------------------------------------


def month_key(month: date) -> str:
    """Return the ``YYYYMM`` key of a month."""
    return f"{month.year:04d}{month.month:02d}"


def parse_month(key: str) -> date:
    """Return the first day of a ``YYYYMM`` month."""
    return date(int(key[:4]), int(key[4:6]), 1)


def add_months(month: date, count: int) -> date:
    """Return the first day of the month ``count`` months after ``month``."""
    index = month.year * 12 + month.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def _month_end(month: date) -> datetime:
    """Return the start of the month after ``month`` (the cloud's UTC)."""
    following = add_months(month, 1)
    return datetime(following.year, following.month, 1, tzinfo=UTC)


# --- totals ----------------------------------------------------------------


def zero_totals() -> Totals:
    """Return totals of 0 kWh."""
    return dict.fromkeys(KINDS, 0.0)


def _number(value: Any) -> float:
    """Return a bucket value as a finite, non-negative number (else 0)."""
    try:
        number = float(value or 0.0)
    except TypeError, ValueError:
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def bucket_totals(bucket: aioaquarea.Consumption) -> Totals:
    """Return the three consumption kinds of a consumption bucket."""
    return {
        "heat": _number(bucket.heat_consumption),
        "cool": _number(bucket.cool_consumption),
        "tank": _number(bucket.tank_consumption),
    }


def _max_totals(first: Totals, second: Totals) -> Totals:
    return {kind: max(first[kind], second[kind]) for kind in KINDS}


def month_totals(records: Iterable[aioaquarea.Consumption], today: date) -> Totals:
    """Sum the daily buckets of a MONTH query up to the cloud's date ``today``."""
    totals = zero_totals()
    for record in records:
        day = None
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                day = datetime.strptime(record.data_time or "", fmt).date()
                break
            except ValueError:
                continue
        if day is None or day > today:
            continue
        for kind, value in bucket_totals(record).items():
            totals[kind] += value
    return totals


def hourly_totals(
    records: Iterable[aioaquarea.Consumption],
) -> dict[datetime, Totals]:
    """Map the DAY query's records to {start of the hour in UTC: totals}."""
    records = list(records)
    by_kind = {
        "heat": hourly_consumption(records, aioaquarea.ConsumptionType.HEAT),
        "cool": hourly_consumption(records, aioaquarea.ConsumptionType.COOL),
        "tank": hourly_consumption(records, aioaquarea.ConsumptionType.WATER_TANK),
    }
    hours = set().union(*by_kind.values())
    return {
        hour: {kind: by_kind[kind].get(hour, 0.0) for kind in KINDS} for hour in hours
    }


# --- stored state ----------------------------------------------------------


def fresh_state() -> dict[str, Any]:
    """Return the state of a device with no history read yet."""
    return {
        # Backfill: done (and why it ended), the end month of the next window,
        # the unit's registration month (from the cloud), windows read,
        # windows in a row without energy, and the "no data" answers for the
        # next window with when the first came.
        "complete": False,
        "ended_by": None,
        "cursor": None,
        "register_month": None,
        "windows": 0,
        "empty_windows": 0,
        "none_responses": 0,
        "none_since": None,
        # Failed syncs in a row, and when the next may start.
        "failures": 0,
        "retry_at": None,
        # The month being shown and its totals as of the latest fetch.
        "open_month": None,
        "open": zero_totals(),
        # Finished months {YYYYMM: totals} (months without energy are absent),
        # and months to look up again {YYYYMM: not before (ISO, UTC)}.
        "months": {},
        "pending": {},
        # The largest lifetime value published per kind: never goes back.
        "floor": zero_totals(),
        # Hourly totals {start of the hour (ISO, UTC): totals}.
        "hours": {},
    }


def _valid_totals(value: Any, limit: float | None = None) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(KINDS)
        and all(
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(number)
            and number >= 0
            and (limit is None or number <= limit)
            for number in value.values()
        )
    )


def _valid_month(value: Any) -> bool:
    return isinstance(value, str) and _MONTH_KEY.fullmatch(value) is not None


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_state(raw: Any) -> dict[str, Any] | None:
    """Return the stored state of a device, or None when it is invalid.

    Invalid means a shape or value that this code never writes, or a
    combination that cannot happen (a finished month at or after the open
    month, a started backfill without a cursor, a month with an implausible
    amount of energy); the history is then read again. A ledger below the
    published floor is not invalid: the floor keeps the value, see
    ``LifetimeTracker.ledger_below_floor``.
    """
    if not isinstance(raw, dict):
        return None
    state = fresh_state()
    if not set(state) <= set(raw):
        return None
    for name in ("windows", "empty_windows", "none_responses", "failures"):
        if (
            not isinstance(raw[name], int)
            or isinstance(raw[name], bool)
            or raw[name] < 0
        ):
            return None
    if not isinstance(raw["complete"], bool):
        return None
    if raw["ended_by"] is not None and not isinstance(raw["ended_by"], str):
        return None
    for name in ("cursor", "open_month", "register_month"):
        if raw[name] is not None and not _valid_month(raw[name]):
            return None
    for name in ("retry_at", "none_since"):
        if raw[name] is not None and not _valid_time(raw[name]):
            return None
    if not (
        _valid_totals(raw["open"], LIFETIME_MAX_MONTH_KWH)
        and _valid_totals(raw["floor"])
    ):
        return None
    months, pending, hours = raw["months"], raw["pending"], raw["hours"]
    if not (
        isinstance(months, dict)
        and isinstance(pending, dict)
        and isinstance(hours, dict)
    ):
        return None
    if not (
        all(
            _valid_month(k) and _valid_totals(v, LIFETIME_MAX_MONTH_KWH)
            for k, v in months.items()
        )
        and all(_valid_month(k) and _valid_time(v) for k, v in pending.items())
        and all(
            _valid_time(k) and _valid_totals(v, LIFETIME_MAX_MONTH_KWH)
            for k, v in hours.items()
        )
    ):
        return None
    open_month = raw["open_month"]
    if open_month is None:
        if raw["complete"] or months or pending:
            return None
    else:
        if any(key >= open_month for key in (*months, *pending)):
            return None
        if not raw["complete"] and raw["cursor"] is None:
            return None
    return {name: raw[name] for name in state}


class LifetimeStore:
    """The persisted lifetime state of the devices of one config entry.

    It lives in ``.storage/aquarea.lifetime.<entry id>``: it survives restarts,
    reloads and updates, and is removed with the config entry only.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store; ``async_load`` reads it."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.lifetime.{entry_id}"
        )
        self._devices: dict[str, Any] = {}
        self._trackers: dict[str, LifetimeTracker] = {}

    async def async_load(self) -> None:
        """Read the stored state."""
        try:
            raw = await self._store.async_load()
        except HomeAssistantError as err:
            _LOGGER.warning("Unreadable lifetime consumption storage: %s", err)
            raw = None
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if isinstance(devices, dict):
            self._devices = devices
        elif raw is not None:
            _LOGGER.warning("Invalid lifetime consumption storage; reading it again")

    def tracker(self, device_id: str) -> LifetimeTracker:
        """Return the tracker of a device, creating it from the stored state."""
        if device_id not in self._trackers:
            raw = self._devices.get(device_id)
            state = validate_state(raw)
            if state is None:
                if raw is not None:
                    _LOGGER.warning(
                        "Invalid stored lifetime consumption of a device; "
                        "reading its history again"
                    )
                state = fresh_state()
            self._trackers[device_id] = LifetimeTracker(state, self)
        return self._trackers[device_id]

    def _data(self) -> dict[str, Any]:
        # Devices that are not set up now (removed from the account, or not
        # loaded) keep what was stored for them.
        return {
            "devices": {
                **self._devices,
                **{id_: t.state for id_, t in self._trackers.items()},
            }
        }

    def schedule_save(self) -> None:
        """Write the state soon (the writes of one burst are combined)."""
        self._store.async_delay_save(self._data, LIFETIME_SAVE_DELAY)

    async def async_flush(self) -> None:
        """Write the state now."""
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        """Delete the stored state (the config entry is deleted)."""
        await self._store.async_remove()


def _buckets_by_month(
    report: aioaquarea.ConsumptionReport,
) -> dict[str, Totals]:
    """Return the monthly totals of a YEAR response, keyed by their own label."""
    months: dict[str, Totals] = {}
    for bucket in (*report.history, *report.last_year_history):
        key = (bucket.data_time or "")[:6]
        if _valid_month(key):
            totals = bucket_totals(bucket)
            months[key] = _max_totals(months.get(key, zero_totals()), totals)
    return months


class LifetimeTracker:
    """The lifetime and today totals of one device (see the module docstring)."""

    def __init__(self, state: dict[str, Any], store: LifetimeStore) -> None:
        """Initialize the tracker with a valid state."""
        self.state = state
        self._store = store
        self.running = False
        self._flush_soon = False
        self._warned_ledger = False
        # A retry time further away than the longest back-off is a clock
        # jump or damaged data: do not wait for it.
        limit = dt_util.utcnow() + LIFETIME_RETRY_MAX
        retry_at = self.retry_at
        if retry_at is not None and retry_at > limit:
            state["retry_at"] = limit.isoformat()

    # --- reading -----------------------------------------------------------

    @property
    def complete(self) -> bool:
        """Return whether the backfill has completed."""
        return bool(self.state["complete"])

    @property
    def retry_at(self) -> datetime | None:
        """Return when a failed sync may be tried again, or None."""
        retry_at = self.state["retry_at"]
        return datetime.fromisoformat(retry_at) if retry_at else None

    @property
    def months_included(self) -> int:
        """Return the number of finished months with energy."""
        return len(self.state["months"])

    @property
    def earliest_month(self) -> date | None:
        """Return the first month with energy found, or None."""
        months = self.state["months"]
        return parse_month(min(months)) if months else None

    def _boundary(self, kind: str) -> float:
        """Return the energy of the hours after the open month's end.

        The cloud has published them (the hourly fetch is ahead), but the
        coordinator still shows the open month; once it shows the next one its
        own totals include them.
        """
        open_month = self.state["open_month"]
        end = _month_end(parse_month(open_month)).isoformat()
        return sum(
            totals[kind] for key, totals in self.state["hours"].items() if key >= end
        )

    def _ledger(self, kind: str) -> float:
        state = self.state
        finished = sum(
            totals[kind]
            for month, totals in state["months"].items()
            if month < state["open_month"]
        )
        return finished + state["open"][kind] + self._boundary(kind)

    @property
    def ledger_below_floor(self) -> bool:
        """Return whether the ledger is well below what was published.

        Not an error: the published value stays at the floor until the ledger
        catches up (a cloud revision downwards, or stored months lost).
        """
        state = self.state
        if not self.complete or state["open_month"] is None:
            return False
        return any(
            state["floor"][kind] > 0
            and self._ledger(kind) < state["floor"][kind] * (1 - LEDGER_WARN_RATIO)
            for kind in KINDS
        )

    def _kind_value(self, kind: str) -> float:
        return round(max(self._ledger(kind), self.state["floor"][kind]), 3)

    def lifetime_value(
        self, consumption_type: aioaquarea.ConsumptionType
    ) -> float | None:
        """Return the lifetime energy in kWh, or None until it is known.

        Before the backfill has completed it is unknown, except while the
        history is read again (rebuild): the last published value is kept.
        """
        state = self.state
        kinds = KINDS_BY_TYPE[consumption_type]
        if not self.complete or state["open_month"] is None:
            floors = [round(state["floor"][kind], 3) for kind in kinds]
            return round(sum(floors), 3) if any(floors) else None
        return round(sum(self._kind_value(kind) for kind in kinds), 3)

    def today_value(
        self, consumption_type: aioaquarea.ConsumptionType, now: datetime
    ) -> float | None:
        """Return the energy of the local calendar day of ``now``, or None.

        None when no hourly record is known at all.
        """
        hours = self.state["hours"]
        if not hours:
            return None
        start, end = local_day_bounds(now)
        kinds = KINDS_BY_TYPE[consumption_type]
        return sum(
            totals[kind]
            for key, totals in hours.items()
            if start <= datetime.fromisoformat(key) < end
            for kind in kinds
        )

    # --- observing the coordinator's data ------------------------------------

    def observe_month(self, shown: date, totals: Totals, today: date) -> None:
        """Take the month the coordinator shows, and its totals so far.

        ``today`` is the cloud's (UTC) date.
        """
        state = self.state
        key = month_key(shown)
        changed = False
        open_month = state["open_month"]
        if open_month is not None and open_month > month_key(
            add_months(today.replace(day=1), 1)
        ):
            # A month that has not begun: the stored state is wrong, but the
            # finished months before the shown one are not. Open the shown one.
            _LOGGER.warning(
                "Stored open month %s is in the future; repairing", open_month
            )
            for name in ("months", "pending"):
                for stale in [m for m in state[name] if m >= key]:
                    del state[name][stale]
            state["open_month"] = open_month = None
            changed = True
        if open_month is None:
            state["open_month"] = key
            state["open"] = dict(totals)
            if not state["complete"] and state["cursor"] is None:
                state["cursor"] = month_key(add_months(shown, -1))
            changed = True
        elif key > open_month:
            self._fold(shown, totals)
            self._flush_soon = changed = True
        elif key == open_month and state["open"] != totals:
            # The month is replaced as a whole, as the cloud's list is.
            state["open"] = dict(totals)
            changed = True
        # A month before the open one is a stale cache: ignored.
        if self._refresh_floor() or changed:
            self._store.schedule_save()

    def _fold(self, shown: date, totals: Totals) -> None:
        """Move the open month to the finished months and open ``shown``."""
        state = self.state
        old = parse_month(state["open_month"])
        if any(state["open"].values()):
            existing = state["months"].get(month_key(old), zero_totals())
            state["months"][month_key(old)] = _max_totals(existing, state["open"])
        # The old month, and any month skipped while Home Assistant was off,
        # are looked up once the cloud has had time to publish their last hours.
        month = old
        while month < shown:
            if month_key(month) not in state["pending"]:
                state["pending"][month_key(month)] = (
                    _month_end(month) + LIFETIME_RECONCILE_DELAY
                ).isoformat()
            month = add_months(month, 1)
        state["open_month"] = month_key(shown)
        state["open"] = dict(totals)

    def _refresh_floor(self) -> bool:
        """Raise the stored floor to the current values; return if it changed."""
        state = self.state
        if not self.complete or state["open_month"] is None:
            return False
        changed = False
        for kind in KINDS:
            value = self._kind_value(kind)
            if value > state["floor"][kind] + _EPSILON:
                state["floor"][kind] = value
                changed = True
        if self.ledger_below_floor and not self._warned_ledger:
            self._warned_ledger = True
            _LOGGER.warning(
                "The lifetime consumption recomputed from the cloud's history "
                "is more than %d%% below what was published; keeping the "
                "published value until the history catches up",
                round(LEDGER_WARN_RATIO * 100),
            )
        return changed

    def observe_hours(self, hours: dict[datetime, Totals], now: datetime) -> None:
        """Take the hourly records of a fetch: a known hour is replaced."""
        stored = self.state["hours"]
        changed = False
        for start, totals in hours.items():
            key = start.astimezone(UTC).isoformat()
            if stored.get(key) != totals:
                stored[key] = dict(totals)
                changed = True
        cutoff = (
            (now - LIFETIME_HOURS_KEPT)
            .astimezone(UTC)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )
        for key in [key for key in stored if key < cutoff]:
            del stored[key]
            changed = True
        if changed:
            self._refresh_floor()
            self._store.schedule_save()

    async def async_save_if_needed(self) -> None:
        """Write the state at once after a fold (rare, and worth keeping)."""
        if self._flush_soon:
            self._flush_soon = False
            await self._store.async_flush()

    # --- sync ----------------------------------------------------------------

    def sync_due(self, now: datetime) -> bool:
        """Return whether the backfill or a lookup of a finished month is due."""
        retry_at = self.retry_at
        if retry_at is not None and now < retry_at:
            return False
        state = self.state
        if not self.complete:
            return state["cursor"] is not None
        return any(
            datetime.fromisoformat(not_before) <= now
            for not_before in state["pending"].values()
        )

    def note_failure(self, now: datetime, *, access: bool) -> None:
        """Record a failed sync and set the time of the next attempt.

        ``access`` is an authentication or access failure (for example an HTTP
        403 block): the wait then starts at ``LIFETIME_RETRY_AUTH_INITIAL``
        instead of ``LIFETIME_RETRY_INITIAL``. Both double up to the maximum.
        """
        state = self.state
        state["failures"] += 1
        initial = LIFETIME_RETRY_AUTH_INITIAL if access else LIFETIME_RETRY_INITIAL
        delay = min(initial * 2 ** (state["failures"] - 1), LIFETIME_RETRY_MAX)
        state["retry_at"] = (now + delay).isoformat()
        self._store.schedule_save()

    def note_success(self) -> None:
        """Record a finished sync."""
        state = self.state
        if state["failures"] or state["retry_at"]:
            state["failures"] = 0
            state["retry_at"] = None
            self._store.schedule_save()

    async def async_sync(self, fetch: FetchYear, lock: asyncio.Lock) -> None:
        """Run the backfill if it is not complete, then look up due months.

        ``fetch`` queries one YEAR window and may raise. Every request holds
        ``lock``, which is shared by all devices and entries, and is followed
        by a pause inside it, so requests never overlap and keep their
        spacing. Progress is written after every window, so an exception (a
        block, an authentication failure, a cancellation) leaves a state the
        next run resumes from.
        """
        self.running = True

        async def request(end: date) -> aioaquarea.ConsumptionReport | None:
            async with lock:
                report = await fetch(end)
                await asyncio.sleep(LIFETIME_REQUEST_DELAY)
                return report

        try:
            await asyncio.sleep(LIFETIME_START_DELAY)
            if not self.complete:
                await self._backfill(request)
            if self.complete:
                await self._reconcile(request)
        finally:
            self.running = False

    def _apply_window(
        self, months: dict[str, Totals], keys: set[str] | None, now: datetime
    ) -> None:
        """Store the months of a YEAR response (the larger figure wins).

        ``keys`` limits the months taken (None: all before the open month).
        """
        state = self.state
        previous = month_key(add_months(parse_month(state["open_month"]), -1))
        for key, totals in months.items():
            if (keys is not None and key not in keys) or key >= state["open_month"]:
                continue
            if any(totals.values()):
                existing = state["months"].get(key, zero_totals())
                state["months"][key] = _max_totals(existing, totals)
            # The month before the open one may have been read before the
            # cloud published its last hours: look it up again once.
            not_before = _month_end(parse_month(key)) + LIFETIME_RECONCILE_DELAY
            if key == previous and now < not_before:
                state["pending"].setdefault(key, not_before.isoformat())

    async def _backfill(
        self,
        request: Callable[[date], Awaitable[aioaquarea.ConsumptionReport | None]],
    ) -> None:
        state = self.state
        while not state["complete"]:
            end = parse_month(state["cursor"])
            report = await request(end)
            now = dt_util.utcnow()
            if report is None:
                await self._no_data(now, end)
                return
            state["none_responses"] = 0
            state["none_since"] = None
            months = _buckets_by_month(report)
            registered = _register_month(report.device_register_time)
            if registered is not None:
                state["register_month"] = month_key(registered)
            if months and min(months) >= month_key(end):
                raise LifetimeSyncError(
                    f"Malformed consumption window for {month_key(end)}"
                )
            first = parse_month(min(months)) if months else add_months(end, -23)
            self._apply_window(months, None, now)
            energy = sum(sum(totals.values()) for totals in months.values())
            state["windows"] += 1
            state["empty_windows"] = 0 if energy > 0 else state["empty_windows"] + 1
            state["cursor"] = month_key(add_months(first, -1))
            register = state["register_month"]
            if register is not None:
                reached = month_key(first) <= register
                before = any(
                    key < register and any(totals.values())
                    for key, totals in months.items()
                )
                if reached and not before:
                    self._finish("register")
            elif state["empty_windows"] >= LIFETIME_EMPTY_WINDOWS_STOP:
                self._finish("empty")
            if not state["complete"] and state["windows"] >= MAX_WINDOWS:
                self._finish("cap")
            self._refresh_floor()
            await self._store.async_flush()

    async def _no_data(self, now: datetime, end: date) -> None:
        """Handle a window answered with no data: raise, or end the history.

        The library returns None for "no data" and for network errors, rate
        limits and server errors alike, so this is a failure to retry. Only
        when the registration month never became known, and the answers kept
        coming for days, is it taken as the end of the history.
        """
        state = self.state
        state["none_responses"] += 1
        if state["none_since"] is None:
            state["none_since"] = now.isoformat()
        span = now - datetime.fromisoformat(state["none_since"])
        if (
            state["register_month"] is None
            and state["none_responses"] >= LIFETIME_NO_DATA_MIN_RESPONSES
            and span >= LIFETIME_NO_DATA_MIN_SPAN
        ):
            _LOGGER.warning(
                "No consumption data for %s after %d attempts over %s; "
                "taking it as the start of the history",
                month_key(end),
                state["none_responses"],
                span,
            )
            self._finish("no_data")
            await self._store.async_flush()
            return
        await self._store.async_flush()
        raise LifetimeSyncError(f"No consumption data for {month_key(end)}")

    def _finish(self, reason: str) -> None:
        state = self.state
        state["complete"] = True
        state["ended_by"] = reason
        _LOGGER.debug(
            "Lifetime consumption backfill complete (%s): %d months with energy",
            reason,
            len(state["months"]),
        )

    async def _reconcile(
        self,
        request: Callable[[date], Awaitable[aioaquarea.ConsumptionReport | None]],
    ) -> None:
        """Look up the finished months that are due; the larger figure wins."""
        state = self.state
        for _ in range(MAX_RECONCILE_REQUESTS):
            now = dt_util.utcnow()
            due = {
                key
                for key, not_before in state["pending"].items()
                if datetime.fromisoformat(not_before) <= now
            }
            if not due:
                return
            end = parse_month(max(due))
            report = await request(end)
            if report is None:
                raise LifetimeSyncError(f"No consumption data for {month_key(end)}")
            months = _buckets_by_month(report)
            window = {
                month_key(add_months(end, -offset)) for offset in range(WINDOW_MONTHS)
            }
            self._apply_window(months, due & window, now)
            for key in due & window:
                state["pending"].pop(key, None)
            self._refresh_floor()
            await self._store.async_flush()

    # --- rebuild -------------------------------------------------------------

    async def async_rebuild(
        self,
        shown: date | None,
        totals: Totals | None,
        now: datetime,
        today: date,
    ) -> None:
        """Forget the stored months and start the backfill again.

        The published floor, the hourly records and a back-off that is still
        running are kept: the value never goes back, and the button cannot
        send requests while Panasonic is refusing them. The caller makes sure
        no sync is running.
        """
        state = self.state
        keep = {
            "floor": state["floor"],
            "hours": state["hours"],
        }
        retry_at = self.retry_at
        if retry_at is not None and retry_at > now:
            keep["failures"] = state["failures"]
            keep["retry_at"] = state["retry_at"]
        state.clear()
        state.update(fresh_state())
        state.update(keep)
        self._warned_ledger = False
        if shown is not None and totals is not None:
            self.observe_month(shown, totals, today)
        await self._store.async_flush()


def _register_month(value: str | None) -> date | None:
    """Return the month the unit was registered in (``YYYYMMDD``), or None."""
    if not value or not _valid_month(value[:6]):
        return None
    return parse_month(value[:6])


def local_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return the start of the local calendar day of ``now`` and of the next one.

    Both are aware; between them lie 24 hours, or 23 or 25 on the days the
    clocks change.
    """
    day = dt_util.as_local(now).date()
    return (
        dt_util.start_of_local_day(day),
        dt_util.start_of_local_day(day + timedelta(days=1)),
    )
