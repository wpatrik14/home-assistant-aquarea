"""Tests for `async_unload_entry`.

Background
----------
Unloading the entry unloads every platform and, only if that succeeded,
drops the entry's coordinators from `hass.data[DOMAIN]`. If a platform fails
to unload, the data must stay, because the entities that are still loaded
read their coordinator from it. Other entries' data is never touched.

These describe current behaviour; they were listed as missing coverage in the
audit (wpatrik14/fleet-backlog#88). The real `async_unload_entry` is loaded
out of __init__.py via AST.

Intentionally dependency-free (stdlib only):

    python3 tests/test_unload_entry.py
"""
import __future__
import ast
import asyncio
import os
import sys
import types

INIT = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "__init__.py"
)
DOMAIN = "aquarea"
PLATFORMS = ["button", "sensor", "climate"]


def _load():
    with open(INIT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_unload_entry"
    )
    module = ast.fix_missing_locations(ast.Module([node], []))
    namespace = {"DOMAIN": DOMAIN, "PLATFORMS": PLATFORMS}
    exec(
        compile(
            module, INIT, "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        ),
        namespace,
    )
    return namespace["async_unload_entry"]


async_unload_entry = _load()


class _ConfigEntries:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def async_unload_platforms(self, entry, platforms):
        self.calls.append((entry, list(platforms)))
        return self.result


def _hass(result):
    return types.SimpleNamespace(
        config_entries=_ConfigEntries(result),
        data={DOMAIN: {"entry-1": {"devices": {}}, "entry-2": {"devices": {}}}},
    )


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    entry = types.SimpleNamespace(entry_id="entry-1")

    hass = _hass(True)
    result = asyncio.run(async_unload_entry(hass, entry))
    check("successful unload returns True", result is True, f"got={result!r}")
    check("unloads every platform of this entry",
          hass.config_entries.calls == [(entry, PLATFORMS)],
          f"calls={hass.config_entries.calls}")
    check("drops this entry's data, keeps the other entry",
          set(hass.data[DOMAIN]) == {"entry-2"}, f"data={hass.data[DOMAIN]}")

    hass = _hass(False)
    result = asyncio.run(async_unload_entry(hass, entry))
    check("failed platform unload returns False", result is False, f"got={result!r}")
    check("failed platform unload keeps the data",
          set(hass.data[DOMAIN]) == {"entry-1", "entry-2"},
          f"data={hass.data[DOMAIN]}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
