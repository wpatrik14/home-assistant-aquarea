"""Tests for the switch, select, button and binary sensor entities.

Background
----------
`test_optimistic_failure.py` covers failed switch/select commands. This file
covers the rest of those entities:

- switches (force DHW, force heater, holiday timer): `is_on` prefers the
  optimistic value, else reads the device; a successful turn on/off sets the
  optimistic value, writes state, sends the matching enum and schedules one
  delayed refresh, which clears the optimistic value and forces a refetch;
- selects (quiet mode, powerful time): an unknown option or the current one
  sends nothing; a new option is mapped through the lookup table and sent;
  `current_option` prefers the optimistic value;
- the defrost button requests a defrost unless one is already running;
- the binary sensors report the device's error flag and defrost status.

These describe current behaviour; they were listed as missing coverage in the
audit (wpatrik14/fleet-backlog#88). Each class's methods (all but __init__)
are loaded out of their module via AST into a class built on a stub entity,
and the module-level lookup tables are executed with stub enums.

The stub counts a delayed refresh whether the entity schedules it with
`hass.async_create_task` or with `_start_delayed_refresh`, so these tests do
not depend on how the refresh task is created.

Intentionally dependency-free (stdlib only):

    python3 tests/test_command_entities.py
"""

import __future__

import ast
import asyncio
import enum
import os
import sys
import types

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")


class OnOff(enum.Enum):
    ON = "on"
    OFF = "off"


class QuietMode(enum.Enum):
    OFF = 0
    LEVEL1 = 1
    LEVEL2 = 2
    LEVEL3 = 3


class PowerfulTime(enum.Enum):
    OFF = 0
    ON_30MIN = 1
    ON_60MIN = 2
    ON_90MIN = 3


class DeviceModeStatus(enum.Enum):
    NORMAL = 0
    DEFROST = 1


aioaquarea = types.SimpleNamespace(
    ForceDHW=OnOff,
    ForceHeater=OnOff,
    HolidayTimer=OnOff,
    QuietMode=QuietMode,
    PowerfulTime=PowerfulTime,
    DeviceModeStatus=DeviceModeStatus,
    errors=types.SimpleNamespace(RequestFailedError=type("RFE", (Exception,), {})),
)


async def _no_sleep(delay):
    pass


class _Device:
    device_id = "dev"

    def __init__(self, **state):
        self.sent = []
        self.__dict__.update(state)

    def __getattr__(self, name):
        if name.startswith(("set_", "request_")):

            async def _send(*args):
                self.sent.append((name, *args))

            return _send
        raise AttributeError(name)


class _Coordinator:
    def __init__(self, device):
        self.device = device
        self.refreshes = []

    async def async_request_refresh(self, force_fetch=False):
        self.refreshes.append(force_fetch)


class _Entity:
    def __init__(self, device):
        self.coordinator = _Coordinator(device)
        self.writes = 0
        self.scheduled = []
        self._optimistic_is_on = None
        self._optimistic_option = None
        outer = self
        self.hass = types.SimpleNamespace(
            async_create_task=lambda coro: outer.scheduled.append(coro)
        )

    def _start_delayed_refresh(self, coro):
        self.scheduled.append(coro)

    def async_write_ha_state(self):
        self.writes += 1


def _load(filename, class_names, extra=None):
    path = os.path.join(BASE, filename)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    namespace = {
        "_Entity": _Entity,
        "aioaquarea": aioaquarea,
        "QuietMode": QuietMode,
        "PowerfulTime": PowerfulTime,
        "asyncio": types.SimpleNamespace(sleep=_no_sleep),
        "_LOGGER": types.SimpleNamespace(
            debug=lambda *a, **k: None, exception=lambda *a, **k: None
        ),
        **(extra or {}),
    }
    # Module-level constants (lookup tables, delays).
    consts = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and all(
            isinstance(t, ast.Name) and t.id.isupper() and not t.id.startswith("_")
            for t in n.targets
        )
    ]
    exec(
        compile(ast.fix_missing_locations(ast.Module(consts, [])), path, "exec"),
        namespace,
    )
    out = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        if cls.name not in class_names:
            continue
        body = [
            n
            for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name != "__init__"
        ]
        flow = ast.ClassDef(
            name=cls.name,
            bases=[ast.Name("_Entity", ast.Load())],
            keywords=[],
            body=body,
            decorator_list=[],
            type_params=[],
        )
        exec(
            compile(
                ast.fix_missing_locations(ast.Module([flow], [])),
                path,
                "exec",
                flags=__future__.annotations.compiler_flag,
                dont_inherit=True,
            ),
            namespace,
        )
        out[cls.name] = namespace[cls.name]
    assert sorted(out) == sorted(class_names), (filename, sorted(out))
    return out


