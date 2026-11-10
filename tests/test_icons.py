"""Regression test: icons.json matches the entities strings.json defines.

Icons live in icons.json (Home Assistant's icon translations), keyed by
platform and translation_key. This test checks that

* icons.json has no orphan entry: every (platform, key) is also an entity
  in strings.json, and every icon is an `mdi:` name;
* every entity whose icon used to be hard-coded still has its entry;
* a `state` icon is only given next to a `default`;
* the source keeps no `_attr_icon` / `icon` property for those entities (the
  two it keeps are listed in CODE_ICONS).

Intentionally dependency-free (stdlib only):

    python3 tests/test_icons.py
"""

import json
import os
import re
import sys

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")

# (platform, translation_key) pairs that had a hard-coded icon in the code.
MIGRATED = {
    ("binary_sensor", "defrost"),
    ("binary_sensor", "weekly_timer"),
    ("binary_sensor", "pump_status"),
    ("button", "request_defrost"),
    ("button", "rebuild_lifetime_consumption"),
    ("select", "quiet_mode"),
    ("select", "powerful_time"),
    ("sensor", "direction"),
    ("sensor", "error_code"),
    ("sensor", "dhw_cycles_today"),
    ("sensor", "zone_cycles_today"),
    ("sensor", "defrost_cycles_today"),
    ("switch", "force_dhw"),
    ("switch", "force_heater"),
    ("switch", "holiday_timer"),
}

# Icons that depend on more than the entity's state, so they stay in code.
CODE_ICONS = {"climate.py", "water_heater.py"}


def _load(name):
    with open(os.path.join(BASE, name), encoding="utf-8") as fh:
        return json.load(fh)


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<40} {detail}")

    strings = _load("translations/en.json").get("entity", {})
    icons = _load("icons.json").get("entity", {})

    have = {(p, k) for p, keys in icons.items() for k in keys}
    known = {(p, k) for p, keys in strings.items() for k in keys}

    check(
        "icons.json has no orphan keys",
        have <= known,
        f"orphans={sorted(have - known)}",
    )
    check(
        "every migrated icon has an entry",
        have >= MIGRATED,
        f"missing={sorted(MIGRATED - have)}",
    )

    bad = []
    for platform, keys in icons.items():
        for key, entry in keys.items():
            values = [entry.get("default"), *entry.get("state", {}).values()]
            if not entry.get("default") or not all(
                isinstance(v, str) and v.startswith("mdi:") for v in values
            ):
                bad.append(f"{platform}.{key}")
    check("every entry has a default and mdi: icons", not bad, f"bad={bad}")

    stray = []
    for name in sorted(os.listdir(BASE)):
        if not name.endswith(".py") or name in CODE_ICONS:
            continue
        with open(os.path.join(BASE, name), encoding="utf-8") as fh:
            if re.search(r"_attr_icon|def icon\(|\bicon=", fh.read()):
                stray.append(name)
    check("no icon left in code", not stray, f"files={stray}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
