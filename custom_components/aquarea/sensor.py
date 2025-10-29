"""Adds Aquarea sensors."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Self

from aioaquarea import ConsumptionType, DataNotAvailableError
from aioaquarea.statistics import DateType

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import AquareaBaseEntity
from .const import DEVICES, DOMAIN
from .coordinator import AquareaDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

@dataclass(kw_only=True)
class AquareaEnergyConsumptionSensorDescription(SensorEntityDescription):
    """Entity Description for Aquarea Energy Consumption Sensors."""

    consumption_type: ConsumptionType
    exists_fn: Callable[[AquareaDataUpdateCoordinator],bool] = lambda _: True

ACCUMULATED_ENERGY_SENSORS: list[AquareaEnergyConsumptionSensorDescription] = [
    AquareaEnergyConsumptionSensorDescription(
        key="heating_accumulated_energy_consumption",
        translation_key="heating_accumulated_energy_consumption",
        name="Heating Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.HEAT,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="cooling_accumulated_energy_consumption",
        translation_key="cooling_accumulated_energy_consumption",
        name= "Cooling Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.COOL,
        exists_fn=lambda coordinator: any(zone.cool_mode for zone in coordinator.device.zones.values())
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="tank_accumulated_energy_consumption",
        translation_key = "tank_accumulated_energy_consumption",
        name= "Tank Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.WATER_TANK,
        exists_fn=lambda coordinator: coordinator.device.has_tank
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="accumulated_energy_consumption",
        translation_key="accumulated_energy_consumption",
        name= "Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.TOTAL
    ),
]

ENERGY_SENSORS: list[AquareaEnergyConsumptionSensorDescription] = [
    AquareaEnergyConsumptionSensorDescription(
        key="heating_energy_consumption",
        translation_key="heating_energy_consumption",
        name="Heating Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.HEAT,
        entity_registry_enabled_default=False
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="tank_energy_consumption",
        translation_key="tank_energy_consumption",
        name= "Tank Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.WATER_TANK,
        exists_fn=lambda coordinator: coordinator.device.has_tank,
        entity_registry_enabled_default=False
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="cooling_energy_consumption",
        translation_key="cooling_energy_consumption",
        name= "Cooling Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.COOL,
        exists_fn=lambda coordinator: any(zone.cool_mode for zone in coordinator.device.zones.values()),
        entity_registry_enabled_default=False
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="energy_consumption",
        translation_key="energy_consumption",
        name= "Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.TOTAL,
        entity_registry_enabled_default=False
    ),
]

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea sensors from config entry."""

    data: dict[str, AquareaDataUpdateCoordinator] = hass.data[DOMAIN][
        config_entry.entry_id
    ][DEVICES]

    entities: list[SensorEntity] = []

    for coordinator in data.values():
        entities.append(OutdoorTemperatureSensor(coordinator))
        entities.extend(
            [
                EnergyAccumulatedConsumptionSensor(description,coordinator)
                for description in ACCUMULATED_ENERGY_SENSORS
                if description.exists_fn(coordinator)
            ]
        )
        entities.extend(
            [
                EnergyConsumptionSensor(description,coordinator)
                for description in ENERGY_SENSORS
                if description.exists_fn(coordinator)
            ]
        )

    async_add_entities(entities)


