"""Regression tests for the options flow (Configure dialog).

Background
----------
Home Assistant removed the `OptionsFlow.__init__(config_entry)` signature: the
base class now has no `__init__` of its own, and the flow manager wires the
flow to its entry after construction (`flow.hass`, `flow.handler` = entry id),
exposing it through the `config_entry` property.

`AquareaOptionsFlowHandler` was already written against the new API - its
`async_step_init` reads `self.config_entry` - but the factory still passed the
argument:

    return AquareaOptionsFlowHandler(config_entry)

so opening Configure raised `TypeError: AquareaOptionsFlowHandler() takes no
arguments` and the UI showed "Config flow could not be loaded: 500 Internal
Server Error" (issue #39). The fix (#40) drops the argument.

These tests load the *actual* `async_get_options_flow` and
`AquareaOptionsFlowHandler` out of config_flow.py (via AST, so they exercise
the shipped code rather than a copy) and drive them with a stub `OptionsFlow`
base that mirrors Home Assistant's: no `__init__`, and a `config_entry`
property that is only available once the flow manager has wired the flow up.

Intentionally dependency-free (stdlib only) so it runs without Home Assistant,
voluptuous or pytest installed:

    python3 tests/test_options_flow.py
"""

import __future__

import ast
import asyncio
import os
import sys
import types

HERE = os.path.dirname(__file__)
CONFIG_FLOW = os.path.join(HERE, "..", "custom_components", "aquarea", "config_flow.py")
CONST = os.path.join(HERE, "..", "custom_components", "aquarea", "const.py")


def _parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _class(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


# --- the shipped constants ------------------------------------------------
def _load_constants(*names):
    values = {}
    for node in _parse(CONST).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    values[target.id] = ast.literal_eval(node.value)
    return values


_CONSTANTS = _load_constants(
    "CONF_CONSUMPTION_INTERVAL", "DEFAULT_CONSUMPTION_INTERVAL"
)
CONF_CONSUMPTION_INTERVAL = _CONSTANTS["CONF_CONSUMPTION_INTERVAL"]
DEFAULT_CONSUMPTION_INTERVAL = _CONSTANTS["DEFAULT_CONSUMPTION_INTERVAL"]


# --- stub Home Assistant base class ---------------------------------------
class _OptionsFlow:
    """Mirrors homeassistant.config_entries.OptionsFlow (2026.9).

    No `__init__`, so constructing a subclass with arguments raises
    `TypeError`, exactly as it does in Home Assistant. `config_entry` raises
    until the flow manager has set `hass` and `handler`.
    """

    hass = None
    handler = None

    @property
    def config_entry(self):
        if self.hass is None:
            raise ValueError("The config entry is not available during initialisation")
        if self.handler is None:
            raise ValueError(
                "The config entry id is not available during initialisation"
            )
        return self.hass.config_entries.async_get_known_entry(self.handler)

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id, data_schema):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}


# --- stub voluptuous: records the schema instead of validating ------------
class _Required:
    def __init__(self, key, default=None):
        self.key = key
        self.default = default


class _Schema:
    def __init__(self, fields):
        self.fields = fields


_vol = types.SimpleNamespace(
    Schema=_Schema,
    Required=_Required,
    All=lambda *validators: validators,
    Coerce=lambda type_: ("coerce", type_),
    Range=lambda **bounds: ("range", bounds),
)


# --- pull the real code out of config_flow.py -----------------------------
def _load():
    tree = _parse(CONFIG_FLOW)
    factory = next(
        n
        for n in _class(tree, "AquareaConfigFlow").body
        if isinstance(n, ast.FunctionDef) and n.name == "async_get_options_flow"
    )
    handler = _class(tree, "AquareaOptionsFlowHandler")
    namespace = {
        "config_entries": types.SimpleNamespace(
            OptionsFlow=_OptionsFlow, OptionsFlowWithReload=_OptionsFlow
        ),
        "callback": lambda func: func,
        "vol": _vol,
        "CONF_CONSUMPTION_INTERVAL": CONF_CONSUMPTION_INTERVAL,
        "DEFAULT_CONSUMPTION_INTERVAL": DEFAULT_CONSUMPTION_INTERVAL,
    }
    # config_flow.py has `from __future__ import annotations`; compile with the
    # same flag so its annotations are never evaluated, on any Python version.
    module = ast.Module([handler, factory], [])
    code = compile(
        module,
        CONFIG_FLOW,
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    exec(code, namespace)
    return namespace["async_get_options_flow"], namespace["AquareaOptionsFlowHandler"]


async_get_options_flow, AquareaOptionsFlowHandler = _load()


# --- fakes for the flow manager's side ------------------------------------
class _Entry:
    def __init__(self, *, data=None, options=None):
        self.entry_id = "entry-1"
        self.data = data or {}
        self.options = options or {}


class _Hass:
    def __init__(self, entry):
        self._entry = entry
        self.config_entries = self

    def async_get_known_entry(self, entry_id):
        assert entry_id == self._entry.entry_id, entry_id
        return self._entry


def _create_flow(entry):
    """What OptionsFlowManager.async_create_flow + async_init do."""
    flow = async_get_options_flow(entry)
    flow.hass = _Hass(entry)
    flow.handler = entry.entry_id
    return flow


def _default(result):
    (field,) = result["data_schema"].fields
    return field.key, field.default


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    # --- the #39 regression: the factory must not pass config_entry ------
    entry = _Entry()
    try:
        flow = async_get_options_flow(entry)
        error = None
    except TypeError as exc:
        flow, error = None, exc
    check(
        "async_get_options_flow constructs the handler (#39)",
        isinstance(flow, AquareaOptionsFlowHandler),
        f"raised {error!r}" if error else "",
    )
    if error:
        # Every later check builds the flow through the same factory.
        print("\n1 FAILURE(S) - skipping the remaining checks")
        return 1

    # --- the wired-up flow: Configure dialog defaults --------------------
    entry = _Entry(
        data={CONF_CONSUMPTION_INTERVAL: 30},
        options={CONF_CONSUMPTION_INTERVAL: 45},
    )
    result = asyncio.run(_create_flow(entry).async_step_init())
    check(
        "init step shows the form",
        result["type"] == "form" and result["step_id"] == "init",
        f"got={result['type']}/{result.get('step_id')}",
    )
    check(
        "form default comes from options first",
        _default(result) == (CONF_CONSUMPTION_INTERVAL, 45),
        f"got={_default(result)!r}",
    )

    entry = _Entry(data={CONF_CONSUMPTION_INTERVAL: 30})
    result = asyncio.run(_create_flow(entry).async_step_init())
    check(
        "form default falls back to entry data",
        _default(result) == (CONF_CONSUMPTION_INTERVAL, 30),
        f"got={_default(result)!r}",
    )

    entry = _Entry()
    result = asyncio.run(_create_flow(entry).async_step_init())
    check(
        "form default falls back to DEFAULT_CONSUMPTION_INTERVAL",
        _default(result) == (CONF_CONSUMPTION_INTERVAL, DEFAULT_CONSUMPTION_INTERVAL),
        f"got={_default(result)!r}",
    )

    # --- submitting the dialog -------------------------------------------
    user_input = {CONF_CONSUMPTION_INTERVAL: 90}
    result = asyncio.run(_create_flow(_Entry()).async_step_init(user_input))
    check(
        "submitting saves the input as the entry's options",
        result == {"type": "create_entry", "title": "", "data": user_input},
        f"got={result!r}",
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
