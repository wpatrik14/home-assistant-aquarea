# Aquarea Smart Cloud Integration Overview

This document provides a detailed technical overview of the Panasonic Aquarea Smart Cloud integration for Home Assistant. It is designed to be used by AI agents and developers to understand the architecture, data flow, and component implementation.

## 1. Architecture Overview

The integration follows the standard Home Assistant integration pattern, utilizing a Data Update Coordinator to manage communication with the Panasonic Aquarea Smart Cloud API via the `aioaquarea` library.

### Key Components:
- **`aioaquarea` Library**: An asynchronous Python library that handles the low-level communication with the Panasonic API, including authentication, device discovery, and command execution.
- **Data Update Coordinator (`coordinator.py`)**: Manages the polling of data from the cloud. It handles rate limiting for consumption data and ensures that all entities have access to the latest device state.
- **Config Flow (`config_flow.py`)**: Handles the user-facing setup process, including credential validation and initial device discovery.
- **Platforms**: Implementation of various Home Assistant platforms:
    - `climate`: Controls heating/cooling zones.
    - `water_heater`: Controls the domestic hot water tank.
    - `sensor`: Provides temperature, energy consumption, and status information.
    - `binary_sensor`: Provides boolean status (e.g., error states).
    - `switch`: Controls specific features like Powerful mode or Eco mode.
    - `select`: Allows selecting operation modes or schedules.
    - `button`: Triggers one-time actions like force-refreshing data.

## 2. Data Flow

### Initialization
1. During `async_setup_entry`, a `aioaquarea.Client` is created.
2. The client logs into the Panasonic service.
3. `client.get_devices()` is called to retrieve all registered heat pumps.
4. For each device, an `AquareaDataUpdateCoordinator` is instantiated.
5. The coordinator performs its first refresh (`async_config_entry_first_refresh`).
6. Platforms are loaded, and entities are created based on the device's capabilities (e.g., if it has a tank, a `water_heater` entity is added).

### Polling (Read)
- The coordinator polls the API at a configurable interval (default: 60 seconds).
- **Device State**: Fetched via `client.get_device()`. This includes temperatures, operation modes, and zone statuses.
- **Consumption Data**: Fetched at a separate interval (default: 60 minutes) to avoid excessive API calls. It retrieves:
    - **Monthly**: one record per day of the month, read by the energy sensors.
    - **Hourly**: one record per hour, labelled in UTC: aioaquarea sends `osTimezone: +00:00`, so the cloud's days and hour labels are UTC (`statistics.CLOUD_TIME_ZONE`; a test pins the offset aioaquarea sends). Today (UTC) is fetched on its own cadence (the consumption interval), independent of the monthly fetch. Yesterday is fetched again, at most hourly, until it has been received at least 3 hours into today, since the cloud publishes its last hours after the day ends. The last three days are kept, the oldest as context only. Used to correct the energy sensors' hourly statistics, see below.
- When the coordinator receives new data, it calls `self.async_set_updated_data(device)`, which triggers `_handle_coordinator_update` in all associated entities.

### Commands (Write)
- When a user changes a state in Home Assistant (e.g., setting a new target temperature):
    1. The entity performs an **optimistic update** of its internal state and calls `self.async_write_ha_state()` to provide immediate UI feedback.
    2. The entity calls the corresponding method on the `aioaquarea.Device` object (e.g., `device.set_temperature`).
    3. A **delayed refresh** is scheduled (usually 5-10 seconds). This is crucial because the Panasonic API often takes several seconds to reflect changes in its status response. The delayed refresh ensures the UI eventually shows the confirmed state from the cloud.

## 3. Entity Implementation Details

### Climate (`climate.py`)
- **Zones**: Each physical zone in the Aquarea system is represented as a separate `climate` entity.
- **HVAC Modes**: Maps Home Assistant `HVACMode` (HEAT, COOL, AUTO, OFF) to Aquarea's `UpdateOperationMode`.
- **Presets**: Supports `ECO`, `COMFORT`, and `NONE` via the `special_status` feature of the heat pump.
- **Temperature Control**: Only enabled if the zone supports it (`zone.supports_set_temperature`).

### Water Heater (`water_heater.py`)
- **Tank Control**: Represents the domestic hot water tank.
- **Operation Modes**: Supports `heating` and `off`.
- **Active State**: Determines if the tank is actively being heated by checking `device.current_direction` (WATER) or `device.current_action`.

### Sensors (`sensor.py`)
- **Temperature Sensors**: Outdoor temperature and zone temperatures.
- **Energy Sensors**:
    - **Accumulated Consumption**: Uses `TOTAL_INCREASING` state class. It restores state from Home Assistant's database to maintain a continuous total even if the cloud data resets or the integration restarts.
    - **Hourly Consumption**: Provides the energy used in the last processed hour.
    - **Hourly statistics** (`statistics.py`): the cloud publishes an hour's consumption only after the hour has closed, so the recorder files each increase one or two hours late. After every hourly consumption fetch, each energy sensor rewrites its own past hourly statistics rows (same statistic id, so the Energy dashboard needs no change). It matches the energy recorded in each hour, oldest first, to the cloud hours up to and including it (the sensors' daily figures include the hour in progress), at most 3 hours back (longer for consumption intervals over an hour), working from the recorder's 5-minute statistics, which it never writes. The most recent hourly row is never rewritten and comes out unchanged, so the recorder carries on from the same total. Energy lost at a sensor reset is not matched, and energy the cloud does not label stays where it was recorded.
- **Direction Sensor**: Indicates whether the heat pump is currently servicing the zones (PUMP) or the water tank (WATER).

## 4. Error Handling and Authentication
- **Authentication**: The integration handles token expiration by catching `AuthenticationError` and re-logging automatically during the update cycle.
- **Request Failures**: Communication errors are caught and logged, and the coordinator marks the update as failed, which results in entities showing as "Unavailable" in Home Assistant until connectivity is restored.

## 5. Technical Constraints
- **Cloud Polling**: The integration is subject to the latency and availability of the Panasonic Smart Cloud.
- **API Propagation**: Changes made via the API are not instantaneous. The integration uses delayed refreshes to mitigate this.
- **Rate Limiting**: Consumption data is fetched less frequently than status data to stay within reasonable API usage limits.