@dataclass
class AquareaSensorExtraStoredData(SensorExtraStoredData):
    """Class to hold Aquarea sensor specific state data."""

    period_being_processed: datetime | None = None

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self:
        """Return AquareaSensorExtraStoredData from dict."""
        sensor_data = super().from_dict(restored)
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            period_being_processed=dt_util.parse_datetime(
                restored.get("period_being_processed","")
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return AquareaSensorExtraStoredData as dict."""
        data = super().as_dict()

        if self.period_being_processed is not None:
            data["period_being_processed"] = dt_util.as_local(
                self.period_being_processed
            ).isoformat()

        return data


@dataclass
class AquareaAccumulatedSensorExtraStoredData(AquareaSensorExtraStoredData):
    """Class to hold Aquarea sensor specific state data."""

    accumulated_period_being_processed: float | None = None

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self:
        """Return AquareaSensorExtraStoredData from dict."""
        sensor_data = super().from_dict(restored)
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            period_being_processed=sensor_data.period_being_processed,
            accumulated_period_being_processed=restored[
                "accumulated_period_being_processed"
            ],
        )

    def as_dict(self) -> dict[str, Any]:
        """Return AquareaAccumulatedSensorExtraStoredData as dict."""
        data = super().as_dict()
        data[
            "accumulated_period_being_processed"
        ] = self.accumulated_period_being_processed
        return data


class OutdoorTemperatureSensor(AquareaBaseEntity, SensorEntity):
    """Representation of a Aquarea sensor."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize outdoor temperature sensor."""
        super().__init__(coordinator)

        self._attr_translation_key = "outdoor_temperature"
        self._attr_unique_id = f"{super().unique_id}_outdoor_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "Updating sensor '%s' of %s",
            "outdoor_temperature",
            self.coordinator.device_info.name,
        )

        self._attr_native_value = self.coordinator.device.temperature_outdoor
        super()._handle_coordinator_update()

