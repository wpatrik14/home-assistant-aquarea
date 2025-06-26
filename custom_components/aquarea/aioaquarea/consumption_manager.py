from __future__ import annotations

from typing import TYPE_CHECKING

from .const import AQUAREA_SERVICE_A2W_STATUS_DISPLAY, AQUAREA_SERVICE_CONSUMPTION
from .statistics import Consumption, DateType # Import from statistics
from .auth import PanasonicRequestHeader

if TYPE_CHECKING:
    from .api_client import AquareaAPIClient


class AquareaConsumptionManager:
    """Handles consumption data retrieval."""

    def __init__(self, api_client: AquareaAPIClient, base_url: str):
        self._api_client = api_client
        self._base_url = base_url

    async def get_device_consumption(
        self, long_id: str, aggregation: DateType, date_input: str
    ) -> Consumption:
        """Get device consumption."""
        response = await self._api_client.request(
            "GET",
            f"{AQUAREA_SERVICE_CONSUMPTION}/{long_id}?{aggregation}={date_input}",
            headers=PanasonicRequestHeader.get_aqua_headers(
                referer=f"{self._base_url}{AQUAREA_SERVICE_A2W_STATUS_DISPLAY}"
            ),
        )

        date_data = await response.json()
        return Consumption(date_data.get("dateData")[0])
