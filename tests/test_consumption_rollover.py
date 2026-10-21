"""Regression tests: which cloud month the consumption cache holds, and when.

Background
----------
`_async_update_data` caches the month's consumption list for the
month-to-date sensors and refetches it every `consumption_interval` minutes
(default 15), and as soon as the cloud's date changes. The month is the
cloud's, a UTC one (aioaquarea sends `osTimezone: +00:00`): requesting the
local month dropped the cloud month's last hours.

The cloud publishes an hour about an hour after it starts, so a month's last
hour appears only after the month has ended. Moving to the new month at the
cloud's midnight lost it. The old month now stays until it has been fetched
YESTERDAY_REFETCH_HOURS (3) into the new month: that fetch is due at once,
and the new month is fetched in the next hour, so the recorder files the two
in different hours. A failed fetch is retried on the next poll, keeping the
cache.

The real `_async_update_data` and `_monthly_month_due` are loaded out of
coordinator.py via AST and driven with a stub client and a controllable
clock, UTC unless stated (the last cases run on a CET clock).

Intentionally dependency-free (stdlib only):

    python3 tests/test_consumption_rollover.py
"""

import __future__

import ast
import asyncio
from datetime import UTC, datetime, time, timedelta, timezone
import os
import sys
import types

COORDINATOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "coordinator.py"
)


class ClientError(Exception):
    pass


class AuthenticationError(ClientError):
    error_code = None


aioaquarea = types.SimpleNamespace(
    ClientError=ClientError,
    AuthenticationError=AuthenticationError,
    AuthenticationErrorCodes=types.SimpleNamespace(
        INVALID_USERNAME_OR_PASSWORD="1", INVALID_CREDENTIALS="2"
    ),
)


class _Device:
    long_id = "LONG"

    async def refresh_data(self):
        pass


class _Client:
    is_logged = True

    def __init__(self):
        self.consumption_calls = []
        self.fail = False

    async def get_device(self, **kwargs):
        return _Device()

    async def get_device_consumption(self, long_id, date_type, date_str):
        self.consumption_calls.append(date_str)
        if self.fail:
            raise ClientError("cloud down")
        return [date_str]


class _Clock:
    now = None


