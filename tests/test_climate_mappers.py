"""Tests for the climate entity's mode mappers and coordinator update.

Background
----------
climate.py translates between aioaquarea's device model and Home Assistant's
climate model with four module-level mappers, and `_handle_coordinator_update`
applies them per zone:

- HVAC mode: a zone that is off is OFF whatever the device mode; otherwise
  HEAT/COOL map directly, AUTO_HEAT/AUTO_COOL become AUTO;
- HVAC action: the pump direction means heating or cooling, depending on the
  mode; anything else is idle;
- target temperature and limits come from the cool or heat values of the zone
  depending on the device mode. A zone that can't set its temperature, or a
  device that is off, gets min = max = current temperature (a locked slider);
- the preset follows the device's special status when it is supported.

These describe current behaviour; they were listed as missing coverage in the
audit (wpatrik14/fleet-backlog#88). The real functions and method are loaded
out of climate.py via AST with stub enums.

Intentionally dependency-free (stdlib only):

    python3 tests/test_climate_mappers.py
"""
import __future__
import ast
import enum
import os
import sys
import types

CLIMATE = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "climate.py"
)


class ExtendedOperationMode(enum.Enum):
    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO_HEAT = 3
    AUTO_COOL = 4


class OperationStatus(enum.Enum):
    OFF = 0
    ON = 1


class DeviceAction(enum.Enum):
    IDLE = 0
    HEATING = 1
    COOLING = 2
    HEATING_WATER = 3


class DeviceDirection(enum.Enum):
    IDLE = 0
    PUMP = 1
    WATER = 2


class UpdateOperationMode(enum.Enum):
    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO = 8


class SpecialStatus(enum.Enum):
    ECO = 1
    COMFORT = 2


class HVACMode(enum.StrEnum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"


class HVACAction(enum.StrEnum):
    IDLE = "idle"
    HEATING = "heating"
    COOLING = "cooling"


class _Base:
    def __init__(self):
        self.writes = 0

    def _handle_coordinator_update(self):
        self.writes += 1


def _load():
    with open(CLIMATE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    funcs = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("get_")
    ]
    lookups = [
        n for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
        and "SPECIAL_STATUS" in ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target)
    ]
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HeatPumpClimate"
    )
    method = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_coordinator_update"
    )
    method.decorator_list = []
    flow = ast.ClassDef(
        name="Extracted", bases=[ast.Name("_Base", ast.Load())], keywords=[],
        body=[method], decorator_list=[], type_params=[],
    )
    namespace = {
        "_Base": _Base,
        "ExtendedOperationMode": ExtendedOperationMode,
        "OperationStatus": OperationStatus,
        "DeviceAction": DeviceAction,
        "DeviceDirection": DeviceDirection,
        "UpdateOperationMode": UpdateOperationMode,
        "SpecialStatus": SpecialStatus,
        "HVACMode": HVACMode,
        "HVACAction": HVACAction,
        "PRESET_ECO": "eco",
        "PRESET_COMFORT": "comfort",
        "PRESET_NONE": "none",
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module([*lookups, *funcs, flow], [])),
            CLIMATE, "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        ),
        namespace,
    )
    return namespace


NS = _load()


def _zone(status=OperationStatus.ON, supports=True):
    return types.SimpleNamespace(
        operation_status=status, temperature=21, supports_set_temperature=supports,
        heat_min=20, heat_max=55, cool_min=5, cool_max=20,
        heat_target_temperature=45, cool_target_temperature=18,
    )


