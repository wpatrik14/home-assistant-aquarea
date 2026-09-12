"""Regression test for the error-code detector (_current_error_info).

Background
----------
Issue #50: the integration already exposes *whether* the device is in an
error state (`binary_sensor.AquareaStatusBinarySensor`, backed by
`Device.is_on_error`), but not *which* error. `aioaquarea.Device` already
carries this via `current_error` (a `FaultError(error_code, error_message)`
or `None`) - it just wasn't read anywhere in the HA integration.

`_current_error_info` is the small pure helper `ErrorCodeSensor` uses to turn
`device.current_error` into the `(error_code, error_message)` pair it puts on
the entity's state/attributes. Kept as a standalone function (matching the
`_is_zone_active`/`_is_defrosting`/`_is_heating_water` detectors already in
sensor.py) specifically so it can be tested without instantiating a real
`SensorEntity`/coordinator.

This test loads the *actual* `_current_error_info` out of sensor.py (via AST,
so it exercises the shipped code rather than a copy).

Intentionally dependency-free (stdlib only) so it runs without Home
Assistant, aioaquarea, or pytest installed:

    python3 tests/test_error_code_sensor.py
"""
import ast
import os
import sys
import types
from dataclasses import dataclass

SENSOR = os.path.join(
    os.path.dirname(__file__),
    "..", "custom_components", "aquarea", "sensor.py",
)


# --- stub aioaquarea module (only what the detector touches) ---------------
@dataclass
class FaultError:
    error_message: str
    error_code: str


aioaquarea = types.ModuleType("aioaquarea")
aioaquarea.Device = object  # only referenced as a type annotation
sys.modules["aioaquarea"] = aioaquarea


# --- pull the real _current_error_info out of sensor.py --------------------
def _load_detector():
    with open(SENSOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_current_error_info"
    )
    namespace = {"aioaquarea": aioaquarea}
    exec(compile(ast.Module([node], []), SENSOR, "exec"), namespace)
    return namespace["_current_error_info"]


# --- fakes -------------------------------------------------------------------
class _Device:
    def __init__(self, current_error):
        self.current_error = current_error


def main():
    current_error_info = _load_detector()
    cases = [
        # (name, device, expected)
        ("no error", _Device(None), (None, None)),
        (
            "H62 fault active",
            _Device(FaultError(error_code="H62", error_message="Water flow error")),
            ("H62", "Water flow error"),
        ),
        (
            "different fault code",
            _Device(FaultError(error_code="F12", error_message="Sensor fault")),
            ("F12", "Sensor fault"),
        ),
    ]

    failures = 0
    for name, dev, expected in cases:
        got = current_error_info(dev)
        ok = got == expected
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<28} expected={expected!s:<35} got={got}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
