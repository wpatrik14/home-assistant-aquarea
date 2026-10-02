"""Tests for the energy consumption sensors' update logic.

Background
----------
Both energy sensor families read the coordinator's cached month list (one
entry per day, `data_time` as `YYYYMMDD` or `YYYY-MM-DD`):

- `EnergyAccumulatedConsumptionSensor` (month to date) sums every entry dated
  today or earlier, per consumption type. Future-dated entries are ignored.
  When `total_consumption` is unusable it falls back to heat + cool + tank.
  An empty or missing list makes the value unknown.
- `EnergyConsumptionSensor` (today) reports today's entry. With no list, or no
  entry for today, it keeps its previous value while that value belongs to
  today; once the value it holds is from an earlier day it drops to 0 for
  the new day. Before that, yesterday's total stayed on the "today" sensor
  until the cloud published an entry for the new day, which can take hours.
  The drop happens only on a day change, never mid-day: these sensors are
  TOTAL_INCREASING, so a transient 0 within a day would be read as a meter
  reset and the day's consumption counted twice in long-term statistics.
  A value with no recorded period (unknown day) is kept as is.

The behaviour was listed as missing coverage, and the stale "today" value as
a bug, in the audit (wpatrik14/fleet-backlog#88). The real `_handle_coordinator_update`
methods are loaded out of sensor.py via AST into classes built on a stub base.

Intentionally dependency-free (stdlib only):

    python3 tests/test_energy_sensors.py
"""
import __future__
import ast
from datetime import datetime, timedelta, timezone
import os
import sys
import types

SENSOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "sensor.py"
)
TZ = timezone(timedelta(hours=1))


class ConsumptionType:
    HEAT = "heat"
    COOL = "cool"
    WATER_TANK = "tank"
    TOTAL = "total"


aioaquarea = types.SimpleNamespace(ConsumptionType=ConsumptionType)


class _Clock:
    now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)


class _Logger:
    def __init__(self):
        self.calls = []

    def warning(self, *args, **kwargs):
        self.calls.append(("warning", args))

    def exception(self, *args, **kwargs):
        self.calls.append(("exception", args))


LOGGER = _Logger()


class _Base:
    def __init__(self, ctype, month):
        self.coordinator = types.SimpleNamespace(month_consumption=month)
        self.entity_description = types.SimpleNamespace(consumption_type=ctype)
        self._attr_native_value = "previous"
        self._period_being_processed = None
        self.writes = 0

    def _handle_coordinator_update(self):
        self.writes += 1


