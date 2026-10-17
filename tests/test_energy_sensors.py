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
- `EnergyConsumptionSensor` reports the energy the cloud published since the
  previous hourly consumption fetch: the rise of the total over all cached
  hourly records, recomputed only when `hourly_consumption_fetched_at`
  changes (not on every 60 s state refresh), kept until the next fetch, 0
  when nothing new came. The first fetch only sets the baseline (0), and a
  shrinking total (a day left the cache) reads 0. The total falls back like
  `statistics._consumption_value`.

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
    _seen_fetch = None
    _seen_total = None

    def __init__(self, ctype, month, hourly=None, fetched_at=None):
        self.coordinator = types.SimpleNamespace(
            month_consumption=month,
            hourly_consumption=hourly,
            hourly_consumption_fetched_at=fetched_at,
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
    extra = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_observe_fetch"
    ]
    ahead = next(
        n
        for n in base.body
        if isinstance(n, ast.FunctionDef) and n.name == "_ahead_of_the_cloud"
    )
    flow = ast.ClassDef(
        name="Extracted",
        bases=[ast.Name("_Base", ast.Load())],
        keywords=[],
        body=[node, ahead, *extra],
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

    # --- since the previous fetch ------------------------------------------
    def fetch(obj, hours, minute):
        obj.coordinator.hourly_consumption = hours
        obj.coordinator.hourly_consumption_fetched_at = datetime(
            2026, 10, 14, 12, minute, tzinfo=TZ
        )
        obj._handle_coordinator_update()
        return obj._attr_native_value

    H1 = _hour("20261014 10", heat=1.0, cool=0.5, tank=0.25)
    H2 = _hour("20261014 11", heat=2.0, tank=1.0)
    H3 = _hour("20261014 12", heat=0.5, tank=0.125)
    H3b = _hour("20261014 12", heat=0.75, tank=0.25)

    obj = Today(ConsumptionType.HEAT, MONTH, [H1, H2], None)
    obj._handle_coordinator_update()
    check(
        "since-fetch: before any fetch, no recompute",
        obj._attr_native_value == "previous" and obj.writes == 1,
        f"value={obj._attr_native_value!r}",
    )

    obj = Today(ConsumptionType.HEAT, MONTH)
    v1 = fetch(obj, [H1, H2], 0)
    v2 = fetch(obj, [H1, H2, H3], 15)
    v3 = fetch(obj, [H1, H2, H3b], 30)
    v4 = fetch(obj, [H1, H2, H3b], 45)
    check(
        "since-fetch heat: first fetch 0, then the rise, then 0 when idle",
        (v1, v2, v3, v4) == (0.0, 0.5, 0.25, 0.0),
        f"values={(v1, v2, v3, v4)}",
    )

    obj = Today(ConsumptionType.HEAT, MONTH)
    fetch(obj, [H1, H2, H3], 0)
    writes = obj.writes
    fetch(obj, [H1, H2, H3b], 15)
    held = obj._attr_native_value
    for _ in range(14):  # 60 s refreshes between fetches
        obj._handle_coordinator_update()
    check(
        "since-fetch: value held (not recomputed) between fetches",
        held == 0.25 and obj._attr_native_value == 0.25 and obj.writes == writes + 15,
        f"value={obj._attr_native_value} writes={obj.writes}",
    )

    for ctype, expected_value in (
        (ConsumptionType.COOL, 0.0),
        (ConsumptionType.WATER_TANK, 0.125),
        (ConsumptionType.TOTAL, 0.375),
    ):
        obj = Today(ctype, MONTH)
        fetch(obj, [H1, H2, H3], 0)
        value = fetch(obj, [H1, H2, H3b], 15)
        check(
            f"since-fetch {ctype}: rise of its own type",
            value == expected_value,
            f"value={value!r}",
        )

    # Hour/day boundary: the sum covers all cached hours, so a new hour's
    # record or a new day's is just more energy.
    obj = Today(ConsumptionType.HEAT, MONTH)
    fetch(obj, [H1, H2], 0)
    value = fetch(obj, [H1, H2, _hour("20261015 00", heat=0.125)], 15)
    check("since-fetch across a day boundary: the new record", value == 0.125)

    # A day leaving the cache shrinks the total: 0, and a new baseline.
    obj = Today(ConsumptionType.HEAT, MONTH)
    fetch(obj, [_hour("20261012 10", heat=5.0), H1, H2], 0)
    shrunk = fetch(obj, [H1, H2], 15)
    after = fetch(obj, [H1, H2, H3], 30)
    check(
        "since-fetch, total shrinks: 0, re-baselined",
        shrunk == 0.0 and after == 0.5,
        f"shrunk={shrunk!r} after={after!r}",
    )

    obj = Today(ConsumptionType.TOTAL, MONTH)
    fetch(obj, [], 0)
    value = fetch(
        obj, [_hour("20261014 12", heat=1.0, cool=0.5, tank=0.25, total="n/a")], 15
    )
    check(
        "since-fetch total: falls back to heat+cool+tank",
        value == 1.75,
        f"value={value!r}",
    )

    for label, hours in (("empty list", []), ("no list", None)):
        obj = Today(ConsumptionType.HEAT, MONTH)
        value = fetch(obj, hours, 0)
        check(f"since-fetch with {label}: 0", value == 0.0, f"value={value!r}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
