"""Config flow for Aquarea Smart Cloud integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aioaquarea
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_SCAN_INTERVAL,
    CONF_CONSUMPTION_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_CONSUMPTION_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=10)
        ),
        vol.Required(
            CONF_CONSUMPTION_INTERVAL, default=DEFAULT_CONSUMPTION_INTERVAL
        ): vol.All(vol.Coerce(int), vol.Range(min=10)),
    }
)


class AquareaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aquarea Smart Cloud."""

    VERSION = 1

    _username: str | None = None
    _session: aiohttp.ClientSession | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.info = {}
        self._api: aioaquarea.Client = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AquareaOptionsFlowHandler:
        """Get the options flow for this handler."""
        return AquareaOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(str.lower(user_input[CONF_USERNAME]))
            self._abort_if_unique_id_configured()

            errors = await self._validate_input(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )

            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any], user_input=None):
        """Perform reauth upon an API authentication error."""
        self._username = self._try_get_username(entry_data)
        errors = {}

        if user_input is not None:
            errors = await self._validate_input(
                self._username, user_input[CONF_PASSWORD]
            )

            if not errors:
                # If we get here, we have a valid login
                return await self.async_complete_reauth(
                    self._username, user_input[CONF_PASSWORD]
                )

        return await self.async_show_reauth_form(self._username, errors)

    async def async_complete_reauth(self, username: str, password: str) -> FlowResult:
        """Complete reauth."""
        entry = await self.async_set_unique_id(self.unique_id)
        assert entry
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            },
        )
        return self.async_abort(reason="reauth_successful")

    async def async_show_reauth_form(
        self, username: str, errors: dict[str, str] | None = None
    ) -> FlowResult:
        """Show the reauth form."""
        return self.async_show_form(
            step_id="reauth",
            description_placeholders={"username": username},
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    def _try_get_username(self, entry_data: Mapping[str, Any]) -> str:
        """Try to get username from entry data or context"""
        if self._username is not None:
            return self._username

        if entry_data and entry_data.get(CONF_USERNAME):
            self._username = entry_data[CONF_USERNAME]
            return self._username

        if self.context.init_data and self.context.init_data.get(CONF_USERNAME):
            self._username = self.context.init_data[CONF_USERNAME]
            return self._username

        if self.unique_id:
            self._username = self.unique_id
            return self._username

        return None

    async def _validate_input(self, username, password) -> dict[str, str]:
        """Validate the user input allows us to connect."""
        errors = {}
        if self._session is None:
            self._session = async_create_clientsession(self.hass)

        self._api = aioaquarea.Client(self._session, username, password)
        try:
            await self._api.login()
            # Also try to get devices to catch ApiErrors like "Terms updated"
            await self._api.get_devices()
        except aioaquarea.AuthenticationError:
            errors["base"] = "invalid_auth"
        except aioaquarea.errors.ApiError as err:
            _LOGGER.error("API error during setup: %s", err)
            errors["base"] = "api_error"
            self.context["api_error_msg"] = str(err)
        except aioaquarea.errors.RequestFailedError:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected error during setup")
            errors["base"] = "unknown"

        return errors

    async def async_show_form(
        self,
        *,
        step_id: str,
        data_schema: vol.Schema | None = None,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
        last_step: bool | None = None,
    ) -> FlowResult:
        """Show the form with dynamic error message if needed."""
        if errors and errors.get("base") == "api_error":
            if description_placeholders is None:
                description_placeholders = {}
            description_placeholders["api_error_msg"] = self.context.get(
                "api_error_msg", "Unknown API error"
            )

        return await super().async_show_form(
            step_id=step_id,
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )


class AquareaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Aquarea options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL,
                            self.config_entry.data.get(
                                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                            ),
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                    vol.Required(
                        CONF_CONSUMPTION_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_CONSUMPTION_INTERVAL,
                            self.config_entry.data.get(
                                CONF_CONSUMPTION_INTERVAL, DEFAULT_CONSUMPTION_INTERVAL
                            ),
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
