"""Regression tests: a climate zone must show real data as soon as it is added.

Background
----------
`HeatPumpClimate.__init__` hard-sets `hvac_mode=OFF` and fills no
temperatures. `CoordinatorEntity.async_added_to_hass` only subscribes to
*future* coordinator updates, so after every restart or reload each zone
reported "off" with no current temperature until the next poll (up to a
minute), which can fire automations triggered on that state. The entity now
runs its update logic once in `async_added_to_hass`, after the base class has
subscribed.

The real `async_added_to_hass` is loaded out of climate.py via AST into a
class built on a stub `CoordinatorEntity`.

Intentionally dependency-free (stdlib only):

    python3 tests/test_climate_initial_state.py
"""

import __future__

import ast
import asyncio
import os
import sys

CLIMATE = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "climate.py"
)


class _CoordinatorEntity:
    calls = None

    async def async_added_to_hass(self):
        self.calls.append("super.async_added_to_hass")


def _load():
    with open(CLIMATE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "HeatPumpClimate"
    )
    node = next(
        (
            n
            for n in cls.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_added_to_hass"
        ),
        None,
    )
    if node is None:
        return None
    flow = ast.ClassDef(
        name="Extracted",
        bases=[ast.Name("_CoordinatorEntity", ast.Load())],
        keywords=[],
        body=[node],
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {"_CoordinatorEntity": _CoordinatorEntity}
    exec(
        compile(
            module,
            CLIMATE,
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
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

    cls = _load()
    check("HeatPumpClimate defines async_added_to_hass", cls is not None)
    if cls is not None:
        calls = []
        _CoordinatorEntity.calls = calls

        class Entity(cls):
            def _handle_coordinator_update(self):
                calls.append("update")

        asyncio.run(Entity().async_added_to_hass())
        check(
            "subscribes via the base class, then applies the current data once",
            calls == ["super.async_added_to_hass", "update"],
            f"got={calls!r}",
        )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
