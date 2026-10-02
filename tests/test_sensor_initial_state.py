"""Regression tests: state sensors must show real data as soon as they are added.

Background
----------
The outdoor temperature, tank temperature, direction, pump status and error
code sensors set their value only in `_handle_coordinator_update`.
`CoordinatorEntity.async_added_to_hass` only subscribes to *future*
coordinator updates, so after every restart or reload these sensors were
"unknown" until the next poll (up to a minute), although the first refresh
had already fetched the data. #69 fixed the same thing for climate zones.
They now run their update logic once in `async_added_to_hass`, after the base
class has subscribed.

For each sensor class, the `async_added_to_hass` it resolves to (its own or
one inherited from another class in sensor.py) is loaded via AST into a class
built on a stub base, and the call order is checked.

Intentionally dependency-free (stdlib only):

    python3 tests/test_sensor_initial_state.py
"""
import __future__
import ast
import asyncio
import os
import sys

SENSOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "sensor.py"
)

SENSORS = [
    "OutdoorTemperatureSensor",
    "TankTemperatureSensor",
    "PumpDirectionSensor",
    "PumpStatusSensor",
    "ErrorCodeSensor",
]


class _Base:
    def __init__(self):
        self.calls = []

    async def async_added_to_hass(self):
        self.calls.append("subscribe")

    def _handle_coordinator_update(self):
        self.calls.append("apply data")


def _find_method(classes, name):
    """Depth-first search of sensor.py's own classes for async_added_to_hass."""
    cls = classes.get(name)
    if cls is None:
        return None
    for node in cls.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_added_to_hass":
            return node
    for base in cls.bases:
        if isinstance(base, ast.Name):
            found = _find_method(classes, base.id)
            if found is not None:
                return found
    return None


def _load(tree, name):
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    node = _find_method(classes, name)
    if node is None:
        return None
    flow = ast.ClassDef(
        name="Extracted", bases=[ast.Name("_Base", ast.Load())],
        keywords=[], body=[node], decorator_list=[], type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {"_Base": _Base}
    exec(
        compile(
            module, SENSOR, "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    with open(SENSOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    for name in SENSORS:
        cls = _load(tree, name)
        if cls is None:
            check(f"{name} applies data on add", False,
                  "no async_added_to_hass in sensor.py")
            continue
        obj = cls()
        asyncio.run(obj.async_added_to_hass())
        check(f"{name} applies data on add",
              obj.calls == ["subscribe", "apply data"], f"calls={obj.calls}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
