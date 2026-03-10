# Refactoring Proposal: Optimized Fetching Mechanism for Aquarea Integration

## Current Behavior Analysis

The current `AquareaDataUpdateCoordinator` in [`custom_components/aquarea/coordinator.py`](custom_components/aquarea/coordinator.py) uses a single `DataUpdateCoordinator` with a fixed `update_interval` (defaulting to 60 seconds). 

Inside `_async_update_data`, it performs the following:
1.  **Device Data Fetching**: Calls `self._client.get_device()` which retrieves the main device state.
2.  **Zone Data Refresh**: Calls `await self._device.refresh_data()` immediately after fetching the device.
3.  **Consumption Fetching**: Uses a simple time-based check (`self.consumption_interval`) to fetch both daily (hourly) and monthly consumption in parallel.

### Issues with Current Approach:
- **Redundancy**: `get_device` and `refresh_data` are often called in sequence.
- **Coarse Granularity**: All consumption data (daily and monthly) is fetched at the same interval.
- **Inefficiency**: The coordinator doesn't distinguish between high-priority data and low-priority data.

---

## Proposed Refactoring

The goal is to implement a multi-tiered fetching strategy with the following fixed and configurable intervals:
- **Main Device Details**: Fixed at **1 minute**.
- **Zone Details (Zone 1 + Zone 2)**: Fixed at **1 minute**.
- **Current Consumption (Daily)**: Fixed at **15 minutes**.
- **Accumulated Consumption (Monthly)**: Configurable (default **1 hour**).

### 1. Tiered Update Logic in `_async_update_data`

The `DataUpdateCoordinator` will be initialized with a fixed `update_interval` of **1 minute**. Inside `_async_update_data`, we will use the current time to decide what else needs to be fetched.

```python
async def _async_update_data(self) -> None:
    now = dt_util.now()
    
    # 1. Fetch Main Device & Zone Details (Every 1 minute - the coordinator's tick)
    await self._fetch_device_and_zones()

    # 2. Fetch Daily Consumption (Every 15 minutes)
    if self._should_fetch_daily(now):
        await self._fetch_daily_consumption(now)

    # 3. Fetch Monthly Consumption (Configurable interval)
    if self._should_fetch_monthly(now):
        await self._fetch_monthly_consumption(now)
```

### 2. Configuration Integration

The `config_flow.py` and `const.py` will be updated to reflect these changes:
- **`CONF_SCAN_INTERVAL`**: Will be removed or hidden from the user, as it's now fixed at 1 minute.
- **`CONF_CONSUMPTION_INTERVAL`**: Will be renamed or repurposed to specifically control the **Accumulated Consumption (Monthly)** interval.

### 3. Code Changes in `coordinator.py`

- **Tracking Variables**:
    - `self._last_daily_fetch: datetime | None = None`
    - `self._last_monthly_fetch: datetime | None = None`
- **Helper Methods**:
    - `_fetch_device_and_zones()`: Combines the logic of getting device and refreshing zones.
    - `_fetch_daily_consumption()`: Specifically for `DateType.DAY`.
    - `_fetch_monthly_consumption()`: Specifically for `DateType.MONTH`.
- **Parallelization**: When multiple intervals coincide (e.g., at the top of the hour), use `asyncio.gather` to minimize total wait time.

### 4. Benefits
- **Reduced API Load**: Monthly data is fetched less frequently than daily data.
- **Better Responsiveness**: Daily consumption is updated more frequently (15m vs 60m).
- **Simplified Configuration**: Users only need to configure the interval for the least frequent data (monthly consumption).

---

## Implementation Plan

1.  **Modify `const.py`**:
    - Set `DEFAULT_SCAN_INTERVAL = 60` (fixed).
    - Set `DEFAULT_DAILY_CONSUMPTION_INTERVAL = 15` (fixed).
    - Keep `DEFAULT_CONSUMPTION_INTERVAL = 60` (configurable for monthly).
2.  **Update `AquareaDataUpdateCoordinator`**:
    - Initialize `_last_daily_fetch` and `_last_monthly_fetch`.
    - Refactor `_async_update_data` to use these timestamps.
    - Implement logic to fetch daily data every 15 minutes and monthly data every `consumption_interval` (default 60 minutes).
3.  **Update `config_flow.py`**:
    - Remove `CONF_SCAN_INTERVAL` from the user form.
    - Update the label for `CONF_CONSUMPTION_INTERVAL` to clarify it's for monthly consumption.
4.  **Update Sensors**: Ensure sensors using this data are compatible with the new update frequencies.