class EnergyAccumulatedConsumptionSensor(
    AquareaBaseEntity, SensorEntity, RestoreEntity
):
    """Representation of a Aquarea sensor."""

    entity_description: AquareaEnergyConsumptionSensorDescription

    def __init__(self, description: AquareaEnergyConsumptionSensorDescription, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize an accumulated energy consumption sensor."""
        super().__init__(coordinator)
 
        self._attr_unique_id = (
            f"{super().unique_id}_{description.key}"
        )
        self._period_being_processed: datetime | None = None
        self._accumulated_period_being_processed: float | None = None
        self.entity_description = description
        # Track last hour we called the consumption API to ensure we call it at most once per hour
        # Format uses YYYYMMDDHH to avoid duplicate calls when date/hour crosses.
        self._last_api_hour: str | None = None

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        if (sensor_data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = sensor_data.native_value
            self._period_being_processed = sensor_data.period_being_processed
            self._accumulated_period_being_processed = (
                sensor_data.accumulated_period_being_processed
            )

        if self._attr_native_value is None:
            self._attr_native_value = 0

        if self._accumulated_period_being_processed is None:
            self._accumulated_period_being_processed = 0

        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> AquareaAccumulatedSensorExtraStoredData:
        """Return sensor specific state data to be restored."""
        return AquareaAccumulatedSensorExtraStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            self.period_being_processed,
        )

    async def async_get_last_sensor_data(
        self,
    ) -> AquareaAccumulatedSensorExtraStoredData | None:
        """Restore native_value and native_unit_of_measurement."""
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return AquareaAccumulatedSensorExtraStoredData.from_dict(
            restored_last_extra_data.as_dict()
        )

    @property
    def period_being_processed(self) -> datetime | None:
        """Return the period being processed."""
        return self._period_being_processed

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "Updating sensor '%s' of %s",
            self.unique_id,
            self.coordinator.device_info.name,
        )

        # Trigger monthly API fetch once per hour; the async helper will update the entity when data is available.
        current_hour = dt_util.now().strftime("%Y%m%d%H")
        if self._last_api_hour != current_hour:
            self._last_api_hour = current_hour
            try:
                self.hass.async_create_task(self._async_fetch_month_consumption_and_apply())
            except Exception:
                _LOGGER.exception("Failed to schedule async month consumption fetch")
        # No immediate state change here; monthly fetch will call _handle_coordinator_update when data is applied.
        super()._handle_coordinator_update()
        return

    async def _async_fetch_month_consumption_and_apply(self) -> None:
        """Async helper to fetch month-to-date consumption and apply totals.

        Fetches monthly consumption entries via the client and sums entries up to today
        to produce month-to-date totals broken down by heat/cool/tank and a total.
        The result is applied to the accumulated sensors (for the matching consumption_type).
        """
        try:
            now = dt_util.now()
            # YYYYMM01 to request the month aggregation from the API
            month_date_str = now.strftime("%Y%m01")
            _LOGGER.info("Fetching month consumption for %s", month_date_str)

            month_consumption = await self.coordinator._client.get_device_consumption(
                self.coordinator.device.long_id, DateType.MONTH, month_date_str
            )

            if not month_consumption:
                _LOGGER.warning("No month consumption data returned for %s", month_date_str)
                return

            month_heat = 0.0
            month_cool = 0.0
            month_tank = 0.0
            month_total = 0.0

            for c in month_consumption:
                try:
                    dt_str = c.data_time
                    if not dt_str:
                        continue
                    # dataTime for month mode is expected as YYYYMMDD
                    item_date = datetime.strptime(dt_str, "%Y%m%d").date()
                    if item_date <= now.date():
                        month_heat += float(c.heat_consumption or 0.0)
                        month_cool += float(c.cool_consumption or 0.0)
                        month_tank += float(c.tank_consumption or 0.0)
                        # total_consumption may be provided; otherwise sum parts per item
                        try:
                            month_total += float(c.total_consumption or 0.0)
                        except Exception:
                            month_total += float(
                                (c.heat_consumption or 0.0)
                                + (c.cool_consumption or 0.0)
                                + (c.tank_consumption or 0.0)
                            )
                except Exception:
                    _LOGGER.exception(
                        "Failed to parse month consumption item date: %s", getattr(c, "data_time", None)
                    )

            _LOGGER.info(
                "Month-to-date consumption (kWh) - heat: %.3f, cool: %.3f, water_tank: %.3f, total: %.3f",
                month_heat,
                month_cool,
                month_tank,
                month_total,
            )

            # Map sensor consumption_type to the appropriate reported value
            reported_val = None
            ctype = self.entity_description.consumption_type
            if ctype == ConsumptionType.HEAT:
                reported_val = month_heat
            elif ctype == ConsumptionType.COOL:
                reported_val = month_cool
            elif ctype == ConsumptionType.WATER_TANK:
                reported_val = month_tank
            elif ctype == ConsumptionType.TOTAL:
                reported_val = month_total

            if reported_val is not None:
                # Set period being processed to the start of the month
                month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                self._period_being_processed = month_start
                self._attr_native_value = reported_val
                # write state via base handler
                super()._handle_coordinator_update()

        except Exception:
            _LOGGER.exception("Error fetching month consumption")

class EnergyConsumptionSensor(AquareaBaseEntity, SensorEntity, RestoreEntity):
    """Representation of a Aquarea sensor."""

    entity_description: AquareaEnergyConsumptionSensorDescription

    def __init__(self, description: AquareaEnergyConsumptionSensorDescription, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize an accumulated energy consumption sensor."""
        super().__init__(coordinator)
 
        self._attr_unique_id = (
            f"{super().unique_id}_{description.key}"
        )
        self._period_being_processed: datetime | None = None
        self.entity_description = description
        # Track last hour we called the consumption API to ensure we call it at most once per hour
        # Format uses YYYYMMDDHH to avoid duplicate calls when date/hour crosses.
        self._last_api_hour: str | None = None

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        if (sensor_data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = sensor_data.native_value
            self._period_being_processed = sensor_data.period_being_processed

        if self._attr_native_value is None:
            self._attr_native_value = 0

        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> AquareaSensorExtraStoredData:
        """Return sensor specific state data to be restored."""
        return AquareaSensorExtraStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            self.period_being_processed,
        )

    async def async_get_last_sensor_data(self) -> AquareaSensorExtraStoredData | None:
        """Restore native_value and native_unit_of_measurement."""
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return AquareaSensorExtraStoredData.from_dict(
            restored_last_extra_data.as_dict()
        )

    @property
    def period_being_processed(self) -> datetime | None:
        """Return the period being processed."""
        return self._period_being_processed

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "Updating sensor '%s' of %s",
            self.unique_id,
            self.coordinator.device_info.name,
        )

        # Trigger hourly API fetch once per hour; the async helper will update the entity when data is available.
        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        previous_hour = now - timedelta(hours=1)
        current_hour = dt_util.now().strftime("%Y%m%d%H")
        if self._last_api_hour != current_hour:
            self._last_api_hour = current_hour
            try:
                self.hass.async_create_task(self._async_fetch_day_consumption_and_apply(now, previous_hour))
            except Exception:
                _LOGGER.exception("Failed to schedule async day consumption fetch")
        # No immediate state change here; hourly fetch will update the entity asynchronously.
        super()._handle_coordinator_update()
        return

    async def _async_fetch_day_consumption_and_apply(self, now: datetime, previous_hour: datetime) -> None:
        """Async helper to fetch day/hourly consumption and apply current/previous hour values.

        This fetches hourly/day consumption via the client and, when an entry for the
        current hour (or previous hour) is present, writes the value to the entity.
        """
        try:
            date_str = now.strftime("%Y%m%d")
            current_hour = now.hour
            _LOGGER.info(
                "Fetching day (hourly/daily) consumption for date %s (current hour %02d)",
                date_str,
                current_hour,
            )

            # Attempt to fetch hourly/day consumption entries from the client
            day_consumption = await self.coordinator._client.get_device_consumption(
                self.coordinator.device.long_id, DateType.DAY, date_str
            )

            # Default values (explicit zeros if missing)
            heat_val = 0.0
            cool_val = 0.0
            tank_val = 0.0
            total_val = 0.0
            data_time_val = None
            raw_val = None

            current_entry = None

            if day_consumption:
                # Try to find the entry for the current hour using a few common data_time formats
                for c in day_consumption:
                    dt_str = c.data_time
                    if not dt_str:
                        continue
                    # Try YYYYMMDDHH
                    try:
                        item_dt = datetime.strptime(dt_str, "%Y%m%d%H")
                        if item_dt.date() == now.date() and item_dt.hour == current_hour:
                            current_entry = c
                            break
                    except Exception:
                        pass
                    # Try HH or H
                    try:
                        if len(dt_str) <= 2 and dt_str.isdigit():
                            if int(dt_str) == current_hour:
                                current_entry = c
                                break
                    except Exception:
                        pass

            # Populate values from found entries
            if current_entry:
                heat_val = float(current_entry.heat_consumption or 0.0)
                cool_val = float(current_entry.cool_consumption or 0.0)
                tank_val = float(current_entry.tank_consumption or 0.0)
                # total_consumption may be provided by the model, otherwise sum parts
                try:
                    total_val = float(current_entry.total_consumption or 0.0)
                except Exception:
                    total_val = heat_val + cool_val + tank_val
                data_time_val = current_entry.data_time
                raw_val = current_entry.raw_data if hasattr(current_entry, "raw_data") else getattr(current_entry, "_data", {})

            # Do not fallback to previous hour entry — only use the current hour entry when present.

            # Map sensor consumption_type to the appropriate reported value only if a current hour entry was found
            reported_val = None
            ctype = self.entity_description.consumption_type
            if current_entry:
                if ctype == ConsumptionType.HEAT:
                    reported_val = heat_val
                elif ctype == ConsumptionType.COOL:
                    reported_val = cool_val
                elif ctype == ConsumptionType.WATER_TANK:
                    reported_val = tank_val
                elif ctype == ConsumptionType.TOTAL:
                    reported_val = total_val

            # Always log the current-hour values (zeros if missing)
            _LOGGER.info(
                "Current hour consumption (kWh) - heat: %.3f, cool: %.3f, water_tank: %.3f, total: %.3f; data_time=%s raw=%s",
                heat_val,
                cool_val,
                tank_val,
                total_val,
                data_time_val or "N/A",
                raw_val or {},
            )

            # If we have a value for this sensor, apply it and write state
            if reported_val is not None:
                # Set period being processed to the current hour (we no longer fallback to previous hour)
                if current_entry:
                    self._period_being_processed = now
                    self._attr_native_value = reported_val
                    # Use the base handler to write state
                    super()._handle_coordinator_update()
        except Exception:
            _LOGGER.exception("Error fetching/applying day consumption")
