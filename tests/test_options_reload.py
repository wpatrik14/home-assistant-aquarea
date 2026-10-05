"""Regression tests: changing options must reload the config entry.

Background
----------
The coordinator reads `consumption_interval` once, when it is constructed.
With no update listener, saving a new value in the options dialog changed the
stored option but nothing ever re-read it until Home Assistant restarted.
`async_setup_entry` now registers `_async_update_listener`, which reloads the
entry, and unregisters it on unload.

The real `_async_update_listener` is loaded out of __init__.py via AST and the
registration in `async_setup_entry` is checked on the source tree.

Intentionally dependency-free (stdlib only):

    python3 tests/test_options_reload.py
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


def _tree():
    with open(INIT, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _find(tree, name):
    return next(
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _listener():
    module = ast.fix_missing_locations(
        ast.Module([_find(_tree(), "_async_update_listener")], [])
    )
    namespace = {}
    exec(
        compile(
            module,
            INIT,
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        ),
        namespace,
    )
    return namespace["_async_update_listener"]


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    reloaded = []

    async def async_reload(entry_id):
        reloaded.append(entry_id)

    hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(async_reload=async_reload)
    )
    asyncio.run(_listener()(hass, types.SimpleNamespace(entry_id="abc")))
    check(
        "update listener reloads the changed entry",
        reloaded == ["abc"],
        f"got={reloaded!r}",
    )

    src = ast.unparse(_find(_tree(), "async_setup_entry"))
    check(
        "async_setup_entry registers the listener and unregisters on unload",
        "entry.async_on_unload(entry.add_update_listener(_async_update_listener))"
        in src,
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
