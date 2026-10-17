"""Tests for the energy consumption sensors' update logic.

Background
----------
Both energy sensor families read the coordinator's cached month list (one
entry per day, `data_time` as `YYYYMMDD` or `YYYY-MM-DD`). The cloud labels
days by the UTC date, so "today" below is the UTC date and the clock is UTC
(the local-timezone boundary is covered by tests/ha/test_energy.py):

- `EnergyAccumulatedConsumptionSensor` (month to date) sums every entry dated
  today or earlier, per consumption type. Future-dated entries are ignored.
  When `total_consumption` is unusable it falls back to heat + cool + tank.
  An empty or missing list makes the value unknown.
- `EnergyConsumptionSensor` (current hour) reports the energy of the latest
  hourly record, the one labelled with the current UTC hour, and 0 when the
  cloud has no record for that hour (or no hourly records at all). It drops
  back to 0 when the hour changes. The total falls back like
  `statistics._consumption_value`. It is a MEASUREMENT sensor without state.

The behaviour was listed as missing coverage, and the stale "today" value as
a bug, in the audit (wpatrik14/fleet-backlog#88). The real `_handle_coordinator_update`
methods are loaded out of sensor.py via AST into classes built on a stub base.

Intentionally dependency-free (stdlib only):

    python3 tests/test_energy_sensors.py
"""

import __future__

import ast
from datetime import UTC, datetime, time
import os
import sys
import types

SENSOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "sensor.py"
)
STATISTICS = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "statistics.py"
)
TZ = UTC


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
    def __init__(self, ctype, month, hourly=None):
        self.coordinator = types.SimpleNamespace(
            month_consumption=month, hourly_consumption=hourly
        )
        self.entity_description = types.SimpleNamespace(consumption_type=ctype)
        self._attr_native_value = "previous"
        self._period_being_processed = None
        self.writes = 0

    def _handle_coordinator_update(self):
        self.writes += 1


def _statistics_hourly_consumption():
    """Load statistics.hourly_consumption (and its helpers) out of statistics.py."""
    with open(STATISTICS, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = {"_kwh", "_consumption_value", "hourly_consumption"}
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    namespace = {
        "aioaquarea": aioaquarea,
        "datetime": datetime,
        "CLOUD_TIME_ZONE": UTC,
        "_LOGGER": types.SimpleNamespace(debug=lambda *a, **k: None),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body, [])),
            STATISTICS,
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        ),
        namespace,
    )
    return namespace["hourly_consumption"]


