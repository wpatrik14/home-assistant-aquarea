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

# Only what is needed to connect. The consumption interval is an option
# (options flow below); entries created before it moved there still carry it
# in their data, and the coordinator and the options form fall back to it.
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AquareaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aquarea Smart Cloud."""

    VERSION = 1

    _username: str | None = None
    _session: aiohttp.ClientSession | None = None
    _api_error_msg: str | None = None

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

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Perform reauth upon an API authentication error."""
        username = self._try_get_username(entry_data)
        if username is None:
            # No username in the entry data, the flow context or the unique ID -
            # nothing to reauthenticate against. Abort with a clear message
            # instead of letting None reach the API client as invalid_auth.
            return self.async_abort(reason="reauth_no_username")
        self._username = username
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Ask for the new password and validate it.

        The form is submitted back to this step (Home Assistant dispatches a
        submission to `async_step_<step_id>`), not to `async_step_reauth`.
        """
        username = self._username
        if username is None:
            return self.async_abort(reason="reauth_no_username")
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
        changed = self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            },
        )
        # A loaded entry whose data changed is reloaded by its update listener
        # (`_async_update_listener` in __init__.py). Otherwise nothing would
        # reload it: an entry whose setup failed on the old password has no
        # listener, and Home Assistant does not reload entries when a reauth
        # flow ends, so it would stay in setup_error until a restart.
        if not changed or entry.state is not config_entries.ConfigEntryState.LOADED:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def async_show_reauth_form(
        self, username: str, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the reauth form."""
        return self.async_show_form(
            step_id="reauth_confirm",
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
        except aioaquarea.AuthenticationError as err:
            # SESSION_CLOSED and TOKEN_EXPIRED are transient; telling the user
            # their (correct) password is wrong would send them the wrong way.
            if err.error_code in (
                aioaquarea.AuthenticationErrorCodes.SESSION_CLOSED,
                aioaquarea.AuthenticationErrorCodes.TOKEN_EXPIRED,
            ):
                errors["base"] = "cannot_connect"
            else:
                errors["base"] = "invalid_auth"
        except aioaquarea.errors.ApiError as err:
            _LOGGER.error("API error during setup: %s", err)
            errors["base"] = "api_error"
            self._api_error_msg = str(err)
        except (aioaquarea.errors.RequestFailedError, aiohttp.ClientError, TimeoutError):
            # aioaquarea does not wrap network failures (DNS, connection
            # resets, timeouts); without this they would surface as "unknown".
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected error during setup")
            errors["base"] = "unknown"

        return errors

    def async_show_form(
        self,
        *,
        step_id: str | None = None,
        data_schema: vol.Schema | None = None,
        errors: dict[str, str] | None = None,
        description_placeholders: Mapping[str, str] | None = None,
        last_step: bool | None = None,
        preview: str | None = None,
    ) -> ConfigFlowResult:
        """Show the form with dynamic error message if needed."""
        if errors and errors.get("base") == "api_error":
            description_placeholders = {
                **(description_placeholders or {}),
                "api_error_msg": self._api_error_msg or "Unknown API error",
            }

        return super().async_show_form(
            step_id=step_id,
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=last_step,
            preview=preview,
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
