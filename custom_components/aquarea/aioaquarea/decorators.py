import functools
import logging
from typing import TYPE_CHECKING # Re-add TYPE_CHECKING
from .errors import AuthenticationError, AuthenticationErrorCodes

if TYPE_CHECKING: # Re-add TYPE_CHECKING block
    from .core import AquareaClient

_LOGGER = logging.getLogger(__name__)

def auth_required(fn):
    """Decorator to require authentication and to refresh login if it's able to."""

    @functools.wraps(fn)
    async def _wrap(client: "AquareaClient", *args, **kwargs): # Use string literal for type hint
        if client.is_logged is False:
            client.logger.warning(f"{client}: User is not logged or session is too old")
            await client.login()

        try:
            response = await fn(client, *args, **kwargs)
        except AuthenticationError as exception:
            client.logger.warning(
                f"{client}: Auth Error: {exception.error_code} - {exception.error_message}."
            )

            # If the error is invalid credentials, we don't want to retry the request.
            if (
                exception.error_code
                == AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD
                or not client.is_refresh_login_enabled
            ):
                raise

            client.logger.warning(f"{client}: Trying to login again.")
            await client.login()
            response = await fn(client, *args, **kwargs)

        return response

    return _wrap
