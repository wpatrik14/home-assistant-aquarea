"""Regression test: the coordinator passes its config entry to the base class.

Background
----------
`DataUpdateCoordinator.__init__` takes `config_entry=`. When it is omitted,
Home Assistant falls back to a ContextVar holding the entry being set up. That
only works while the coordinator is built inside the entry's setup call, and
Home Assistant reports it as an error for core integrations (it is silenced,
not endorsed, for custom ones). `AquareaDataUpdateCoordinator` already receives
its entry, so it hands it over explicitly (audit finding,
wpatrik14/fleet-backlog#88).

The real `__init__` is loaded out of coordinator.py via AST into a class built
on a stub base that records the keyword arguments it receives.

Intentionally dependency-free (stdlib only):

    python3 tests/test_coordinator_init.py
"""
import __future__
import ast
from datetime import timedelta
import os
import sys
import types

COORDINATOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "coordinator.py"
)


class _Base:
    def __init__(self, hass, logger, **kwargs):
        self.base_args = (hass, logger)
        self.base_kwargs = kwargs


LOGGER = object()


def _load():
    with open(COORDINATOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaDataUpdateCoordinator"
    )
    node = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    flow = ast.ClassDef(
        name="Extracted", bases=[ast.Name("_Base", ast.Load())], keywords=[],
        body=[node], decorator_list=[], type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {
        "_Base": _Base,
        "_LOGGER": LOGGER,
        "timedelta": timedelta,
        "DOMAIN": "aquarea",
        "CONF_USERNAME": "username",
        "CONF_CONSUMPTION_INTERVAL": "consumption_interval",
        "DEFAULT_SCAN_INTERVAL": 60,
        "DEFAULT_CONSUMPTION_INTERVAL": 60,
    }
    exec(
        compile(
            module, COORDINATOR, "exec",
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

    Extracted = _load()
    hass = object()
    entry = types.SimpleNamespace(
        data={"username": "user@example.com"}, options={"consumption_interval": 30}
    )
    device_info = types.SimpleNamespace(device_id="dev1")
    obj = Extracted(hass, entry, object(), device_info)

    check("config_entry passed to DataUpdateCoordinator",
          obj.base_kwargs.get("config_entry", "missing") is entry,
          f"kwargs={sorted(obj.base_kwargs)}")
    check("hass and logger passed positionally",
          obj.base_args == (hass, LOGGER), f"args={obj.base_args}")
    check("name and update interval unchanged",
          obj.base_kwargs.get("name") == "aquarea-user@example.com-dev1"
          and obj.base_kwargs.get("update_interval") == timedelta(seconds=60),
          f"kwargs={obj.base_kwargs}")
    check("consumption interval read from options",
          obj.consumption_interval == 30, f"got={obj.consumption_interval}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
