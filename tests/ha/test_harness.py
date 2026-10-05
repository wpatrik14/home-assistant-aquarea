"""Smoke tests for the pytest harness itself, not for the integration."""

from unittest.mock import AsyncMock

import aioaquarea

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.aquarea.const import DOMAIN


async def test_integration_loads_from_repo(hass: HomeAssistant) -> None:
    """Home Assistant finds the integration in this repo's custom_components/."""
    integration = await async_get_integration(hass, DOMAIN)

    assert not integration.is_built_in
    assert integration.config_flow


def test_aioaquarea_client_is_mocked(mock_aquarea_client: AsyncMock) -> None:
    """Creating a client returns the mock, so no test can reach the cloud."""
    assert aioaquarea.Client(None, "user", "password") is mock_aquarea_client
