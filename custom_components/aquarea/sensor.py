"""Adds Aquarea sensors."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
import math
from typing import Any, Self

import aioaquarea
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPressure, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import LIFETIME_TODAY_SETTLE
from .coordinator import AquareaConfigEntry, AquareaDataUpdateCoordinator
from .entity import AquareaBaseEntity
from .lifetime import local_day_bounds
from .statistics import (
    CLOUD_TIME_ZONE,
    async_redistribute_hourly_statistics,
    cloud_date,
    hourly_consumption,
    max_match_lag,
)

_LOGGER = logging.getLogger(__name__)

# Read-only: the coordinator polls, entities only read its data.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AquareaEnergyConsumptionSensorDescription(SensorEntityDescription):
    """Describes an energy consumption sensor and which consumption it reports."""

    consumption_type: aioaquarea.ConsumptionType
    exists_fn: Callable[[AquareaDataUpdateCoordinator], bool] = lambda _: True


ACCUMULATED_ENERGY_SENSORS = [
    AquareaEnergyConsumptionSensorDescription(
        key="heating_accumulated_energy_consumption",
        translation_key="heating_accumulated_energy_consumption",
        name="Heating Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.HEAT,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="cooling_accumulated_energy_consumption",
        translation_key="cooling_accumulated_energy_consumption",
        name="Cooling Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.COOL,
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="tank_accumulated_energy_consumption",
        translation_key="tank_accumulated_energy_consumption",
        name="Tank Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.WATER_TANK,
        exists_fn=lambda coordinator: coordinator.device.has_tank,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="accumulated_energy_consumption",
        translation_key="accumulated_energy_consumption",
        name="Accumulated Consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.TOTAL,
    ),
]


def _accumulated_variant(
    description: AquareaEnergyConsumptionSensorDescription,
    *,
    key: str,
    name: str,
    enabled: bool | None = None,
    **kwargs: Any,
) -> AquareaEnergyConsumptionSensorDescription:
    """Return a description for a sensor that mirrors an accumulated one.

    It reports the same consumption type, for the same devices, and is
    enabled by default on the same terms (the cooling one is not) unless
    ``enabled`` says otherwise.
    """
    return AquareaEnergyConsumptionSensorDescription(
        key=key,
        translation_key=key,
        name=name,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=description.consumption_type,
        exists_fn=description.exists_fn,
        entity_registry_enabled_default=(
            description.entity_registry_enabled_default if enabled is None else enabled
        ),
        **kwargs,
    )


# All the energy since the first record of the cloud, for the Energy dashboard
# (see lifetime.py). Unknown until the history has been read the first time.
LIFETIME_ENERGY_SENSORS = [
    _accumulated_variant(
        description,
        key=key,
        name=name,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )
    for description, key, name in zip(
        ACCUMULATED_ENERGY_SENSORS,
        (
            "heating_lifetime_energy_consumption",
            "cooling_lifetime_energy_consumption",
            "tank_lifetime_energy_consumption",
            "lifetime_energy_consumption",
        ),
        (
            "Heating Lifetime Consumption",
            "Cooling Lifetime Consumption",
            "Tank Lifetime Consumption",
            "Lifetime Consumption",
        ),
        strict=True,
    )
]

# The energy of the local calendar day, from the cloud's hourly records. TOTAL
# with a last_reset at the local midnight: it counts back from zero every day.
# Only the total is enabled by default.
TODAY_ENERGY_SENSORS = [
    _accumulated_variant(
        description,
        key=key,
        name=name,
        enabled=description.consumption_type == aioaquarea.ConsumptionType.TOTAL,
        state_class=SensorStateClass.TOTAL,
    )
    for description, key, name in zip(
        ACCUMULATED_ENERGY_SENSORS,
        (
            "heating_today_energy_consumption",
            "cooling_today_energy_consumption",
            "tank_today_energy_consumption",
            "today_energy_consumption",
        ),
        (
            "Heating Consumption Today",
            "Cooling Consumption Today",
            "Tank Consumption Today",
            "Consumption Today",
        ),
        strict=True,
    )
]

# The energy published since the previous fetch. The value drops back to 0, which rules
# out TOTAL_INCREASING, and Home Assistant does not accept the ENERGY device
# class with MEASUREMENT, so these sensors have no device class.
ENERGY_SENSORS = [
    AquareaEnergyConsumptionSensorDescription(
        key="heating_energy_consumption",
        translation_key="heating_energy_consumption",
        name="Heating Consumption",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.HEAT,
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="tank_energy_consumption",
        translation_key="tank_energy_consumption",
        name="Tank Consumption",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.WATER_TANK,
        exists_fn=lambda coordinator: coordinator.device.has_tank,
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="cooling_energy_consumption",
        translation_key="cooling_energy_consumption",
        name="Cooling Consumption",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.COOL,
        entity_registry_enabled_default=False,
    ),
    AquareaEnergyConsumptionSensorDescription(
        key="energy_consumption",
        translation_key="energy_consumption",
        name="Consumption",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        consumption_type=aioaquarea.ConsumptionType.TOTAL,
        entity_registry_enabled_default=False,
    ),
]


def _is_heating_water(device: aioaquarea.Device) -> bool:
    direction = getattr(device, "current_direction", None)
    if direction is None:
        return False
    name = getattr(direction, "name", None)
    return name == "WATER" if name is not None else str(direction) == "WATER"


def _is_zone_active(device: aioaquarea.Device) -> bool:
    # DeviceDirection.PUMP is reported not only for space heating/cooling but
    # also transiently while the tank is being heated, so a PUMP reading on its
    # own would miscount DHW activity as zone cycles. Only count it when at
    # least one zone is actually switched on.
    direction = getattr(device, "current_direction", None)
    if direction is None:
        return False
    name = getattr(direction, "name", None)
    is_pump = name == "PUMP" if name is not None else str(direction) == "PUMP"
    if not is_pump:
        return False
    zones = getattr(device, "zones", None) or {}
    return any(
        zone.operation_status == aioaquarea.OperationStatus.ON
        for zone in zones.values()
    )


def _is_defrosting(device: aioaquarea.Device) -> bool:
    return device.device_mode_status is aioaquarea.DeviceModeStatus.DEFROST


def _current_error_info(device: aioaquarea.Device) -> tuple[str | None, str | None]:
    """Returns (error_code, error_message) for the device's current fault, or (None, None)."""
    current_error = device.current_error
    if current_error is None:
        return None, None
    return current_error.error_code, current_error.error_message


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AquareaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aquarea sensors from a config entry."""
    data: dict[str, AquareaDataUpdateCoordinator] = config_entry.runtime_data
    entities: list[SensorEntity] = []
    for coordinator in data.values():
        entities.append(OutdoorTemperatureSensor(coordinator))
        entities.append(PumpDirectionSensor(coordinator))
        entities.append(PumpStatusSensor(coordinator))
        entities.append(ErrorCodeSensor(coordinator))
        if coordinator.device.has_tank:
            entities.append(TankTemperatureSensor(coordinator))
        entities.extend(
            ZoneTemperatureSensor(coordinator, zone_id)
            for zone_id in coordinator.device.zones
        )
        # Not every unit reports a water pressure; skip the sensor then.
        if coordinator.device.water_pressure is not None:
            entities.append(WaterPressureSensor(coordinator))
        entities.append(
            DailyEdgeCounterSensor(
                coordinator,
                unique_suffix="dhw_cycles_today",
                translation_key="dhw_cycles_today",
                icon="mdi:water-boiler",
                detector=_is_heating_water,
            )
        )
        entities.append(
            DailyEdgeCounterSensor(
                coordinator,
                unique_suffix="zone_cycles_today",
                translation_key="zone_cycles_today",
                icon="mdi:radiator",
                detector=_is_zone_active,
            )
        )
        entities.append(
            DailyEdgeCounterSensor(
                coordinator,
                unique_suffix="defrost_cycles_today",
                translation_key="defrost_cycles_today",
                icon="mdi:snowflake-melt",
                detector=_is_defrosting,
            )
        )
        entities.extend(
            [
                EnergyAccumulatedConsumptionSensor(description, coordinator)
                for description in ACCUMULATED_ENERGY_SENSORS
                if description.exists_fn(coordinator)
            ]
        )
        entities.extend(
            [
                EnergyConsumptionSensor(description, coordinator)
                for description in ENERGY_SENSORS
                if description.exists_fn(coordinator)
            ]
        )
        entities.extend(
            [
                EnergyLifetimeSensor(description, coordinator)
                for description in LIFETIME_ENERGY_SENSORS
                if description.exists_fn(coordinator)
            ]
        )
        entities.extend(
            [
                EnergyTodaySensor(description, coordinator)
                for description in TODAY_ENERGY_SENSORS
                if description.exists_fn(coordinator)
            ]
        )
    async_add_entities(entities)


@dataclass
class AquareaSensorExtraStoredData(SensorExtraStoredData):
    """Restorable sensor data plus the period the value belongs to."""

    period_being_processed: datetime | None = None

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Rebuild the stored data from a restored dict."""
        sensor_data = super().from_dict(restored)
        if sensor_data is None:
            return None
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            period_being_processed=dt_util.parse_datetime(
                restored["period_being_processed"]
            )
            if "period_being_processed" in restored
            else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the stored data as a dict."""
        data = super().as_dict()
        if self.period_being_processed is not None:
            data["period_being_processed"] = dt_util.as_local(
                self.period_being_processed
            ).isoformat()
        return data


@dataclass
class AquareaAccumulatedSensorExtraStoredData(AquareaSensorExtraStoredData):
    """Restorable data of an accumulated sensor, with the running sum of its period."""

    accumulated_period_being_processed: float | None = None

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Rebuild the stored data from a restored dict."""
        sensor_data = super().from_dict(restored)
        if sensor_data is None:
            return None
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            period_being_processed=sensor_data.period_being_processed,
            accumulated_period_being_processed=restored.get(
                "accumulated_period_being_processed"
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the stored data as a dict."""
        data = super().as_dict()
        data["accumulated_period_being_processed"] = (
            self.accumulated_period_being_processed
        )
        return data


class AquareaStateSensor(AquareaBaseEntity, SensorEntity):
    """A sensor whose value is read straight from the current device data."""

    async def async_added_to_hass(self) -> None:
        """Fill in the value from the data the coordinator already has.

        CoordinatorEntity only subscribes to future updates, so without this
        the sensor would be unknown until the next poll after every restart
        or reload.
        """
        await super().async_added_to_hass()
        self._handle_coordinator_update()


class OutdoorTemperatureSensor(AquareaStateSensor):
    """Outdoor temperature reported by the heat pump."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "outdoor_temperature"
        self._attr_unique_id = f"{super().unique_id}_outdoor_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.device is None:
            return
        self._attr_native_value = self.coordinator.device.temperature_outdoor
        super()._handle_coordinator_update()


class TankTemperatureSensor(AquareaStateSensor):
    """Temperature of the hot water tank."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "tank_temperature"
        self._attr_unique_id = f"{super().unique_id}_tank_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.device is None:
            return
        self._attr_native_value = self.coordinator.device.tank.temperature
        super()._handle_coordinator_update()


class ZoneTemperatureSensor(AquareaStateSensor):
    """Temperature reported for one zone of the heat pump."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator, zone_id: int) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        zone = coordinator.device.zones[zone_id]
        self._attr_translation_key = "zone_temperature"
        self._attr_translation_placeholders = {"zone_name": zone.name}
        self._attr_unique_id = f"{super().unique_id}_zone_{zone_id}_temperature"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @callback
    def _handle_coordinator_update(self) -> None:
        # Keep the last value if the zone is gone from the data.
        zone = self.coordinator.device.zones.get(self._zone_id)
        if zone is None:
            return
        self._attr_native_value = zone.temperature
        super()._handle_coordinator_update()


class WaterPressureSensor(AquareaStateSensor):
    """Water circuit pressure reported by the heat pump, in bar."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "water_pressure"
        self._attr_unique_id = f"{super().unique_id}_water_pressure"
        self._attr_device_class = SensorDeviceClass.PRESSURE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfPressure.BAR
        self._attr_suggested_display_precision = 2

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.device is None:
            return
        self._attr_native_value = self.coordinator.device.water_pressure
        super()._handle_coordinator_update()


class PumpDirectionSensor(AquareaStateSensor):
    """Direction the pump is currently directed at, e.g. space or water."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "direction"
        self._attr_unique_id = f"{super().unique_id}_direction"
        self._attr_icon = "mdi:compass"

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.device is None:
            return
        self._attr_native_value = self.coordinator.device.current_direction.name
        super()._handle_coordinator_update()


class PumpStatusSensor(AquareaStateSensor):
    """Whether the pump is running: On when its duty is 1, otherwise Off."""

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "pump_status"
        self._attr_unique_id = f"{super().unique_id}_pump_status"
        self._attr_icon = "mdi:pump"

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.device is None:
            return
        self._attr_native_value = (
            "On" if self.coordinator.device.pump_duty == 1 else "Off"
        )
        super()._handle_coordinator_update()


class ErrorCodeSensor(AquareaStateSensor):
    """Exposes the device's current fault code (e.g. H62), if any.

    `binary_sensor.AquareaStatusBinarySensor` already reports *whether* the
    device is in an error state; this sensor reports *which* error (issue #50
    asked for the code, not just a boolean problem flag).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: AquareaDataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "error_code"
        self._attr_unique_id = f"{super().unique_id}_error_code"

    @callback
    def _handle_coordinator_update(self) -> None:
        device = self.coordinator.device
        if device is None:
            super()._handle_coordinator_update()
            return
        error_code, error_message = _current_error_info(device)
        self._attr_native_value = error_code
        self._attr_extra_state_attributes = (
            {"error_message": error_message} if error_message is not None else {}
        )
        super()._handle_coordinator_update()


class AquareaEnergySensor(AquareaBaseEntity):
    """An energy sensor whose hourly statistics follow the cloud's hour labels.

    The sensor's value moves when a consumption fetch first sees an hour's
    energy, after that hour has closed, so the recorder files it one or two
    hours late. Each time the coordinator fetches the hourly consumption,
    the sensor's past hourly statistics are moved onto the hours the cloud
    labels the energy with (see statistics.py). The statistic id stays the
    same, so the Energy dashboard needs no change.
    """

    entity_description: AquareaEnergyConsumptionSensorDescription
    _hourly_consumption_fetched_at: datetime | None = None
    _period_being_processed: datetime | None = None

    def _ahead_of_the_cloud(self, period_first_day: date) -> bool:
        """Return whether the value belongs to a period after the one shown now.

        Earlier versions moved to the next month sooner: up to 1.5.3 at the
        local midnight (east of UTC, before the cloud's), 1.5.4 to 1.8.0 at
        the cloud's midnight, before the coordinator's consumption_month
        moves on. Restored in between, the value already belongs to the next
        month (typically 0 after its reset); showing the current one again
        would count energy the recorder has metered already, so the value is
        kept until the coordinator catches up.

        The periods are compared by their first day: the local date of the
        restored period's start against the month shown. As instants they
        can't be compared, since a local midnight west of UTC comes after the
        cloud's. A start at the cloud's midnight on the 1st (1.5.4 to 1.8.0)
        has the 1st as its local date, or west of UTC the previous month's
        last day: either way later than the first day of the month before.
        A period stored by this version starts at the cloud's midnight of a
        month already shown, so this only holds a value restored from those
        versions; it can go once upgrades from them no longer need care.
        """
        previous = self._period_being_processed
        return previous is not None and dt_util.as_local(previous).date() > (
            period_first_day
        )

    async def async_added_to_hass(self) -> None:
        """Follow the hourly consumption fetches, and use the one already made."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_hourly_consumption)
        )
        self._handle_hourly_consumption()

    @callback
    def _handle_hourly_consumption(self) -> None:
        """Correct the hourly statistics once per hourly consumption fetch."""
        fetched_at = self.coordinator.hourly_consumption_fetched_at
        if fetched_at is None or fetched_at == self._hourly_consumption_fetched_at:
            return
        self._hourly_consumption_fetched_at = fetched_at
        first_day = self.coordinator.hourly_consumption_first_day
        if first_day is None:
            return
        consumption_type = self.entity_description.consumption_type
        hourly = hourly_consumption(
            self.coordinator.hourly_consumption, consumption_type
        )
        # The first cached day is context only: what was recorded before it
        # is not known, so its own hours are not rewritten. Today is always
        # rewritten, even with no earlier day cached (yesterday's fetch failed):
        # energy recorded early today for yesterday's hours then stays put.
        write_from = datetime.combine(
            min(
                first_day + timedelta(days=1),
                cloud_date(dt_util.utcnow()),
            ),
            time(),
            CLOUD_TIME_ZONE,
        )
        self.coordinator.entry.async_create_background_task(
            self.hass,
            async_redistribute_hourly_statistics(
                self.hass,
                self.entity_id,
                hourly,
                write_from,
                max_match_lag(self.coordinator.consumption_interval),
            ),
            name=f"aquarea hourly statistics {self.entity_id}",
        )


class EnergyAccumulatedConsumptionSensor(
    AquareaEnergySensor, SensorEntity, RestoreEntity
):
    """Energy consumed so far in the cloud's (UTC) month, restored across restarts."""

    entity_description: AquareaEnergyConsumptionSensorDescription

    def __init__(
        self,
        description: AquareaEnergyConsumptionSensorDescription,
        coordinator: AquareaDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor from its description."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{description.key}"
        self._accumulated_period_being_processed: float | None = None
        self.entity_description = description

    async def async_added_to_hass(self) -> None:
        """Restore the last value and period before the first update."""
        sensor_data = await self.async_get_last_sensor_data()
        if sensor_data is not None:
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
        """Return the data to restore after a restart."""
        return AquareaAccumulatedSensorExtraStoredData(
            self.native_value,
            self.native_unit_of_measurement,
            self.period_being_processed,
            self._accumulated_period_being_processed,
        )

    async def async_get_last_sensor_data(
        self,
    ) -> AquareaAccumulatedSensorExtraStoredData | None:
        """Return the restored sensor data, or None if there is none."""
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return AquareaAccumulatedSensorExtraStoredData.from_dict(
            restored_last_extra_data.as_dict()
        )

    @property
    def period_being_processed(self) -> datetime | None:
        """Return the month the current value belongs to."""
        return self._period_being_processed

    @callback
    def _handle_coordinator_update(self) -> None:
        # The cloud month the coordinator fetched: it moves on a few hours
        # after the cloud's midnight, once the old month's last hours are in.
        # Within it, every day up to the cloud's current one counts.
        month = self.coordinator.consumption_month
        if month is not None and self._ahead_of_the_cloud(month):
            super()._handle_coordinator_update()
            return
        today = cloud_date(dt_util.utcnow())
        month_consumption = self.coordinator.month_consumption
        if month is None or not month_consumption:
            self._attr_native_value = None
        else:
            month_heat = month_cool = month_tank = month_total = 0.0
            for c in month_consumption:
                try:
                    dt_str = c.data_time
                    if not dt_str:
                        continue
                    item_date = None
                    for fmt in ("%Y%m%d", "%Y-%m-%d"):
                        try:
                            item_date = datetime.strptime(dt_str, fmt).date()
                            break
                        except ValueError:
                            continue

                    if item_date is None:
                        _LOGGER.warning(
                            "Unexpected date format for month consumption: %s", dt_str
                        )
                        continue

                    if item_date <= today:
                        month_heat += float(c.heat_consumption or 0.0)
                        month_cool += float(c.cool_consumption or 0.0)
                        month_tank += float(c.tank_consumption or 0.0)
                        try:
                            month_total += float(c.total_consumption or 0.0)
                        except ValueError, TypeError:
                            month_total += float(
                                (c.heat_consumption or 0.0)
                                + (c.cool_consumption or 0.0)
                                + (c.tank_consumption or 0.0)
                            )
                except ValueError, TypeError:
                    _LOGGER.exception(
                        "Failed to parse month consumption item date: %s",
                        getattr(c, "data_time", None),
                    )

            ctype = self.entity_description.consumption_type
            reported_val = None
            if ctype == aioaquarea.ConsumptionType.HEAT:
                reported_val = month_heat
            elif ctype == aioaquarea.ConsumptionType.COOL:
                reported_val = month_cool
            elif ctype == aioaquarea.ConsumptionType.WATER_TANK:
                reported_val = month_tank
            elif ctype == aioaquarea.ConsumptionType.TOTAL:
                reported_val = month_total
            if reported_val is not None:
                self._period_being_processed = datetime.combine(
                    month, time(), CLOUD_TIME_ZONE
                )
                self._attr_native_value = reported_val
        super()._handle_coordinator_update()


class EnergyConsumptionSensor(AquareaBaseEntity, SensorEntity):
    """Energy the cloud published since the previous hourly consumption fetch.

    The value is the rise of the cached hourly records' total between two
    fetches, kept until the next fetch and 0 when nothing new came. It is a
    momentary value, not a meter: it has no restored state and no long-term
    statistics correction. Use the accumulated sensors for the Energy
    dashboard.
    """

    entity_description: AquareaEnergyConsumptionSensorDescription
    _seen_fetch: datetime | None = None
    _seen_total: float | None = None

    def __init__(
        self,
        description: AquareaEnergyConsumptionSensorDescription,
        coordinator: AquareaDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor from its description."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{description.key}"
        self.entity_description = description
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        """Take the fetch already made as the baseline."""
        await super().async_added_to_hass()
        self._observe_fetch()

    def _observe_fetch(self) -> None:
        """Update the value once per hourly consumption fetch."""
        fetched_at = self.coordinator.hourly_consumption_fetched_at
        if fetched_at is None or fetched_at == self._seen_fetch:
            return
        self._seen_fetch = fetched_at
        total = sum(
            hourly_consumption(
                self.coordinator.hourly_consumption or [],
                self.entity_description.consumption_type,
            ).values()
        )
        # The first fetch only sets the baseline. The total shrinks when a day
        # leaves the cache: that is not consumption, so it reads 0 as well.
        previous = self._seen_total
        self._seen_total = total
        delta = 0.0 if previous is None else total - previous
        self._attr_native_value = delta if delta > 1e-6 else 0.0

    @callback
    def _handle_coordinator_update(self) -> None:
        self._observe_fetch()
        super()._handle_coordinator_update()


class EnergyLifetimeSensor(AquareaEnergySensor, SensorEntity, RestoreEntity):
    """All the energy consumed since the cloud's first record.

    The sum of the finished months, kept in storage, and the month being
    shown, as of the latest fetch; unknown until the history before the
    integration was installed has been read once (see lifetime.py). Like the
    accumulated sensors, its hourly statistics follow the cloud's hour labels.
    The last state Home Assistant restored is a floor, in case the stored
    months were lost.
    """

    entity_description: AquareaEnergyConsumptionSensorDescription
    _unrecorded_attributes = frozenset({"history_complete"})

    def __init__(
        self,
        description: AquareaEnergyConsumptionSensorDescription,
        coordinator: AquareaDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor from its description."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{description.key}"
        self.entity_description = description
        self._restored_floor: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last value, which the value never goes below."""
        if (last := await self.async_get_last_state()) is not None:
            try:
                value = float(last.state)
            except ValueError:
                value = None
            if value is not None and math.isfinite(value) and value >= 0:
                self._restored_floor = value
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_lifetime_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float | None:
        """Return the lifetime energy in kWh, or None until it is known."""
        value = self.coordinator.lifetime.lifetime_value(
            self.entity_description.consumption_type
        )
        if value is None or self._restored_floor is None:
            return value
        return max(value, self._restored_floor)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether the history has been read (the rest is in diagnostics)."""
        return {"history_complete": self.coordinator.lifetime.complete}


def today_settle(consumption_interval: int) -> timedelta:
    """Return how long after local midnight the today sensors keep the old day.

    The last hour of a day is published about an hour after it ends and read
    at the next consumption refresh, so the settle time is the interval plus a
    margin of two hours, but never below LIFETIME_TODAY_SETTLE.
    """
    return max(LIFETIME_TODAY_SETTLE, timedelta(minutes=consumption_interval + 120))


class EnergyTodaySensor(AquareaBaseEntity, SensorEntity):
    """The energy consumed in the local calendar day, up to the last cloud hour.

    The day changes some hours after local midnight (see ``today_settle``).
    Built from the cloud's hourly records (the same fetches as the hourly
    statistics, no extra requests) that fall between the local midnights.
    An hour the cloud revises replaces its old value, and an hour published
    after midnight for the day before is counted in that day, not in this one.
    """

    entity_description: AquareaEnergyConsumptionSensorDescription

    def __init__(
        self,
        description: AquareaEnergyConsumptionSensorDescription,
        coordinator: AquareaDataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor from its description."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{description.key}"
        self.entity_description = description
        self._now = self._presented_time()

    def _presented_time(self) -> datetime:
        """Return a moment inside the day to present.

        The cloud publishes the last hour of a day about an hour after it
        ends, and the integration reads it at the next consumption refresh, so
        the day only changes after the settle time (at least
        LIFETIME_TODAY_SETTLE, longer for a long consumption interval) past
        local midnight; until then the finished day keeps being shown,
        complete, with its own last_reset.
        """
        return dt_util.now() - today_settle(self.coordinator.consumption_interval)

    async def async_added_to_hass(self) -> None:
        """Take the time of the first state write."""
        self._now = self._presented_time()
        await super().async_added_to_hass()

    @property
    def native_value(self) -> float | None:
        """Return today's energy in kWh, or None before any hour is known."""
        return self.coordinator.lifetime.today_value(
            self.entity_description.consumption_type, self._now
        )

    @property
    def last_reset(self) -> datetime:
        """Return the start of the local day the value belongs to."""
        return local_day_bounds(self._now)[0]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._now = self._presented_time()
        super()._handle_coordinator_update()


@dataclass
class AquareaEdgeCounterExtraStoredData(SensorExtraStoredData):
    """Restorable data of a daily edge counter: value, last reset and last detector state."""

    last_reset: datetime | None = None
    last_state: bool = False

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Rebuild the stored data from a restored dict."""
        sensor_data = super().from_dict(restored)
        if sensor_data is None:
            return None
        return cls(
            native_value=sensor_data.native_value,
            native_unit_of_measurement=sensor_data.native_unit_of_measurement,
            last_reset=dt_util.parse_datetime(restored["last_reset"])
            if restored.get("last_reset")
            else None,
            last_state=bool(restored.get("last_state", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the stored data as a dict."""
        data = super().as_dict()
        if self.last_reset is not None:
            data["last_reset"] = dt_util.as_local(self.last_reset).isoformat()
        data["last_state"] = self.last_state
        return data


class DailyEdgeCounterSensor(AquareaBaseEntity, SensorEntity, RestoreEntity):
    """Counts low-to-high transitions of a boolean detector, reset at local midnight."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: AquareaDataUpdateCoordinator,
        *,
        unique_suffix: str,
        translation_key: str,
        icon: str,
        detector: Callable[[aioaquarea.Device], bool],
    ) -> None:
        """Initialize the counter with the detector whose transitions it counts."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{super().unique_id}_{unique_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._detector = detector
        self._last_state: bool = False
        self._attr_last_reset: datetime | None = None
        self._attr_native_value: int = 0

    async def async_added_to_hass(self) -> None:
        """Restore the counter, its last reset and the last detector state."""
        restored = await self.async_get_last_sensor_data()
        if restored is not None:
            try:
                native = restored.native_value
                if native is None or isinstance(native, (date, datetime)):
                    # int() doesn't accept a date/datetime; treat as unusable,
                    # same outcome as the TypeError below used to produce.
                    self._attr_native_value = 0
                else:
                    self._attr_native_value = int(native)
            except TypeError, ValueError:
                self._attr_native_value = 0
            self._attr_last_reset = restored.last_reset
            self._last_state = restored.last_state
        if self._attr_last_reset is None:
            self._attr_last_reset = dt_util.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        await super().async_added_to_hass()

    @property
    def extra_restore_state_data(self) -> AquareaEdgeCounterExtraStoredData:
        """Return the data to restore after a restart."""
        return AquareaEdgeCounterExtraStoredData(
            native_value=self.native_value,
            native_unit_of_measurement=self.native_unit_of_measurement,
            last_reset=self._attr_last_reset,
            last_state=self._last_state,
        )

    async def async_get_last_sensor_data(
        self,
    ) -> AquareaEdgeCounterExtraStoredData | None:
        """Return the restored counter data, or None if there is none."""
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return AquareaEdgeCounterExtraStoredData.from_dict(
            restored_last_extra_data.as_dict()
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        device = self.coordinator.device
        if device is None:
            super()._handle_coordinator_update()
            return

        now = dt_util.now()
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if (
            self._attr_last_reset is None
            or dt_util.as_local(self._attr_last_reset).date() != now.date()
        ):
            self._attr_last_reset = today_midnight
            self._attr_native_value = 0

        try:
            current = bool(self._detector(device))
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Edge detector failed for %s: %s", self._attr_unique_id, e)
            super()._handle_coordinator_update()
            return

        if current and not self._last_state:
            self._attr_native_value = int(self._attr_native_value or 0) + 1
        self._last_state = current

        super()._handle_coordinator_update()