def _climate(mode, zone, direction=DeviceDirection.PUMP, special=None,
             support_special=True):
    device = types.SimpleNamespace(
        mode=mode, operation_status=OperationStatus.ON, current_direction=direction,
        zones={1: zone}, support_special_status=support_special,
        special_status=special,
    )
    obj = NS["Extracted"]()
    obj.coordinator = types.SimpleNamespace(device=device)
    obj._zone_id = 1
    obj._attr_preset_mode = "untouched"
    obj._handle_coordinator_update()
    return obj


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    E, S = ExtendedOperationMode, OperationStatus

    # --- get_hvac_mode_from_ext_op_mode -----------------------------------
    hvac_mode = NS["get_hvac_mode_from_ext_op_mode"]
    for mode, expected in ((E.HEAT, HVACMode.HEAT), (E.COOL, HVACMode.COOL),
                           (E.AUTO_HEAT, HVACMode.AUTO), (E.AUTO_COOL, HVACMode.AUTO),
                           (E.OFF, HVACMode.OFF)):
        got = hvac_mode(mode, S.ON, S.ON)
        check(f"hvac mode: zone on, {mode.name} -> {expected}", got == expected,
              f"got={got}")
    got = hvac_mode(E.HEAT, S.OFF, S.ON)
    check("hvac mode: zone off -> off, whatever the device mode",
          got == HVACMode.OFF, f"got={got}")

    # --- get_hvac_action_from_device_direction ----------------------------
    action = NS["get_hvac_action_from_device_direction"]
    for direction, mode, expected in (
        (DeviceDirection.PUMP, HVACMode.HEAT, HVACAction.HEATING),
        (DeviceDirection.PUMP, HVACMode.COOL, HVACAction.COOLING),
        (DeviceDirection.PUMP, HVACMode.AUTO, HVACAction.IDLE),
        (DeviceDirection.PUMP, HVACMode.OFF, HVACAction.IDLE),
        (DeviceDirection.WATER, HVACMode.HEAT, HVACAction.IDLE),
        (DeviceDirection.IDLE, HVACMode.HEAT, HVACAction.IDLE),
    ):
        got = action(direction, mode)
        check(f"hvac action: {direction.name} + {mode} -> {expected}",
              got == expected, f"got={got}")

    # --- get_hvac_action_from_ext_action ----------------------------------
    ext_action = NS["get_hvac_action_from_ext_action"]
    for act, expected in ((DeviceAction.HEATING, HVACAction.HEATING),
                          (DeviceAction.COOLING, HVACAction.COOLING),
                          (DeviceAction.IDLE, HVACAction.IDLE),
                          (DeviceAction.HEATING_WATER, HVACAction.IDLE)):
        got = ext_action(act)
        check(f"ext action: {act.name} -> {expected}", got == expected, f"got={got}")

    # --- get_update_operation_mode_from_hvac_mode -------------------------
    update_mode = NS["get_update_operation_mode_from_hvac_mode"]
    for mode, expected in ((HVACMode.HEAT, UpdateOperationMode.HEAT),
                           (HVACMode.COOL, UpdateOperationMode.COOL),
                           (HVACMode.AUTO, UpdateOperationMode.AUTO),
                           (HVACMode.OFF, UpdateOperationMode.OFF)):
        got = update_mode(mode)
        check(f"update mode: {mode} -> {expected.name}", got == expected, f"got={got}")

    # --- _handle_coordinator_update ---------------------------------------
    obj = _climate(E.HEAT, _zone())
    check("heat: mode, action, heat limits and target, state written",
          (obj._attr_hvac_mode, obj._attr_hvac_action, obj._attr_min_temp,
           obj._attr_max_temp, obj._attr_target_temperature,
           obj._attr_current_temperature, obj._attr_icon, obj.writes)
          == (HVACMode.HEAT, HVACAction.HEATING, 20, 55, 45, 21, "mdi:hvac", 1),
          f"got={vars(obj)}")

    for mode in (E.COOL, E.AUTO_COOL):
        obj = _climate(mode, _zone())
        check(f"{mode.name}: cool limits and target",
              (obj._attr_min_temp, obj._attr_max_temp, obj._attr_target_temperature)
              == (5, 20, 18), f"got={vars(obj)}")

    obj = _climate(E.AUTO_HEAT, _zone())
    check("AUTO_HEAT: heat limits, action idle (mode is auto)",
          (obj._attr_hvac_mode, obj._attr_hvac_action, obj._attr_min_temp,
           obj._attr_max_temp) == (HVACMode.AUTO, HVACAction.IDLE, 20, 55),
          f"got={vars(obj)}")

    obj = _climate(E.HEAT, _zone(supports=False))
    check("zone can't set temperature: limits locked to current",
          (obj._attr_min_temp, obj._attr_max_temp) == (21, 21), f"got={vars(obj)}")

    obj = _climate(E.OFF, _zone())
    check("device off: off, limits locked, off icon",
          (obj._attr_hvac_mode, obj._attr_min_temp, obj._attr_max_temp,
           obj._attr_icon) == (HVACMode.OFF, 21, 21, "mdi:hvac-off"),
          f"got={vars(obj)}")

    obj = _climate(E.HEAT, _zone(status=S.OFF))
    check("zone off on a heating device: off, idle",
          (obj._attr_hvac_mode, obj._attr_hvac_action)
          == (HVACMode.OFF, HVACAction.IDLE), f"got={vars(obj)}")

    for special, expected in ((SpecialStatus.ECO, "eco"),
                              (SpecialStatus.COMFORT, "comfort"), (None, "none")):
        obj = _climate(E.HEAT, _zone(), special=special)
        check(f"preset follows special status {special}",
              obj._attr_preset_mode == expected, f"got={obj._attr_preset_mode!r}")
    obj = _climate(E.HEAT, _zone(), special=SpecialStatus.ECO, support_special=False)
    check("preset untouched when special status is unsupported",
          obj._attr_preset_mode == "untouched", f"got={obj._attr_preset_mode!r}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