def _load(class_name):
    with open(SENSOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    node = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_coordinator_update"
    )
    node.decorator_list = []
    base = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaEnergySensor"
    )
    ahead = next(
        n
        for n in base.body
        if isinstance(n, ast.FunctionDef) and n.name == "_ahead_of_the_cloud"
    )
    flow = ast.ClassDef(
        name="Extracted",
        bases=[ast.Name("_Base", ast.Load())],
        keywords=[],
        body=[node, ahead],
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {
        "_Base": _Base,
        "aioaquarea": aioaquarea,
        "datetime": datetime,
        "dt_util": types.SimpleNamespace(
            utcnow=lambda: _Clock.now, as_local=lambda moment: moment
        ),
        "cloud_date": lambda moment: moment.astimezone(UTC).date(),
        "time": time,
        "CLOUD_TIME_ZONE": UTC,
        "_LOGGER": LOGGER,
        "hourly_consumption": _statistics_hourly_consumption(),
    }
    exec(
        compile(
            module,
            SENSOR,
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
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
        data_time=data_time,
        heat_consumption=heat,
        cool_consumption=cool,
        tank_consumption=tank,
        total_consumption=total,
    )


MONTH = [
    _day("20261001", heat=1.0, tank=0.5),
    _day("2026-10-13", heat=2.0, cool=0.25, tank=1.0),
    _day("20261014", heat=3.0, tank=2.0),
    _day("20261015", heat=100.0, tank=100.0),  # tomorrow: must be ignored
    _day("", heat=50.0),  # no date: skipped
]


def _hour(label, heat=0.0, cool=0.0, tank=0.0, total="sum"):
    return _day(label, heat=heat, cool=cool, tank=tank, total=total)


def _hourly(ctype, hours):
    obj = Today(ctype, MONTH, hours)
    obj._handle_coordinator_update()
    return obj


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

    # --- month to date ---------------------------------------------------
    expected = {
        ConsumptionType.HEAT: 6.0,
        ConsumptionType.COOL: 0.25,
        ConsumptionType.WATER_TANK: 3.5,
        ConsumptionType.TOTAL: 9.75,
    }
    for ctype, value in expected.items():
        obj = _update(Accumulated, ctype, MONTH)
        check(
            f"accumulated {ctype}: sums days up to today (both formats)",
            obj._attr_native_value == value
            and obj._period_being_processed == month_start
            and obj.writes == 1,
            f"value={obj._attr_native_value} period={obj._period_being_processed}",
        )

    obj = _update(
        Accumulated,
        ConsumptionType.TOTAL,
        [_day("20261014", heat=1.0, cool=2.0, tank=3.0, total="n/a")],
    )
    check(
        "accumulated total: falls back to heat+cool+tank",
        obj._attr_native_value == 6.0,
        f"value={obj._attr_native_value}",
    )

    for label, month in (("empty list", []), ("no list", None)):
        obj = _update(Accumulated, ConsumptionType.HEAT, month)
        check(
            f"accumulated with {label}: unknown, state written",
            obj._attr_native_value is None and obj.writes == 1,
            f"value={obj._attr_native_value!r}",
        )

    LOGGER.calls.clear()
    obj = _update(
        Accumulated,
        ConsumptionType.HEAT,
        [_day("14/10/2026", heat=9.0), _day("20261014", heat=1.0)],
    )
    check(
        "accumulated: unparseable date skipped with a warning",
        obj._attr_native_value == 1.0
        and LOGGER.calls
        and LOGGER.calls[0][0] == "warning",
        f"value={obj._attr_native_value} log={LOGGER.calls}",
    )

    # Day 1 of a new month with last month's list still cached: every entry
    # is "today or earlier", so the value is last month's total until the
    # cache is refetched (no drop in between).
    _Clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=TZ)
    obj = _update(Accumulated, ConsumptionType.HEAT, MONTH[:3])
    check(
        "accumulated, new month with stale cache: last month's sum",
        obj._attr_native_value == 6.0
        and obj._period_being_processed == datetime(2026, 11, 1, tzinfo=TZ),
        f"value={obj._attr_native_value} period={obj._period_being_processed}",
    )
    obj = _update(Accumulated, ConsumptionType.HEAT, [_day("20261101", heat=0.5)])
    check(
        "accumulated, new month after refetch: new month's sum",
        obj._attr_native_value == 0.5,
        f"value={obj._attr_native_value}",
    )
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    # --- current hour ----------------------------------------------------
    HOURS = [
        _hour("20261014 10", heat=9.0, tank=9.0),  # earlier hour: ignored
        _hour("20261014 12", heat=0.75, cool=0.125, tank=0.5),
        _hour("20261014 13", heat=7.0, tank=7.0),  # next hour: ignored
        _hour("20261013 12", heat=8.0, tank=8.0),  # same hour, other day
    ]
    expected = {
        ConsumptionType.HEAT: 0.75,
        ConsumptionType.COOL: 0.125,
        ConsumptionType.WATER_TANK: 0.5,
        ConsumptionType.TOTAL: 1.375,
    }
    for ctype, value in expected.items():
        obj = _hourly(ctype, HOURS)
        check(
            f"hour {ctype}: the current hour's record only",
            obj._attr_native_value == value and obj.writes == 1,
            f"value={obj._attr_native_value}",
        )

    # The minute inside the hour doesn't matter.
    _Clock.now = datetime(2026, 10, 14, 12, 59, 59, tzinfo=TZ)
    obj = _hourly(ConsumptionType.HEAT, HOURS)
    check(
        "hour: late in the hour still reads that hour",
        obj._attr_native_value == 0.75,
        f"value={obj._attr_native_value}",
    )
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    for label, hours in (("no records", []), ("no list", None)):
        obj = _hourly(ConsumptionType.HEAT, hours)
        check(
            f"hour with {label}: 0, state written",
            obj._attr_native_value == 0.0 and obj.writes == 1,
            f"value={obj._attr_native_value!r}",
        )

    obj = _hourly(ConsumptionType.HEAT, HOURS[:1] + HOURS[2:])
    check(
        "hour with no record for the current hour: 0",
        obj._attr_native_value == 0.0,
        f"value={obj._attr_native_value!r}",
    )

    obj = _hourly(ConsumptionType.HEAT, [_hour("20261014 12", heat=0.0)])
    check(
        "hour with a record of 0 kWh: 0",
        obj._attr_native_value == 0.0,
        f"value={obj._attr_native_value!r}",
    )

    obj = _hourly(
        ConsumptionType.TOTAL,
        [_hour("20261014 12", heat=1.0, cool=0.5, tank=0.25, total="n/a")],
    )
    check(
        "hour total: falls back to heat+cool+tank",
        obj._attr_native_value == 1.75,
        f"value={obj._attr_native_value!r}",
    )

    # The hour changes without a new fetch: back to 0 at the next update.
    obj = _hourly(ConsumptionType.HEAT, HOURS)
    _Clock.now = datetime(2026, 10, 14, 12, 59, tzinfo=TZ)
    obj._handle_coordinator_update()
    still = obj._attr_native_value
    _Clock.now = datetime(2026, 10, 14, 14, 0, tzinfo=TZ)
    obj._handle_coordinator_update()
    check(
        "hour rolls over to 0 when the next hour has no record yet",
        still == 0.75 and obj._attr_native_value == 0.0,
        f"before={still} after={obj._attr_native_value!r}",
    )
    _Clock.now = datetime(2026, 10, 14, 13, 5, tzinfo=TZ)
    obj._handle_coordinator_update()
    check(
        "hour rolls over to the next hour's record when there is one",
        obj._attr_native_value == 7.0,
        f"value={obj._attr_native_value!r}",
    )
    _Clock.now = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
