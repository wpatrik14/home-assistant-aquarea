"""Regression test: WaterHeater's reported state never depended on `_attr_state`.

Background
----------
`_update_operation_state` and `async_set_operation_mode` used to write both
`self._attr_current_operation` and `self._attr_state` on every branch, with
the latter under a `# type: ignore[assignment]` (its declared type on
`WaterHeaterEntity` is `None`, not `str`). The `_attr_state` writes were
deleted as dead code: `WaterHeaterEntity.state` is `@final` in Home Assistant
and always returns `self.current_operation` - `_attr_state` is never read
anywhere in that chain. See the write-up in `aquarea_mypy_ci_plan.md` for how
this was verified against HA's actual source.

This test loads the *actual* `_update_operation_state` and
`async_set_operation_mode` out of water_heater.py (via AST, so it exercises
the shipped code rather than a copy) and drives them against a stub entity
whose `state`/`current_operation` properties mirror HA's real
`WaterHeaterEntity` resolution chain exactly (`state` -> `current_operation`
-> `_attr_current_operation`, never `_attr_state`). `_attr_state` is seeded
with a poison value on the stub; every assertion checks that `state` is
still correct *and* that the poison value was never touched - proving the
deleted writes were never on the path Home Assistant actually reads.

Intentionally dependency-free (stdlib only) so it runs without Home
Assistant, aioaquarea, or pytest installed:

    python3 tests/test_water_heater_state.py
"""
import ast
import asyncio
import os
import sys
from enum import IntEnum

WATER_HEATER = os.path.join(
    os.path.dirname(__file__),
    "..", "custom_components", "aquarea", "water_heater.py",
)

STATE_OFF = "off"  # matches homeassistant.const.STATE_OFF
HEATING = "heating"  # matches custom_components/aquarea/const.py
IDLE = "idle"

POISON = "SHOULD_NEVER_BE_READ"


# --- stub aioaquarea enums (only what the two methods touch) ----------------
class OperationStatus(IntEnum):
    OFF = 0
    ON = 1


class DeviceDirection(IntEnum):
    IDLE = 0
    PUMP = 1
    WATER = 2


class DeviceAction(IntEnum):
    IDLE = 0
    HEATING = 1
    HEATING_WATER = 2


class _NullLogger:
    def debug(self, *a, **k):
        pass


# --- pull the real methods out of water_heater.py ----------------------------
def _load_methods():
    with open(WATER_HEATER, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "WaterHeater"
    )
    wanted = ("_update_operation_state", "async_set_operation_mode")
    nodes = {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in wanted
    }
    namespace = {
        "OperationStatus": OperationStatus,
        "DeviceDirection": DeviceDirection,
        "DeviceAction": DeviceAction,
        "STATE_OFF": STATE_OFF,
        "HEATING": HEATING,
        "IDLE": IDLE,
        "WATER_HEATER_DELAY": 10.0,
        "_LOGGER": _NullLogger(),
    }
    module = ast.Module([nodes[name] for name in wanted], [])
    exec(compile(module, WATER_HEATER, "exec"), namespace)
    return namespace["_update_operation_state"], namespace["async_set_operation_mode"]


_update_operation_state, async_set_operation_mode = _load_methods()


# --- fakes -------------------------------------------------------------------
class _FakeTank:
    def __init__(self, operation_status):
        self.operation_status = operation_status
        self.turned_on = False
        self.turned_off = False

    async def turn_on(self):
        self.turned_on = True

    async def turn_off(self):
        self.turned_off = True


class _FakeDevice:
    def __init__(
        self,
        *,
        operation_status,
        is_on_error=False,
        current_action=None,
        current_direction=None,
    ):
        self.tank = _FakeTank(operation_status)
        self.is_on_error = is_on_error
        self.current_action = current_action
        self.current_direction = current_direction
        self.device_id = "dev-1"


class _FakeCoordinator:
    def __init__(self, device):
        self.device = device


