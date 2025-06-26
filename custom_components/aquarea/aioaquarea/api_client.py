import asyncio
import datetime as dt
import logging
from typing import Optional
import urllib.parse

import aiohttp

from .const import (
    AQUAREA_SERVICE_BASE,
    AQUAREA_SERVICE_DEMO_BASE,
    AquareaEnvironment,
)
from .errors import (
    ApiError,
    AuthenticationError,
    AuthenticationErrorCodes,
)
from .auth import PanasonicSettings, CCAppVersion, PanasonicRequestHeader

_LOGGER = logging.getLogger(__name__)

class AquareaAPIClient:
    """Base API client for Aquarea."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: PanasonicSettings,
        app_version: CCAppVersion,
        environment: AquareaEnvironment,
        logger: Optional[logging.Logger] = None,
    ):
        self._sess = session
        self._settings = settings
        self._app_version = app_version
        self._environment = environment
        self._logger = logger or logging.getLogger("aioaquarea")
        self._base_url = (
            AQUAREA_SERVICE_BASE
            if environment == AquareaEnvironment.PRODUCTION
            else AQUAREA_SERVICE_DEMO_BASE
        )
        self._access_token: Optional[str] = None
        self._token_expiration: Optional[dt.datetime] = None

    async def request(
        self,
        method: str,
        url: str = None,
        external_url: str = None,
        referer: str = AQUAREA_SERVICE_BASE,
        throw_on_error=True,
        content_type: str = "application/json",
        headers: Optional[dict] = None,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """Make a request to Aquarea and return the response."""

        # Get base headers including authentication
        base_headers = await PanasonicRequestHeader.get(self._settings, self._app_version)

        # If headers are explicitly provided by the caller, use them as a base
        # Otherwise, use the base_headers as the starting point
        if headers is None:
            headers = base_headers
        else:
            # Merge base_headers into the provided headers, allowing provided headers to override
            base_headers.update(headers)
            headers = base_headers
        
        # Merge any headers from kwargs (e.g., from aiohttp's json=data)
        request_headers = kwargs.get("headers", {})
        headers.update(request_headers)
        kwargs["headers"] = headers

        if external_url is not None:
            # If external_url is provided, use it directly or join with base_url if relative
            parsed_external_url = urllib.parse.urlparse(external_url)
            if not parsed_external_url.scheme or not parsed_external_url.netloc:
                url = urllib.parse.urljoin(self._base_url, external_url)
            else:
                url = external_url
        else:
            # If external_url is not provided, join the base_url with the given url
            url = urllib.parse.urljoin(self._base_url, url)
        resp = await self._sess.request(method, url, **kwargs)

        if resp.content_type == "application/json":
            data = await resp.json()

            # let's check for access token and expiration time
            if self._access_token and self.__contains_valid_token(data):
                self._access_token = data["accessToken"]["token"]
                self._token_expiration = dt.datetime.strptime(
                    data["accessToken"]["expires"], "%Y-%m-%dT%H:%M:%S%z"
                )

            # Aquarea returns a 200 even if the request failed, we need to check the message property to see if it's an error
            # Some errors just require to login again, so we raise a AuthenticationError in those known cases
            if throw_on_error:
                errors = await self.look_for_errors(data)
                # If we have errors, let's look for authentication errors
                for error in errors:
                    if error.error_code in list(AuthenticationErrorCodes):
                        raise AuthenticationError(error.error_code, error.error_message)

                    raise ApiError(error.error_code, error.error_message)

        return resp

    def __contains_valid_token(self, data: dict) -> bool:
        """Check if the data contains a valid token."""
        return (
            "accessToken" in data
            and "token" in data["accessToken"]
            and "expires" in data["accessToken"]
        )

    async def look_for_errors(
        self, data: dict
    ) -> list[ApiError]: # Changed return type to ApiError
        """Look for errors in the response and return them as a list of ApiError objects."""
        if not isinstance(data, dict):
            return []

        messages = data.get("message", [])
        if not isinstance(messages, list):
            messages = [messages] # Wrap single string message in a list

        api_errors = []
        for error_item in messages:
            if isinstance(error_item, dict) and "errorMessage" in error_item and "errorCode" in error_item:
                # Check for token expiration message
                if "Token expires" in error_item["errorMessage"]:
                    api_errors.append(AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED, error_item["errorMessage"]))
                else:
                    api_errors.append(ApiError(error_item["errorCode"], error_item["errorMessage"]))
            elif isinstance(error_item, str):
                # Check for token expiration message in string errors
                if "Token expires" in error_item:
                    api_errors.append(AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED, error_item))
                else:
                    api_errors.append(ApiError("unknown_error_code", error_item))
        return api_errors

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    @access_token.setter
    def access_token(self, value: Optional[str]):
        self._access_token = value

    @property
    def token_expiration(self) -> Optional[dt.datetime]:
        return self._token_expiration

    @token_expiration.setter
    def token_expiration(self, value: Optional[dt.datetime]):
        self._token_expiration = value
