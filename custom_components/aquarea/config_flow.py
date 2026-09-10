"""Config flow for Aquarea Smart Cloud integration."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aioaquarea
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
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
        return AquareaOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
        username = self._try_get_username(entry_data)
        if username is None:
            # No username in the entry data, the flow context or the unique ID -
            # nothing to reauthenticate against. Abort with a clear message
            # instead of letting None reach the API client as invalid_auth.
            return self.async_abort(reason="reauth_no_username")
        self._username = username
        errors = {}

        if user_input is not None:
            errors = await self._validate_input(username, user_input[CONF_PASSWORD])

            if not errors:
                # If we get here, we have a valid login
                return await self.async_complete_reauth(
                    username, user_input[CONF_PASSWORD]
                )

        return await self.async_show_reauth_form(username, errors)

    async def async_complete_reauth(self, username: str, password: str) -> ConfigFlowResult:
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
    ) -> ConfigFlowResult:
        """Show the reauth form."""
        return self.async_show_form(
            step_id="reauth",
            description_placeholders={"username": username},
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    def _try_get_username(self, entry_data: Mapping[str, Any]) -> str | None:
        """Try to get username from entry data or context, None if unknown."""
        if self._username is not None:
            return self._username

        if entry_data and entry_data.get(CONF_USERNAME):
            self._username = entry_data[CONF_USERNAME]
            return self._username

        init_data = self.init_data
        if init_data and init_data.get(CONF_USERNAME):
            self._username = init_data[CONF_USERNAME]
            return self._username

        if self.unique_id:
            self._username = self.unique_id
            return self._username

        # No username is known; async_step_reauth aborts on this.
        return None

    async def _validate_input(self, username, password) -> dict[str, str]:
        """Validate the user input allows us to connect."""
        errors = {}
        if self._session is None:
            self._session = async_create_clientsession(self.hass)

        self._api = aioaquarea.Client(self._session, username, password)
        try:
            await self._api.login()
        except aioaquarea.AuthenticationError:
            errors["base"] = "invalid_auth"
        except aioaquarea.errors.ApiError as err:
            _LOGGER.error("API error during setup: %s", err)
            errors["base"] = "api_error"
            # api_error_msg is not a ConfigFlowContext key
            self.context["api_error_msg"] = str(err)  # type: ignore[typeddict-unknown-key]
        except aioaquarea.errors.RequestFailedError:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected error during setup")
            errors["base"] = "unknown"

        return errors

    # override is narrower than the base signature
    def async_show_form(  # type: ignore[override]
        self,
        *,
        step_id: str,
        data_schema: vol.Schema | None = None,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
        last_step: bool | None = None,
    ) -> ConfigFlowResult:
        """Show the form with dynamic error message if needed."""
        if errors and errors.get("base") == "api_error":
            if description_placeholders is None:
                description_placeholders = {}
            msg = self.context.get("api_error_msg", "Unknown API error")
            # context.get() returns object
            description_placeholders["api_error_msg"] = msg  # type: ignore[assignment]

        return super().async_show_form(
            step_id=step_id,
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
        )


class AquareaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Aquarea options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
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