class _FakeHass:
    def async_create_task(self, coro):
        # _schedule_refresh isn't under test here; discard without awaiting.
        coro.close()


class _FakeWaterHeater:
    """Mirrors WaterHeaterEntity's real state resolution: `state` calls
    `current_operation`, which reads `_attr_current_operation` - exactly as
    `@final def state(self): return self.current_operation` does in HA's
    own water_heater/__init__.py. `_attr_state` is deliberately NOT part of
    this chain, matching the real base class.
    """

    _update_operation_state = _update_operation_state
    async_set_operation_mode = async_set_operation_mode

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = _FakeHass()
        self._attr_current_operation = None
        self._attr_icon = None
        # Poison: if anything on the real state path read this, these tests
        # would see it and fail - nothing should.
        self._attr_state = POISON

    @property
    def current_operation(self):
        return self._attr_current_operation

    @property
    def state(self):
        return self.current_operation

    def async_write_ha_state(self):
        pass

    async def _schedule_refresh(self, delay=10.0):
        # Not under test here; async_set_operation_mode hands this to
        # hass.async_create_task(), which discards it without awaiting.
        pass


def _run(coro):
    return asyncio.run(coro)


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    # --- _update_operation_state -----------------------------------------
    wh = _FakeWaterHeater(_FakeCoordinator(_FakeDevice(operation_status=OperationStatus.OFF)))
    wh._update_operation_state()
    check(
        "tank OFF -> state is STATE_OFF via current_operation",
        wh.state == STATE_OFF,
        f"got={wh.state!r}",
    )
    check("tank OFF -> _attr_state was never touched", wh._attr_state == POISON)

    wh = _FakeWaterHeater(_FakeCoordinator(_FakeDevice(
        operation_status=OperationStatus.ON,
        current_direction=DeviceDirection.WATER,
    )))
    wh._update_operation_state()
    check(
        "tank ON, direction WATER -> state is HEATING",
        wh.state == HEATING,
        f"got={wh.state!r}",
    )
    check("tank ON heating -> _attr_state was never touched", wh._attr_state == POISON)

    wh = _FakeWaterHeater(_FakeCoordinator(_FakeDevice(
        operation_status=OperationStatus.ON,
        current_direction=DeviceDirection.IDLE,
        current_action=DeviceAction.IDLE,
    )))
    wh._update_operation_state()
    check(
        "tank ON, not heating -> state is IDLE",
        wh.state == IDLE,
        f"got={wh.state!r}",
    )
    check("tank ON idle -> _attr_state was never touched", wh._attr_state == POISON)

    # --- async_set_operation_mode ------------------------------------------
    wh = _FakeWaterHeater(_FakeCoordinator(_FakeDevice(operation_status=OperationStatus.ON)))
    _run(wh.async_set_operation_mode(HEATING))
    check(
        "set_operation_mode(HEATING) -> tank.turn_on() called",
        wh.coordinator.device.tank.turned_on is True,
    )
    check(
        "set_operation_mode(HEATING) -> state reflects the new current_operation",
        wh.state == wh._attr_current_operation and wh._attr_current_operation is not None,
        f"state={wh.state!r} current_operation={wh._attr_current_operation!r}",
    )
    check("set_operation_mode(HEATING) -> _attr_state was never touched", wh._attr_state == POISON)

    wh = _FakeWaterHeater(_FakeCoordinator(_FakeDevice(operation_status=OperationStatus.ON)))
    _run(wh.async_set_operation_mode(STATE_OFF))
    check(
        "set_operation_mode(STATE_OFF) -> tank.turn_off() called",
        wh.coordinator.device.tank.turned_off is True,
    )
    check(
        "set_operation_mode(STATE_OFF) -> state is STATE_OFF",
        wh.state == STATE_OFF,
        f"got={wh.state!r}",
    )
    check("set_operation_mode(STATE_OFF) -> _attr_state was never touched", wh._attr_state == POISON)

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
