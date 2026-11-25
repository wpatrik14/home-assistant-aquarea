"""Regression test for the weekly schedule read cadence (_weekly_timer_due).

The weekly schedule is optional and changes rarely, so the coordinator reads
it at most once per interval. A failed read counts as a request (otherwise a
failing endpoint would be hit every poll).

This test loads the *actual* `_weekly_timer_due` out of coordinator.py (via
AST, so it exercises the shipped code rather than a copy).

Intentionally dependency-free (stdlib only):

    python3 tests/test_weekly_timer_due.py
"""

import ast
from datetime import datetime, timedelta
import os
import sys

COORDINATOR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "aquarea",
    "coordinator.py",
)


def _load_helper():
    with open(COORDINATOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_weekly_timer_due"
    )
    namespace = {"datetime": datetime, "timedelta": timedelta}
    exec(compile(ast.Module([node], []), COORDINATOR, "exec"), namespace)
    return namespace["_weekly_timer_due"]


def main():
    due = _load_helper()
    t0 = datetime(2026, 1, 5, 12, 0, 0)
    hour = timedelta(hours=1)
    cases = [
        ("never requested", t0, None, True),
        ("just requested", t0, t0, False),
        ("61 seconds later", t0 + timedelta(seconds=61), t0, False),
        ("one second short", t0 + hour - timedelta(seconds=1), t0, False),
        ("exactly one interval", t0 + hour, t0, True),
        ("much later", t0 + timedelta(days=2), t0, True),
    ]

    failures = 0
    for name, now, requested_at, expected in cases:
        got = due(now, requested_at, hour)
        ok = got is expected
        failures += not ok
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name:<22} expected={expected!s:<6} got={got}"
        )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
