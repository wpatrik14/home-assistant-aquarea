"""Regression tests: delayed refreshes are background tasks owned by the entry.

Background
----------
After a command, the climate, water heater, switch and select entities sleep
5-10 seconds and then request a coordinator refresh. They started that with
`hass.async_create_task`, which ties the task to nothing: when the config
entry was unloaded or reloaded inside that window, the task survived and
refreshed a coordinator that no longer belonged to a loaded entry. Home
Assistant also waits for plain tasks during startup/shutdown.

The delayed refresh is now started through `AquareaBaseEntity
._start_delayed_refresh`, which uses `entry.async_create_background_task`;
Home Assistant cancels those tasks when the entry unloads.

The `except RequestFailedError` blocks around `async_request_refresh` were
dead code: the coordinator handles refresh errors itself (they become
UpdateFailed) and `async_request_refresh` never raises them. They are gone.

Checks:
- static (AST): no `async_create_task` call left in the four platforms, every
  `_schedule_refresh(...)` call is passed to `_start_delayed_refresh`, and no
  `_schedule_refresh` catches `RequestFailedError`;
- behaviour: the real `_start_delayed_refresh` (loaded from entity.py via
  AST) hands the coroutine to the entry's `async_create_background_task`.

Intentionally dependency-free (stdlib only):

    python3 tests/test_delayed_refresh.py
"""

import __future__

import ast
import os
import sys
import types

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")
PLATFORMS = ("climate.py", "water_heater.py", "switch.py", "select.py")


def _tree(filename):
    with open(os.path.join(BASE, filename), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _static_findings(filename):
    tree = _tree(filename)
    create_task = []
    unwrapped = []
    wrapped = 0
    dead_except = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "async_create_task":
                create_task.append(node.lineno)
            if name == "_start_delayed_refresh":
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Call)
                        and _call_name(arg) == "_schedule_refresh"
                    ):
                        wrapped += 1
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_schedule_refresh":
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler) and sub.type is not None:
                    if "RequestFailedError" in ast.unparse(sub.type):
                        dead_except.append(sub.lineno)
    total = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _call_name(n) == "_schedule_refresh"
    )
    if wrapped != total:
        unwrapped.append(f"{total - wrapped} of {total}")
    return create_task, unwrapped, dead_except, total


def _load_helper():
    tree = _tree("entity.py")
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaBaseEntity"
    )
    node = next(
        (
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "_start_delayed_refresh"
        ),
        None,
    )
    if node is None:
        return None
    flow = ast.ClassDef(
        name="Extracted",
        bases=[],
        keywords=[],
        body=[node],
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {"DOMAIN": "aquarea"}
    exec(
        compile(
            module,
            "entity.py",
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


class _Entry:
    def __init__(self):
        self.calls = []

    def async_create_background_task(self, hass, target, name, eager_start=True):
        self.calls.append((hass, target, name))


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    for filename in PLATFORMS:
        create_task, unwrapped, dead_except, total = _static_findings(filename)
        check(f"{filename}: has delayed refreshes", total > 0, f"calls={total}")
        check(
            f"{filename}: no hass.async_create_task",
            not create_task,
            f"lines={create_task}",
        )
        check(
            f"{filename}: every refresh via _start_delayed_refresh",
            not unwrapped,
            f"unwrapped={unwrapped}",
        )
        check(
            f"{filename}: no dead except RequestFailedError",
            not dead_except,
            f"lines={dead_except}",
        )

    cls = _load_helper()
    check("AquareaBaseEntity defines _start_delayed_refresh", cls is not None)
    if cls is not None:
        obj = cls()
        obj.hass = object()
        obj.entity_id = "switch.x"
        entry = _Entry()
        obj.coordinator = types.SimpleNamespace(entry=entry)

        async def _coro():
            pass

        coro = _coro()
        obj._start_delayed_refresh(coro)
        coro.close()
        ok = (
            len(entry.calls) == 1
            and entry.calls[0][0] is obj.hass
            and entry.calls[0][1] is coro
            and isinstance(entry.calls[0][2], str)
        )
        check(
            "helper hands the coroutine to entry.async_create_background_task",
            ok,
            f"calls={entry.calls}",
        )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