def _load(class_name):
    with open(SENSOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    node = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_coordinator_update"
    )
    node.decorator_list = []
    flow = ast.ClassDef(
        name="Extracted", bases=[ast.Name("_Base", ast.Load())],
        keywords=[], body=[node], decorator_list=[], type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {
        "_Base": _Base,
        "aioaquarea": aioaquarea,
        "datetime": datetime,
        "dt_util": types.SimpleNamespace(now=lambda: _Clock.now),
        "_LOGGER": LOGGER,
    }
    exec(
        compile(
            module, SENSOR, "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


Accumulated = _load("EnergyAccumulatedConsumptionSensor")
Today = _load("EnergyConsumptionSensor")


def _day(data_time, heat=0.0, cool=0.0, tank=0.0, total="sum"):
    if total == "sum":
        total = heat + cool + tank
    return types.SimpleNamespace(
        data_time=data_time, heat_consumption=heat, cool_consumption=cool,
        tank_consumption=tank, total_consumption=total,
    )


MONTH = [
    _day("20261001", heat=1.0, tank=0.5),
    _day("2026-10-13", heat=2.0, cool=0.25, tank=1.0),
    _day("20261014", heat=3.0, tank=2.0),
    _day("20261015", heat=100.0, tank=100.0),  # tomorrow: must be ignored
    _day("", heat=50.0),                         # no date: skipped
]


def _update(cls, ctype, month):
    obj = cls(ctype, month)
    obj._handle_coordinator_update()
    return obj


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)
    month_start = datetime(2026, 10, 1, tzinfo=TZ)
    today_start = datetime(2026, 10, 14, tzinfo=TZ)

    # --- month to date ---------------------------------------------------
    expected = {
        ConsumptionType.HEAT: 6.0,
        ConsumptionType.COOL: 0.25,
        ConsumptionType.WATER_TANK: 3.5,
        ConsumptionType.TOTAL: 9.75,
    }
    for ctype, value in expected.items():
        obj = _update(Accumulated, ctype, MONTH)
        check(f"accumulated {ctype}: sums days up to today (both formats)",
              obj._attr_native_value == value
              and obj._period_being_processed == month_start and obj.writes == 1,
              f"value={obj._attr_native_value} period={obj._period_being_processed}")

    obj = _update(Accumulated, ConsumptionType.TOTAL,
                  [_day("20261014", heat=1.0, cool=2.0, tank=3.0, total="n/a")])
    check("accumulated total: falls back to heat+cool+tank",
          obj._attr_native_value == 6.0, f"value={obj._attr_native_value}")

    for label, month in (("empty list", []), ("no list", None)):
        obj = _update(Accumulated, ConsumptionType.HEAT, month)
        check(f"accumulated with {label}: unknown, state written",
              obj._attr_native_value is None and obj.writes == 1,
              f"value={obj._attr_native_value!r}")

    LOGGER.calls.clear()
    obj = _update(Accumulated, ConsumptionType.HEAT,
                  [_day("14/10/2026", heat=9.0), _day("20261014", heat=1.0)])
    check("accumulated: unparseable date skipped with a warning",
          obj._attr_native_value == 1.0 and LOGGER.calls
          and LOGGER.calls[0][0] == "warning",
          f"value={obj._attr_native_value} log={LOGGER.calls}")

    # Day 1 of a new month with last month's list still cached: every entry
    # is "today or earlier", so the value is last month's total until the
    # cache is refetched (no drop in between).
    _Clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=TZ)
    obj = _update(Accumulated, ConsumptionType.HEAT, MONTH[:3])
    check("accumulated, new month with stale cache: last month's sum",
          obj._attr_native_value == 6.0
          and obj._period_being_processed == datetime(2026, 11, 1, tzinfo=TZ),
          f"value={obj._attr_native_value} period={obj._period_being_processed}")
    obj = _update(Accumulated, ConsumptionType.HEAT, [_day("20261101", heat=0.5)])
    check("accumulated, new month after refetch: new month's sum",
          obj._attr_native_value == 0.5, f"value={obj._attr_native_value}")
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    # --- today -----------------------------------------------------------
    expected = {
        ConsumptionType.HEAT: 3.0,
        ConsumptionType.COOL: 0.0,
        ConsumptionType.WATER_TANK: 2.0,
        ConsumptionType.TOTAL: 5.0,
    }
    for ctype, value in expected.items():
        obj = _update(Today, ctype, MONTH)
        check(f"today {ctype}: today's entry",
              obj._attr_native_value == value
              and obj._period_being_processed == today_start and obj.writes == 1,
              f"value={obj._attr_native_value} period={obj._period_being_processed}")

    obj = _update(Today, ConsumptionType.TOTAL,
                  [_day("2026-10-14", heat=1.0, cool=0.5, tank=0.25, total="n/a")])
    check("today total: falls back to heat+cool+tank",
          obj._attr_native_value == 1.75, f"value={obj._attr_native_value}")

    yesterday_start = datetime(2026, 10, 13, tzinfo=TZ)
    for label, month in (("no list", None), ("no entry for today", MONTH[:2])):
        for period, expected_value, expected_period, why in (
            (None, "previous", None, "unknown day: keeps previous value"),
            (today_start, "previous", today_start, "value is today's: keeps it"),
            (yesterday_start, 0, today_start, "value is yesterday's: 0 for today"),
        ):
            obj = Today(ConsumptionType.HEAT, month)
            obj._period_being_processed = period
            obj._handle_coordinator_update()
            check(f"today with {label}, {why}",
                  obj._attr_native_value == expected_value
                  and obj._period_being_processed == expected_period
                  and obj.writes == 1,
                  f"value={obj._attr_native_value!r} "
                  f"period={obj._period_being_processed}")

    # The day after, before the cloud has an entry for it: yesterday's value
    # (written at 23:55) must not carry over past midnight.
    _Clock.now = datetime(2026, 10, 14, 23, 55, tzinfo=TZ)
    obj = _update(Today, ConsumptionType.HEAT, MONTH[:3])
    _Clock.now = datetime(2026, 10, 15, 0, 5, tzinfo=TZ)
    obj.coordinator.month_consumption = MONTH[:3]
    obj._handle_coordinator_update()
    check("today across midnight with no new entry: 0 for the new day",
          obj._attr_native_value == 0
          and obj._period_being_processed == datetime(2026, 10, 15, tzinfo=TZ),
          f"value={obj._attr_native_value!r} period={obj._period_being_processed}")
    obj.coordinator.month_consumption = [*MONTH[:3], _day("20261015", heat=0.75)]
    obj._handle_coordinator_update()
    check("today, entry for the new day appears: its value",
          obj._attr_native_value == 0.75, f"value={obj._attr_native_value!r}")
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
