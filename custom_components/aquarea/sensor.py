"""Adds Aquarea sensors."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Self

from aioaquarea import ConsumptionType

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
    consumption_type: ConsumptionType
    exists_fn: Callable[[AquareaDataUpdateCoordinator], bool] = lambda _: True

ACCUMULATED_ENERGY_SENSORS = [
    AquareaEnergyConsumptionSensorDescription(
        key="heating_accumulated_energy_consumption",
        translation_key="heating_accumulated_energy_consumption",
        name="Heating Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.HEAT,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="cooling_accumulated_energy_consumption",
        translation_key="cooling_accumulated_energy_consumption",
        name="Cooling Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.COOL,
        exists_fn=lambda coordinator: any(zone.cool_mode for zone in coordinator.device.zones.values()),
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="tank_accumulated_energy_consumption",
        translation_key="tank_accumulated_energy_consumption",
        name="Tank Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.WATER_TANK,
        exists_fn=lambda coordinator: coordinator.device.has_tank,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="accumulated_energy_consumption",
        translation_key="accumulated_energy_consumption",
        name="Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.TOTAL,
    ),
]

ENERGY_SENSORS = [
    AquareaEnergyConsumptionSensorDescription(
        key="heating_energy_consumption",
        translation_key="heating_energy_consumption",
        name="Heating Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.HEAT,
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="tank_energy_consumption",
        translation_key="tank_energy_consumption",
        name="Tank Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.WATER_TANK,
        exists_fn=lambda coordinator: coordinator.device.has_tank,
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="cooling_energy_consumption",
        translation_key="cooling_energy_consumption",
        name="Cooling Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.COOL,
        exists_fn=lambda coordinator: any(zone.cool_mode for zone in coordinator.device.zones.values()),
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="energy_consumption",
        translation_key="energy_consumption",
        name="Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=ConsumptionType.TOTAL,
        entity_registry_enabled_default=False,
    ),
]

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data: dict[str, AquareaDataUpdateCoordinator] = hass.data[DOMAIN][config_entry.entry_id][DEVICES]
    entities: list[SensorEntity] = []
    for coordinator in data.values():
        entities.append(OutdoorTemperatureSensor(coordinator))
        entities.extend([EnergyAccumulatedConsumptionSensor(description, coordinator) for description in ACCUMULATED_ENERGY_SENSORS if description.exists_fn(coordinator)])
        entities.extend([EnergyConsumptionSensor(description, coordinator) for description in ENERGY_SENSORS if description.exists_fn(coordinator)])
    async_add_entities(entities)

@dataclass
class AquareaSensorExtraStoredData(SensorExtraStoredData):
    period_being_processed: datetime | None = None

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self:
        sensor_data = super().from_dict(restored)
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            period_being_processed=dt_util.parse_datetime(restored["period_being_processed"]) if "period_being_processed" in restored else None,
        )

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        if self.period_being_processed is not None:
            data["period_being_processed"] = dt_util.as_local(self.period_being_processed).isoformat()
        return data

@dataclass
class AquareaAccumulatedSensorExtraStoredData(AquareaSensorExtraStoredData):
    accumulated_period_being_processed: float | None = None

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self:
        sensor_data = super().from_dict(restored)
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            period_being_processed=sensor_data.period_being_processed,
            accumulated_period_being_processed=restored.get("accumulated_period_being_processed"),
        )

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data["accumulated_period_being_processed"] = self.accumulated_period_being_processed
        return data

class OutdoorTemperatureSensor(AquareaBaseEntity, SensorEntity):
    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = "outdoor_temperature"
        self._attr_unique_id = f"{super().unique_id}_outdoor_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug("Updating sensor '%s' of %s", "outdoor_temperature", self.coordinator.device_info.name)
        self._attr_native_value = self.coordinator.device.temperature_outdoor
        super()._handle_coordinator_update()

class EnergyAccumulatedConsumptionSensor(AquareaBaseEntity, SensorEntity, RestoreEntity):
    entity_description: AquareaEnergyConsumptionSensorDescription

    def __init__(self, description: AquareaEnergyConsumptionSensorDescription, coordinator: AquareaDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{description.key}"
        self._period_being_processed: datetime | None = None
        self._accumulated_period_being_processed: float | None = None
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        sensor_data = await self.async_get_last_sensor_data()
        if sensor_data is not None:
            self._attr_native_value = sensor_data.native_value
            self._period_being_processed = sensor_data.period_being_processed
            self._accumulated_period_being_processed = sensor_data.accumulated_period_being_processed
        if self._attr_native_value is None:
            self._attr_native_value = 0
        if self._accumulated_period_being_processed is None:
            self._accumulated_period_being_processed = 0
        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> AquareaAccumulatedSensorExtraStoredData:
        return AquareaAccumulatedSensorExtraStoredData(self.native_value, self.native_unit_of_measurement, self.period_being_processed, self._accumulated_period_being_processed)

    async def async_get_last_sensor_data(self) -> AquareaAccumulatedSensorExtraStoredData | None:
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return AquareaAccumulatedSensorExtraStoredData.from_dict(restored_last_extra_data.as_dict())

    @property
    def period_being_processed(self) -> datetime | None:
        return self._period_being_processed

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug("Updating sensor '%s' of %s", self.unique_id, self.coordinator.device_info.name)
        month_consumption = self.coordinator.month_consumption
        if not month_consumption:
            self._attr_native_value = None
        else:
            now = dt_util.now()
            month_heat = month_cool = month_tank = month_total = 0.0
            for c in month_consumption:
                try:
                    dt_str = c.data_time
                    if not dt_str:
                        continue
                    item_date = datetime.strptime(dt_str, "%Y%m%d").date()
                    if item_date <= now.date():
                        month_heat += float(c.heat_consumption or 0.0)
                        month_cool += float(c.cool_consumption or 0.0)
                        month_tank += float(c.tank_consumption or 0.0)
                        try:
                            month_total += float(c.total_consumption or 0.0)
                        except (ValueError, TypeError):
                            month_total += float((c.heat_consumption or 0.0) + (c.cool_consumption or 0.0) + (c.tank_consumption or 0.0))
                except (ValueError, TypeError) as e:
                    _LOGGER.exception("Failed to parse month consumption item date: %s, error: %s", getattr(c, "data_time", None), e)

            _LOGGER.debug("Coordinator-provided month-to-date (kWh) - heat: %.3f, cool: %.3f, water_tank: %.3f, total: %.3f", month_heat, month_cool, month_tank, month_total)
            ctype = self.entity_description.consumption_type
            reported_val = None
            if ctype == ConsumptionType.HEAT:
                reported_val = month_heat
            elif ctype == ConsumptionType.COOL:
                reported_val = month_cool
            elif ctype == ConsumptionType.WATER_TANK:
                reported_val = month_tank
            elif ctype == ConsumptionType.TOTAL:
                reported_val = month_total
            if reported_val is not None:
                month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                self._period_being_processed = month_start
                self._attr_native_value = reported_val
        super()._handle_coordinator_update()

class EnergyConsumptionSensor(AquareaBaseEntity, SensorEntity, RestoreEntity):
    entity_description: AquareaEnergyConsumptionSensorDescription

    def __init__(self, description: AquareaEnergyConsumptionSensorDescription, coordinator: AquareaDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{description.key}"
        self._period_being_processed: datetime | None = None
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        sensor_data = await self.async_get_last_sensor_data()
        if sensor_data is not None:
            self._attr_native_value = sensor_data.native_value
            self._period_being_processed = sensor_data.period_being_processed
        if self._attr_native_value is None:
            self._attr_native_value = 0
        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> AquareaSensorExtraStoredData:
        return AquareaSensorExtraStoredData(self.native_value, self.native_unit_of_measurement, self.period_being_processed)

    async def async_get_last_sensor_data(self) -> AquareaSensorExtraStoredData | None:
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return AquareaSensorExtraStoredData.from_dict(restored_last_extra_data.as_dict())

    @property
    def period_being_processed(self) -> datetime | None:
        return self._period_being_processed

    @callback
    def _handle_coordinator_update(self) -> None:
        _LOGGER.debug("Updating sensor '%s' of %s", self.unique_id, self.coordinator.device_info.name)
        day_consumption = self.coordinator.day_consumption
        if not day_consumption:
            self._attr_native_value = None
        else:
            now = dt_util.now().replace(minute=0, second=0, microsecond=0)
            current_hour = now.hour
            current_entry = None
            for c in day_consumption:
                dt_str = c.data_time
                if not dt_str:
                    continue
                try:
                    _LOGGER.debug("Parsing day consumption item date: %s", dt_str)
                    item_dt = datetime.strptime(dt_str, "%Y%m%d %H")
                    _LOGGER.debug("Parsed item_dt: %s, now.date(): %s, current_hour: %s", item_dt, now.date(), current_hour)
                    if item_dt.date() == now.date() and item_dt.hour == current_hour:
                        current_entry = c
                        _LOGGER.debug("Found current_entry for current hour: %s", current_entry)
                        break
                except (ValueError, TypeError) as e:
                    _LOGGER.debug("Failed to parse day consumption item date: %s, error: %s", dt_str, e)
            if current_entry:
                pass
            else:
                # If current hour's data is not available, try to get the last available hour's data
                for c in reversed(day_consumption):
                    dt_str = c.data_time
                    if not dt_str:
                        continue
                    try:
                        item_dt = datetime.strptime(dt_str, "%Y%m%d %H")
                        current_entry = c
                        break
                    except (ValueError, TypeError) as e:
                        _LOGGER.debug("Failed to parse day consumption item date: %s, error: %s", dt_str, e)

            if current_entry:
                _LOGGER.debug("Current entry consumption values - heat: %s, cool: %s, tank: %s, total: %s", current_entry.heat_consumption, current_entry.cool_consumption, current_entry.tank_consumption, current_entry.total_consumption)
                ctype = self.entity_description.consumption_type
                reported_val = None
                if ctype == ConsumptionType.HEAT:
                    reported_val = float(current_entry.heat_consumption or 0.0)
                elif ctype == ConsumptionType.COOL:
                    reported_val = float(current_entry.cool_consumption or 0.0)
                elif ctype == ConsumptionType.WATER_TANK:
                    reported_val = float(current_entry.tank_consumption or 0.0)
                elif ctype == ConsumptionType.TOTAL:
                    try:
                        reported_val = float(current_entry.total_consumption or 0.0)
                    except Exception:
                        reported_val = float((current_entry.heat_consumption or 0.0) + (current_entry.cool_consumption or 0.0) + (current_entry.tank_consumption or 0.0))
                self._period_being_processed = now
                self._attr_native_value = reported_val
            else:
                self._attr_native_value = None
        super()._handle_coordinator_update()
