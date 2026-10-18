"""Regression test for the raw-flag decoder (_flag_state).

The bivalent / external heater diagnostic binary sensors show a raw integer
flag from the device. Its meaning is unverified, so any non-zero value counts
as on and ``None`` (flag not reported) must stay unknown rather than off.

This test loads the *actual* `_flag_state` out of binary_sensor.py (via AST, so
it exercises the shipped code rather than a copy).

Intentionally dependency-free (stdlib only):

    python3 tests/test_flag_binary_sensors.py
"""

import ast
import os
import sys

BINARY_SENSOR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "aquarea",
    "binary_sensor.py",
)


def _load_helper():
    with open(BINARY_SENSOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_flag_state"
    )
    namespace = {}
    exec(compile(ast.Module([node], []), BINARY_SENSOR, "exec"), namespace)
    return namespace["_flag_state"]


def main():
    flag_state = _load_helper()
    cases = [
        ("not reported", None, None),
        ("zero", 0, False),
        ("one", 1, True),
        ("two", 2, True),
        ("bool false", False, False),
        ("bool true", True, True),
    ]

    failures = 0
    for name, raw, expected in cases:
        got = flag_state(raw)
        ok = got is expected
        failures += not ok
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name:<14} expected={expected!s:<6} got={got}"
        )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