def _load():
    with open(COORDINATOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaDataUpdateCoordinator"
    )
    node = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_update_data"
    )
    due = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_monthly_month_due"
    )
    # The hourly (DAY) consumption fetch is covered by the pytest suite
    # (tests/ha/test_hourly_statistics.py); stub it out here.
    hourly_stub = ast.parse(
        "async def _async_fetch_hourly_consumption(self, now): pass"
    ).body[0]
    # Same for the weekly schedule read (tests/ha/test_weekly_timer.py).
    weekly_stub = ast.parse(
        "async def _async_fetch_weekly_timer(self, now): pass"
    ).body[0]
    flow = ast.ClassDef(
        name="Extracted",
        bases=[],
        keywords=[],
        body=[node, due, hourly_stub, weekly_stub],
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {
        "aioaquarea": aioaquarea,
        "UpdateFailed": type("UpdateFailed", (Exception,), {}),
        "ConfigEntryAuthFailed": type("ConfigEntryAuthFailed", (Exception,), {}),
        "dt_util": types.SimpleNamespace(
            now=lambda: _Clock.now, get_time_zone=lambda *_: None
        ),
        "timedelta": timedelta,
        "cloud_date": lambda moment: moment.astimezone(UTC).date(),
        "datetime": datetime,
        "time": time,
        "CLOUD_TIME_ZONE": UTC,
        "YESTERDAY_REFETCH_HOURS": 3,
        "DateType": types.SimpleNamespace(MONTH="month"),
        "_LOGGER": types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None
        ),
    }
    exec(
        compile(
            module,
            COORDINATOR,
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


Extracted = _load()
TZ = UTC
CET = timezone(timedelta(hours=1))


def _coordinator(interval=60):
    obj = Extracted()
    obj._client = _Client()
    obj._device_info = None
    obj._month_consumption = None
    obj._last_monthly_fetch_time = None
    obj._consumption_month = None
    obj.consumption_interval = interval
    obj.hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"))
    return obj


def _poll(obj, *args, tz=TZ):
    _Clock.now = datetime(*args, tzinfo=tz)
    asyncio.run(obj._async_update_data())


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    def shown(obj):
        return str(obj._consumption_month)

    # Same day, interval not yet elapsed: served from cache.
    obj = _coordinator()
    _poll(obj, 2026, 10, 14, 12, 0)
    _poll(obj, 2026, 10, 14, 12, 30)
    calls = obj._client.consumption_calls
    check(
        "same day within interval -> one fetch", calls == ["20261001"], f"calls={calls}"
    )

    # Interval elapsed: refetched.
    _poll(obj, 2026, 10, 14, 13, 0)
    check("interval elapsed -> refetch", len(calls) == 2, f"calls={calls}")

    # The cloud's midnight within a month: refetched for the new day's entry.
    obj = _coordinator()
    _poll(obj, 2026, 10, 14, 23, 50)
    _poll(obj, 2026, 10, 15, 0, 1)
    calls = obj._client.consumption_calls
    check(
        "day rollover -> refetch, same month",
        calls == ["20261001", "20261001"] and shown(obj) == "2026-10-01",
        f"calls={calls} shown={shown(obj)}",
    )

    # The cloud's midnight on the 1st: October stays, its last hour is still
    # to come. Its final fetch at 03:00, November in the next hour.
    obj = _coordinator()
    _poll(obj, 2026, 10, 31, 23, 55)
    _poll(obj, 2026, 11, 1, 0, 1)
    calls = obj._client.consumption_calls
    check(
        "cloud's midnight on the 1st -> the old month again",
        calls == ["20261001", "20261001"] and shown(obj) == "2026-10-01",
        f"calls={calls} shown={shown(obj)}",
    )
    _poll(obj, 2026, 11, 1, 3, 0)
    check(
        "03:00 -> the old month's final fetch",
        calls == ["20261001", "20261001", "20261001"],
        f"calls={calls}",
    )
    _poll(obj, 2026, 11, 1, 3, 59)
    check(
        "same hour -> no fetch, still the old month",
        len(calls) == 3 and shown(obj) == "2026-10-01",
        f"calls={calls} shown={shown(obj)}",
    )
    _poll(obj, 2026, 11, 1, 4, 0)
    check(
        "next hour -> the new month",
        calls == ["20261001", "20261001", "20261001", "20261101"]
        and obj._month_consumption == ["20261101"]
        and shown(obj) == "2026-11-01",
        f"calls={calls} shown={shown(obj)}",
    )
    _poll(obj, 2026, 11, 1, 4, 1)
    check("no extra fetch after the switch", len(calls) == 4, f"calls={calls}")

    # Year rollover.
    obj = _coordinator()
    _poll(obj, 2026, 12, 31, 23, 59)
    _poll(obj, 2027, 1, 1, 3, 0)
    _poll(obj, 2027, 1, 1, 4, 0)
    calls = obj._client.consumption_calls
    check(
        "year rollover -> final fetch, then the new year",
        calls == ["20261201", "20261201", "20270101"],
        f"calls={calls}",
    )

    # A start between the cloud's midnight and 03:00 on the 1st begins with
    # the previous month.
    obj = _coordinator()
    _poll(obj, 2026, 11, 1, 1, 0)
    _poll(obj, 2026, 11, 1, 3, 0)
    _poll(obj, 2026, 11, 1, 4, 0)
    calls = obj._client.consumption_calls
    check(
        "start at 01:00 on the 1st -> the old month, its final fetch, the new",
        calls == ["20261001", "20261001", "20261101"],
        f"calls={calls}",
    )

    # A long interval still gets the old month's final fetch at 03:00.
    obj = _coordinator(interval=180)
    _poll(obj, 2026, 11, 1, 0, 30)
    _poll(obj, 2026, 11, 1, 3, 0)
    _poll(obj, 2026, 11, 1, 4, 0)
    calls = obj._client.consumption_calls
    check(
        "180-minute interval -> final fetch at 03:00",
        calls == ["20261001", "20261001", "20261101"],
        f"calls={calls}",
    )

    # A failed final fetch is retried on the next poll, the old cache kept
    # meanwhile (the sensors keep their value instead of dropping to
    # unknown); only a successful one lets the month move on.
    obj = _coordinator()
    _poll(obj, 2026, 10, 31, 23, 55)
    obj._client.fail = True
    _poll(obj, 2026, 11, 1, 3, 0)
    check(
        "failed final fetch keeps the cache and the month",
        obj._month_consumption == ["20261001"] and shown(obj) == "2026-10-01",
        f"cache={obj._month_consumption} shown={shown(obj)}",
    )
    obj._client.fail = False
    _poll(obj, 2026, 11, 1, 3, 1)
    _poll(obj, 2026, 11, 1, 4, 0)
    calls = obj._client.consumption_calls
    check(
        "retried final fetch, then the new month",
        calls == ["20261001", "20261001", "20261001", "20261101"]
        and shown(obj) == "2026-11-01",
        f"calls={calls} shown={shown(obj)}",
    )

    # CET: the local month starts at 00:00, the cloud's at 01:00, and October
    # is complete at 04:00. Local midnight doesn't refetch; the cloud's
    # refetches October (a 120-minute interval, so nothing else falls due).
    obj = _coordinator(interval=120)
    _poll(obj, 2026, 10, 31, 23, 55, tz=CET)
    _poll(obj, 2026, 11, 1, 0, 30, tz=CET)
    calls = obj._client.consumption_calls
    check(
        "local midnight before the cloud's -> no refetch",
        calls == ["20261001"],
        f"calls={calls}",
    )
    _poll(obj, 2026, 11, 1, 1, 1, tz=CET)
    check(
        "the cloud's midnight -> October again, not November",
        calls == ["20261001", "20261001"],
        f"calls={calls}",
    )
    _poll(obj, 2026, 11, 1, 4, 0, tz=CET)
    _poll(obj, 2026, 11, 1, 5, 0, tz=CET)
    check(
        "04:00 CET -> October's final fetch, 05:00 November",
        calls == ["20261001", "20261001", "20261001", "20261101"],
        f"calls={calls}",
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
