"""Regression test for the zone-cycle detector (_is_zone_active).

Background
----------
`zone_cycles_today` increments on each low->high transition of
`_is_zone_active`. The first version keyed purely off `current_direction ==
PUMP`, but on real hardware the device also reports `PUMP` transiently while
heating the DHW tank, so the counter tracked `dhw_cycles_today` in lockstep
even with every zone switched off. The fix additionally requires that at
least one zone be `operation_status == ON`.

This test loads the *actual* `_is_zone_active` out of sensor.py (via AST, so
it exercises the shipped code rather than a copy) and runs it against stub
enums whose values mirror aioaquarea.data:

    OperationStatus:  OFF=0, ON=1
    DeviceDirection:  IDLE=0, PUMP=1, WATER=2

It is intentionally dependency-free (stdlib only) so it runs without Home
Assistant, aioaquarea, or pytest installed:

    python3 tests/test_zone_detector.py
"""
import ast
import os
import sys
import types
from enum import IntEnum

SENSOR = os.path.join(
    os.path.dirname(__file__),
    "..", "custom_components", "aquarea", "sensor.py",
)


# --- stub aioaquarea module (only what the detector touches) -----------------
class OperationStatus(IntEnum):
    OFF = 0
    ON = 1


class DeviceDirection(IntEnum):
    IDLE = 0
    PUMP = 1
    WATER = 2


aioaquarea = types.ModuleType("aioaquarea")
aioaquarea.OperationStatus = OperationStatus
aioaquarea.DeviceDirection = DeviceDirection
aioaquarea.Device = object  # only referenced as a type annotation
sys.modules["aioaquarea"] = aioaquarea


# --- pull the real _is_zone_active out of sensor.py -------------------------
def _load_detector():
    with open(SENSOR) as fh:
        tree = ast.parse(fh.read())
    node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_is_zone_active"
    )
    namespace = {"aioaquarea": aioaquarea}
    exec(compile(ast.Module([node], []), SENSOR, "exec"), namespace)
    return namespace["_is_zone_active"]


# --- fakes -------------------------------------------------------------------
class _Zone:
    def __init__(self, on):
        self.operation_status = OperationStatus.ON if on else OperationStatus.OFF


class _Device:
    def __init__(self, direction, zones):
        self.current_direction = direction
        self.zones = zones


def main():
    is_zone_active = _load_detector()
    D = DeviceDirection
    cases = [
        # (name, device, expected)
        ("idle, no zones on",            _Device(D.IDLE,  {0: _Zone(False)}),                 False),
        ("DHW heating (WATER)",          _Device(D.WATER, {0: _Zone(False)}),                 False),
        ("PUMP, all zones OFF (DHW)",    _Device(D.PUMP,  {0: _Zone(False), 1: _Zone(False)}), False),
        ("PUMP, one zone ON",            _Device(D.PUMP,  {0: _Zone(False), 1: _Zone(True)}),  True),
        ("PUMP, all zones ON",           _Device(D.PUMP,  {0: _Zone(True)}),                  True),
        ("PUMP, no zones dict",          _Device(D.PUMP,  None),                              False),
        ("direction is None",            _Device(None,    {0: _Zone(True)}),                  False),
    ]

    failures = 0
    for name, dev, expected in cases:
        got = is_zone_active(dev)
        ok = got == expected
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<28} expected={expected!s:<5} got={got}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
