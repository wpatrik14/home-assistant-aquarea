"""Statistics models for Aquarea"""
from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:
    from strenum import StrEnum


class DateType(StrEnum):
    """Date types"""

    DAY = "date"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class AggregationType(StrEnum):
    """Aggregation type"""

    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"


class ConsumptionType(StrEnum):
    """Data type"""

    HEAT = "Heat"
    COOL = "AC"
    WATER_TANK = "HW"
    TOTAL = "Consume"


class GenerationDataType(StrEnum):
    """Generation data type"""

    GHT = "GHT"
    GHR = "GHR"
    GHW = "GHW"


class DataSetName(StrEnum):
    """Data set name"""

    ENERGY = "energyShowing"
    GENERATED = "generateEnergyShowing"
    COST = "costShowing"
    TEMPERATURE = "temperatureShowing"


class Consumption:
    """Consumption"""

    def __init__(self, date_data: dict[str, object]):
        self._data = dict(
            [
                (
                    dataset.get("name"),
                    dict(
                        [
                            (data.get("name"), data.get("values"))
                            for data in dataset.get("data", [])
                        ]
                    ),
                )
                for dataset in date_data.get("dataSets", [])
            ]
        )
        self._start_date = date_data.get("startDate")
        self._aggregation = AggregationType(date_data.get("timeline", {}).get("type"))

    @property
    def energy(self) -> dict[ConsumptionType, list[float | None]]:
        """Energy consumption in kWh"""
        return self._data.get(DataSetName.ENERGY)

    @property
    def generation(self) -> dict[GenerationDataType, list[float]]:
        """Energy generation in kWh"""
        return self._data.get(DataSetName.GENERATED)

    @property
    def cost(self) -> dict[ConsumptionType, list[float]]:
        """Energy cost in configured currency"""
        return self._data.get(DataSetName.COST)

    @property
    def start_date(self) -> str:
        """Start date of the period"""
        return self._start_date

    @property
    def aggregation(self) -> AggregationType:
        """Aggregation type"""
        return self._aggregation
