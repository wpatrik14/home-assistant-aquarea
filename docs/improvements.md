# Aquarea Integration Improvement Plan

This document outlines identified issues, bugs, and proposed architectural improvements for the Home Assistant Aquarea integration, along with a detailed execution plan for each.

---

## 1. Bugs & Critical Issues

### 1.1 Redundant API Calls on Startup
**Description:** In `__init__.py`, the integration calls `coordinator.async_config_entry_first_refresh()` (blocking) and then immediately triggers `coordinator.async_refresh()` via `async_create_task`. This causes two full API fetches back-to-back for every device during startup.
**Execution Plan:**
- Remove the loop in `async_setup_entry` that triggers `coordinator.async_refresh()` after `async_config_entry_first_refresh`.
- Rely solely on the initial refresh and the established `update_interval` for subsequent updates.

### 1.2 Energy Dashboard Data Instability (Total Increasing Resets)
**Description:** `EnergyAccumulatedConsumptionSensor` calculates values by summing monthly entries. If the API returns fewer entries (e.g., due to a temporary cloud sync delay), the sensor value drops, causing Home Assistant's Energy Dashboard to interpret it as a "reset" followed by a massive spike.
**Execution Plan:**
- Modify `_handle_coordinator_update` in `EnergyAccumulatedConsumptionSensor`.
- Add a check to compare the newly calculated `reported_val` with the current `self._attr_native_value`.
- **Condition:** Only update `self._attr_native_value` if the new value is greater than or equal to the current value, OR if the month has changed (resetting for the new billing period).

### 1.3 Inaccurate Optimistic State on `turn_on`
**Description:** `HeatPumpClimate.async_turn_on` defaults to `HVACMode.HEAT`. If the system was previously in `COOL` mode, the UI will flicker incorrectly.
**Execution Plan:**
- Update `async_turn_on` to check the current `device.mode` or a stored "last known mode" if available in the library.
- If the current mode is `OFF`, attempt to retrieve the previous state from the coordinator data before applying the optimistic `HVACMode.HEAT`.

### 1.4 API Pressure from Delayed Refreshes
**Description:** Every climate or water heater adjustment schedules a forced refresh after 5-10 seconds. Rapid user adjustments (e.g., clicking temp +5 times) trigger 5 separate forced API calls.
**Execution Plan:**
- Implement a `_refresh_task` variable (type `asyncio.TimerHandle` or `asyncio.Task`) in the `AquareaDataUpdateCoordinator` or the base entity.
- Create a `async_schedule_delayed_refresh` method that cancels any existing pending refresh task before starting a new one (Debouncing).

---

## 2. Architectural Improvements

### 2.1 Code Cleanup: Remove Redundant Coordinator Logic
**Description:** `AquareaBaseEntity` manually overrides `_handle_coordinator_update` to call `self.async_write_ha_state()`. This is redundant as `CoordinatorEntity` handles this automatically.
**Execution Plan:**
- Remove the `_handle_coordinator_update` implementation from `AquareaBaseEntity`.
- Only override it in specific sensors (like Energy sensors) where custom data processing is required before the state is written.

### 2.2 Dynamic Feature Support (Climate UI)
**Description:** The climate entity hardcodes temperature limits to the current temperature when adjustments are not supported.
**Execution Plan:**
- In `_handle_coordinator_update`, check `zone.supports_set_temperature`.
- Dynamically update `self._attr_supported_features`:
    - Add `ClimateEntityFeature.TARGET_TEMPERATURE` if supported.
    - Remove it if not supported.
- This ensures Home Assistant hides the temperature slider entirely instead of showing a locked slider.

### 2.3 Centralized Token Refresh
**Description:** The coordinator contains repetitive logic to catch `AuthenticationError` and re-login.
**Execution Plan:**
- Refactor `_async_update_data` to use a helper method (e.g., `_async_ensure_logged_in`).
- Alternatively, verify if the `aioaquarea.Client` can be updated to handle transparent token refreshes, reducing complexity in the integration layer.

### 2.4 Disabled Device Optimization
**Description:** Coordinators are created for all devices, even those disabled in the Home Assistant UI.
**Execution Plan:**
- In `async_setup_entry`, check the `entity_registry` to see if the device or its primary entities are disabled.
- Only initialize the coordinator and platforms for active devices to save memory and API quota.

### 2.5 Translation Key Consistency
**Description:** Some sensors use `_attr_name` while others use `translation_key`.
**Execution Plan:**
- Migrate all entities to use `translation_key` exclusively and ensure `strings.json` contains the corresponding entries.
- Remove `_attr_name` where `has_entity_name = True` is used to follow modern Home Assistant standards.
