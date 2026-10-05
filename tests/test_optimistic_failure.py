"""Regression tests: a failed command must not leave optimistic state behind.

Background
----------
The switch and select entities set an optimistic value, write state, then call
the cloud API. If the call raises, `_schedule_refresh` (the only place that
clears the optimistic value) is never reached, so the entity keeps showing the
value that was never applied and ignores real device data until the next
successful command. The fix clears the optimistic value, rewrites state and
re-raises, so the user sees the error and the entity falls back to real data.

These tests load the *actual* command methods of every switch and select
entity out of switch.py / select.py (via AST, so they exercise the shipped
code rather than a copy) and drive them with a device whose setters raise.

Intentionally dependency-free (stdlib only):

    python3 tests/test_optimistic_failure.py
"""

import __future__

import ast
import asyncio
import os
import sys
import types

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")

COMMANDS = {
    "switch.py": ("async_turn_on", "async_turn_off"),
    "select.py": ("async_select_option",),
}
OPTIMISTIC = ("_optimistic_is_on", "_optimistic_option")


class _Anything:
    """Stands in for enums/lookup tables: any attribute or `.get` works."""

    def __getattr__(self, name):
        return _Anything()

    def get(self, key, default=None):
        return "looked-up"

    def __str__(self):
        return "anything"


class _Logger:
    def debug(self, *args, **kwargs):
        pass


class _Boom(Exception):
    pass


class _Device:
    device_id = "dev"
    quiet_mode = "current"
    powerful_time = "current"

    def __getattr__(self, name):
        if name.startswith("set_"):

            async def _raise(*args, **kwargs):
                raise _Boom(name)

            return _raise
        raise AttributeError(name)


def _load(filename):
    path = os.path.join(BASE, filename)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = COMMANDS[filename]
    out = []
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        methods = [
            n
            for n in cls.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name in wanted
        ]
        if not methods:
            continue
        flow = ast.ClassDef(
            name=cls.name,
            bases=[],
            keywords=[],
            body=methods,
            decorator_list=[],
            type_params=[],
        )
        module = ast.fix_missing_locations(ast.Module([flow], []))
        namespace = {"_LOGGER": _Logger()}
        for name in (
            "aioaquarea",
            "QUIET_MODE_LOOKUP",
            "POWERFUL_TIME_LOOKUP",
            "PowerfulTime",
            "SWITCH_DELAY",
            "SELECT_DELAY",
        ):
            namespace[name] = _Anything()
        # The modules have `from __future__ import annotations`; compile with
        # the same flag so annotations are never evaluated on any Python.
        exec(
            compile(
                module,
                path,
                "exec",
                flags=__future__.annotations.compiler_flag,
                dont_inherit=True,
            ),
            namespace,
        )
        out.append((cls.name, namespace[cls.name], methods))
    return out


class _Hass:
    def __init__(self):
        self.tasks = 0

    def async_create_task(self, coro):
        self.tasks += 1
        coro.close()


def _make(cls):
    obj = cls.__new__(cls)
    obj.coordinator = types.SimpleNamespace(device=_Device())
    obj.hass = _Hass()
    obj.writes = []
    obj.async_write_ha_state = lambda: obj.writes.append(
        {a: getattr(obj, a, None) for a in OPTIMISTIC}
    )
    obj._schedule_refresh = lambda *a, **k: asyncio.sleep(0)

    def _start_delayed_refresh(coro):
        obj.hass.tasks += 1
        coro.close()

    obj._start_delayed_refresh = _start_delayed_refresh
    for attr in OPTIMISTIC:
        setattr(obj, attr, None)
    return obj


def main():
    failures = 0
    checked = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    for filename in COMMANDS:
        classes = _load(filename)
        check(
            f"{filename}: found command entities",
            len(classes) >= 2,
            f"got={[c[0] for c in classes]}",
        )
        for cls_name, cls, methods in classes:
            for method in methods:
                obj = _make(cls)
                raised = False
                try:
                    kwargs = (
                        {"option": "x"} if method.name == "async_select_option" else {}
                    )
                    asyncio.run(getattr(obj, method.name)(**kwargs))
                except _Boom:
                    raised = True
                checked += 1
                cleared = all(getattr(obj, a) is None for a in OPTIMISTIC)
                check(
                    f"{cls_name}.{method.name}: error propagates, optimistic cleared",
                    raised
                    and cleared
                    and len(obj.writes) >= 2
                    and all(v is None for v in obj.writes[-1].values())
                    and obj.hass.tasks == 0,
                    f"raised={raised} cleared={cleared} writes={obj.writes}",
                )

    check("exercised all 8 command methods", checked == 8, f"checked={checked}")
    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
