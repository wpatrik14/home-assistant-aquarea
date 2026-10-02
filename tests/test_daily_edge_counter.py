"""Tests for `DailyEdgeCounterSensor` (DHW / zone / defrost cycles today).

Background
----------
The sensor counts low-to-high transitions of a detector over the coordinator
updates and resets at local midnight. It restores count, last reset and the
last detector state after a restart, so a restart neither loses the day's
count nor counts an edge that was already counted. A restored count from a
previous day is reset on the first update of the new day.

These describe current behaviour; they were listed as missing coverage in the
audit (wpatrik14/fleet-backlog#88). The real `async_added_to_hass` and
`_handle_coordinator_update` are loaded out of sensor.py via AST into a class
built on a stub base.

Intentionally dependency-free (stdlib only):

    python3 tests/test_daily_edge_counter.py
"""
import __future__
import ast
import asyncio
from datetime import date, datetime, timedelta, timezone
import os
import sys
import types

SENSOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "sensor.py"
)
TZ = timezone(timedelta(hours=2))


class _Clock:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)


dt_util = types.SimpleNamespace(now=lambda: _Clock.now, as_local=lambda d: d)


class _Base:
    restored = None

    def __init__(self):
        self.coordinator = types.SimpleNamespace(device=object())
        self.writes = 0
        self._attr_unique_id = "dev_dhw_cycles_today"
        self._last_state = False
        self._attr_last_reset = None
        self._attr_native_value = 0
        self.detector_values = []
        self._detector = lambda device: self.detector_values.pop(0)

    async def async_added_to_hass(self):
        pass

    async def async_get_last_sensor_data(self):
        return self.restored

    def _handle_coordinator_update(self):
        self.writes += 1


def _load():
    with open(SENSOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "DailyEdgeCounterSensor"
    )
    wanted = ("async_added_to_hass", "_handle_coordinator_update")
    methods = [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted
    ]
    for m in methods:
        m.decorator_list = []
    flow = ast.ClassDef(
        name="Extracted", bases=[ast.Name("_Base", ast.Load())],
        keywords=[], body=methods, decorator_list=[], type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {
        "_Base": _Base,
        "dt_util": dt_util,
        "date": date,
        "datetime": datetime,
        "_LOGGER": types.SimpleNamespace(debug=lambda *a, **k: None),
    }
    exec(
        compile(
            module, SENSOR, "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


Extracted = _load()


def _restored(value, last_reset, last_state):
    return types.SimpleNamespace(
        native_value=value, last_reset=last_reset, last_state=last_state
    )


def _added(restored=None):
    obj = Extracted()
    obj.restored = restored
    asyncio.run(obj.async_added_to_hass())
    return obj


def _feed(obj, *values):
    obj.detector_values.extend(values)
    for _ in values:
        obj._handle_coordinator_update()


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    midnight = datetime(2026, 10, 14, 0, 0, tzinfo=TZ)
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    # Fresh entity: starts at 0 with today's midnight as last reset.
    obj = _added()
    check("fresh: value 0, last reset today's midnight",
          obj._attr_native_value == 0 and obj._attr_last_reset == midnight,
          f"value={obj._attr_native_value} reset={obj._attr_last_reset}")

    # Counts rising edges only.
    _feed(obj, False, True, True, False, True, False)
    check("counts rising edges only", obj._attr_native_value == 2,
          f"value={obj._attr_native_value}")

    # Restore within the same day keeps the count and the last state, so an
    # edge that was in progress at restart is not counted twice.
    obj = _added(_restored(3, midnight, True))
    _feed(obj, True)
    check("restore same day: count and last state kept",
          obj._attr_native_value == 3 and obj._last_state is True,
          f"value={obj._attr_native_value}")
    _feed(obj, False, True)
    check("restore same day: next edge counted", obj._attr_native_value == 4,
          f"value={obj._attr_native_value}")

    # Restore from yesterday: first update of the day resets.
    yesterday = midnight - timedelta(days=1)
    obj = _added(_restored(7, yesterday, False))
    _feed(obj, False)
    check("restore from yesterday: reset on first update",
          obj._attr_native_value == 0 and obj._attr_last_reset == midnight,
          f"value={obj._attr_native_value} reset={obj._attr_last_reset}")

    # Unusable restored values fall back to 0.
    for label, value in (("None", None), ("a date", date(2026, 10, 14)),
                         ("a non-number", "abc")):
        obj = _added(_restored(value, midnight, False))
        check(f"restored {label} -> 0", obj._attr_native_value == 0,
              f"value={obj._attr_native_value!r}")

    # Midnight rollover while running.
    _Clock.now = datetime(2026, 10, 14, 23, 59, tzinfo=TZ)
    obj = _added()
    _feed(obj, True, False, True)
    check("before midnight: 2 cycles", obj._attr_native_value == 2,
          f"value={obj._attr_native_value}")
    _Clock.now = datetime(2026, 10, 15, 0, 1, tzinfo=TZ)
    _feed(obj, True)
    check("after midnight: reset, an ongoing cycle is not re-counted",
          obj._attr_native_value == 0
          and obj._attr_last_reset == datetime(2026, 10, 15, 0, 0, tzinfo=TZ),
          f"value={obj._attr_native_value} reset={obj._attr_last_reset}")
    _feed(obj, False, True)
    check("after midnight: new cycle counted", obj._attr_native_value == 1,
          f"value={obj._attr_native_value}")

    # A detector failure skips counting but still writes state.
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)
    obj = _added()

    def _broken(device):
        raise AttributeError("no direction")

    obj._detector = _broken
    obj._handle_coordinator_update()
    check("detector failure: no count, state still written",
          obj._attr_native_value == 0 and obj.writes == 1,
          f"value={obj._attr_native_value} writes={obj.writes}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
