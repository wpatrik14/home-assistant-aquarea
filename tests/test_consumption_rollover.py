"""Regression tests: the consumption cache is refetched when the cloud's date changes.

Background
----------
`_async_update_data` caches the month's consumption list and refetches it only
once `consumption_interval` minutes (default 60) have passed. Both energy
sensor families read that one list: the "today" sensors look up today's entry
and the accumulated sensors sum every entry up to today. After midnight the
cache still held the data fetched yesterday, so for up to an hour the "today"
sensors kept showing yesterday's value. At a month boundary it was worse: the
cache was last month's list, fetched with last month's `YYYYMM01` date.

The cache is now also refetched as soon as the date differs from the date of
the last successful fetch, which covers day, month and year rollover. The date
and the requested month are the cloud's, which are UTC ones (aioaquarea sends
`osTimezone: +00:00`): most clocks below are UTC, and the last cases run on a
CET clock, where the local month starts an hour before the cloud's.

The real `_async_update_data` is loaded out of coordinator.py via AST and
driven with a stub client and a controllable clock.

Intentionally dependency-free (stdlib only):

    python3 tests/test_consumption_rollover.py
"""

import __future__

import ast
import asyncio
from datetime import UTC, datetime, timedelta, timezone
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
    # The hourly (DAY) consumption fetch is covered by the pytest suite
    # (tests/ha/test_hourly_statistics.py); stub it out here.
    hourly_stub = ast.parse(
        "async def _async_fetch_hourly_consumption(self, now): pass"
    ).body[0]
    flow = ast.ClassDef(
        name="Extracted",
        bases=[],
        keywords=[],
        body=[node, hourly_stub],
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

    # Same day, interval not yet elapsed: served from cache.
    obj = _coordinator()
    _poll(obj, 2026, 10, 14, 12, 0)
    _poll(obj, 2026, 10, 14, 12, 30)
    calls = obj._client.consumption_calls
    check(
        "same day within interval -> one fetch", calls == ["20261001"], f"calls={calls}"
    )

    # Interval elapsed: refetched (unchanged behaviour).
    _poll(obj, 2026, 10, 14, 13, 0)
    check("interval elapsed -> refetch", len(calls) == 2, f"calls={calls}")

    # Day rollover inside the interval: refetched right after midnight.
    obj = _coordinator()
    _poll(obj, 2026, 10, 14, 23, 50)
    _poll(obj, 2026, 10, 15, 0, 1)
    calls = obj._client.consumption_calls
    check(
        "day rollover -> refetch", calls == ["20261001", "20261001"], f"calls={calls}"
    )
    _poll(obj, 2026, 10, 15, 0, 2)
    check("no extra fetch after the rollover fetch", len(calls) == 2, f"calls={calls}")

    # Month rollover: refetched with the new month's date string, and the
    # cache holds the new month's data.
    obj = _coordinator()
    _poll(obj, 2026, 10, 31, 23, 55)
    _poll(obj, 2026, 11, 1, 0, 1)
    calls = obj._client.consumption_calls
    check(
        "month rollover -> refetch for the new month",
        calls == ["20261001", "20261101"],
        f"calls={calls}",
    )
    check(
        "cache holds the new month",
        obj._month_consumption == ["20261101"],
        f"cache={obj._month_consumption}",
    )

    # Year rollover.
    obj = _coordinator()
    _poll(obj, 2026, 12, 31, 23, 59)
    _poll(obj, 2027, 1, 1, 0, 0)
    calls = obj._client.consumption_calls
    check(
        "year rollover -> refetch", calls == ["20261201", "20270101"], f"calls={calls}"
    )

    # A failed rollover fetch is retried on the next tick, and the old cache is
    # kept meanwhile (accumulated sensors keep their value instead of dropping
    # to unknown).
    obj = _coordinator()
    _poll(obj, 2026, 10, 31, 23, 55)
    obj._client.fail = True
    _poll(obj, 2026, 11, 1, 0, 1)
    check(
        "failed fetch keeps the previous cache",
        obj._month_consumption == ["20261001"],
        f"cache={obj._month_consumption}",
    )
    obj._client.fail = False
    _poll(obj, 2026, 11, 1, 0, 2)
    calls = obj._client.consumption_calls
    check(
        "failed fetch retried next tick",
        calls == ["20261001", "20261101", "20261101"]
        and obj._month_consumption == ["20261101"],
        f"calls={calls}",
    )

    # CET: the local month starts at 00:00, the cloud's at 01:00. The cloud's
    # October is still running, so local midnight neither refetches nor asks
    # for November, whose first hour the cloud has not even started; asking
    # for it dropped October's last hour.
    obj = _coordinator()
    _poll(obj, 2026, 10, 31, 23, 55, tz=CET)
    _poll(obj, 2026, 11, 1, 0, 30, tz=CET)
    calls = obj._client.consumption_calls
    check(
        "local midnight before the cloud's -> no refetch",
        calls == ["20261001"],
        f"calls={calls}",
    )
    # 00:59 is past the 60-minute interval since 23:55.
    _poll(obj, 2026, 11, 1, 0, 59, tz=CET)
    check(
        "interval refetch before the cloud's midnight -> the cloud's month",
        calls == ["20261001", "20261001"],
        f"calls={calls}",
    )
    _poll(obj, 2026, 11, 1, 1, 1, tz=CET)
    check(
        "the cloud's midnight -> refetch for the new month",
        calls == ["20261001", "20261001", "20261101"],
        f"calls={calls}",
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