def _run_scheduled(obj):
    for coro in obj.scheduled:
        asyncio.run(coro)


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    # --- switches ----------------------------------------------------------
    switches = _load(
        "switch.py",
        [
            "AquareaForceDHWSwitch",
            "AquareaForceHeaterSwitch",
            "AquareaHolidayTimerSwitch",
        ],
    )
    attrs = {
        "AquareaForceDHWSwitch": ("force_dhw", "set_force_dhw"),
        "AquareaForceHeaterSwitch": ("force_heater", "set_force_heater"),
        "AquareaHolidayTimerSwitch": ("holiday_timer", "set_holiday_timer"),
    }
    for name, cls in switches.items():
        attr, setter = attrs[name]
        for state, expected in ((OnOff.ON, True), (OnOff.OFF, False)):
            obj = cls(_Device(**{attr: state}))
            check(
                f"{name}.is_on reads device {state.name}",
                obj.is_on is expected,
                f"got={obj.is_on}",
            )
        obj = cls(_Device(**{attr: OnOff.OFF}))
        obj._optimistic_is_on = True
        check(f"{name}.is_on prefers the optimistic value", obj.is_on is True)

        for method, target, optimistic in (
            ("async_turn_on", OnOff.ON, True),
            ("async_turn_off", OnOff.OFF, False),
        ):
            device = _Device(**{attr: OnOff.OFF if optimistic else OnOff.ON})
            obj = cls(device)
            asyncio.run(getattr(obj, method)())
            check(
                f"{name}.{method}: optimistic, sends {target.name}, one refresh",
                obj._optimistic_is_on is optimistic
                and obj.writes == 1
                and device.sent == [(setter, target)]
                and len(obj.scheduled) == 1,
                f"opt={obj._optimistic_is_on} writes={obj.writes} "
                f"sent={device.sent} scheduled={len(obj.scheduled)}",
            )
            _run_scheduled(obj)
            check(
                f"{name}.{method}: refresh clears optimistic, forces refetch",
                obj._optimistic_is_on is None and obj.coordinator.refreshes == [True],
                f"opt={obj._optimistic_is_on} refreshes={obj.coordinator.refreshes}",
            )

    # --- selects -----------------------------------------------------------
    selects = _load(
        "select.py", ["AquareaQuietModeSelect", "AquareaPowerfulTimeSelect"]
    )
    cases = {
        "AquareaQuietModeSelect": (
            "quiet_mode",
            "set_quiet_mode",
            QuietMode.OFF,
            "level2",
            QuietMode.LEVEL2,
        ),
        "AquareaPowerfulTimeSelect": (
            "powerful_time",
            "set_powerful_time",
            PowerfulTime.OFF,
            "on-60m",
            PowerfulTime.ON_60MIN,
        ),
    }
    for name, cls in selects.items():
        attr, setter, current, option, mapped = cases[name]
        device = _Device(**{attr: current})
        obj = cls(device)
        check(
            f"{name}.current_option reads the device",
            obj.current_option == "off",
            f"got={obj.current_option!r}",
        )

        for label, choice in (("unknown option", "nope"), ("current option", "off")):
            device = _Device(**{attr: current})
            obj = cls(device)
            asyncio.run(obj.async_select_option(choice))
            check(
                f"{name}: {label} sends nothing",
                device.sent == [] and obj.writes == 0 and not obj.scheduled,
                f"sent={device.sent}",
            )

        device = _Device(**{attr: current})
        obj = cls(device)
        asyncio.run(obj.async_select_option(option))
        check(
            f"{name}: new option mapped, sent, optimistic, one refresh",
            device.sent == [(setter, mapped)]
            and obj.current_option == option
            and obj.writes == 1
            and len(obj.scheduled) == 1,
            f"sent={device.sent} current={obj.current_option!r}",
        )
        _run_scheduled(obj)
        check(
            f"{name}: refresh clears optimistic and refreshes",
            obj._optimistic_option is None and len(obj.coordinator.refreshes) == 1,
            f"opt={obj._optimistic_option!r} refreshes={obj.coordinator.refreshes}",
        )

    obj = selects["AquareaPowerfulTimeSelect"](_Device(powerful_time=PowerfulTime.OFF))
    icon_off = obj.icon
    obj._optimistic_option = "on-30m"
    check(
        "powerful time icon follows the optimistic value",
        icon_off == "mdi:fire-off" and obj.icon == "mdi:fire",
        f"off={icon_off} optimistic={obj.icon}",
    )

    # --- button ------------------------------------------------------------
    button = _load("button.py", ["AquareaDefrostButton"])["AquareaDefrostButton"]
    device = _Device(device_mode_status=DeviceModeStatus.NORMAL)
    asyncio.run(button(device).async_press())
    check(
        "defrost button requests a defrost",
        device.sent == [("request_defrost",)],
        f"sent={device.sent}",
    )
    device = _Device(device_mode_status=DeviceModeStatus.DEFROST)
    asyncio.run(button(device).async_press())
    check(
        "defrost button does nothing while defrosting",
        device.sent == [],
        f"sent={device.sent}",
    )

    # --- binary sensors ----------------------------------------------------
    binary = _load(
        "binary_sensor.py",
        [
            "AquareaStatusBinarySensor",
            "AquareaDefrostBinarySensor",
        ],
    )
    status = binary["AquareaStatusBinarySensor"]
    check(
        "status sensor follows is_on_error",
        status(_Device(is_on_error=True)).is_on is True
        and status(_Device(is_on_error=False)).is_on is False,
    )
    defrost = binary["AquareaDefrostBinarySensor"]
    on = defrost(_Device(device_mode_status=DeviceModeStatus.DEFROST))
    off = defrost(_Device(device_mode_status=DeviceModeStatus.NORMAL))
    check(
        "defrost sensor and icon follow the device mode",
        on.is_on is True
        and on.icon == "mdi:snowflake-melt"
        and off.is_on is False
        and off.icon == "mdi:snowflake-off",
        f"on={on.is_on}/{on.icon} off={off.is_on}/{off.icon}",
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
