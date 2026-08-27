"""Regression tests for error handling in `async_setup_entry`.

Background
----------
`async_setup_entry` originally wrapped its body in a single
`except aioaquarea.AuthenticationError` clause, which produced two distinct
failure modes:

1. **Transient network errors were permanently fatal.** `client.login()` and
   `client.get_devices()` can raise `aiohttp.ClientError`, `TimeoutError`,
   `ApiError` or `RequestFailedError`. None of those are
   `AuthenticationError`, so they propagated raw out of `async_setup_entry`.
   Home Assistant treats an unhandled exception as a *non-retryable* setup
   failure: the entry lands in `setup_error` and is never retried, leaving
   every entity `unavailable` until a manual reload. A DNS blip lasting
   seconds could take the integration down for days.

2. **Some auth failures were silently swallowed.** If an
   `AuthenticationError` carried an `error_code` outside the two-member tuple
   the `if` tested, the exception was discarded and control fell through to
   `return True` — Home Assistant was told setup succeeded while no devices,
   no coordinators and no platforms had been set up. Three of the five
   `AuthenticationErrorCodes` members (`SESSION_CLOSED`, `API_ERROR`,
   `TOKEN_EXPIRED`) hit this gap.

The fix maps transient failures to `ConfigEntryNotReady` (which HA retries
with exponential backoff) and keeps `ConfigEntryAuthFailed` for genuinely
bad credentials, with no path that can reach `return True` after a failure.

These tests load the *actual* `async_setup_entry` out of `__init__.py` (via
AST, so they exercise the shipped code rather than a copy) and drive it with
stubbed Home Assistant / aioaquarea / aiohttp objects.

Intentionally dependency-free (stdlib only) so it runs without Home
Assistant, aioaquarea, aiohttp or pytest installed:

    python3 tests/test_setup_entry_errors.py
"""
import ast
import asyncio
import os
import sys
import types

INIT = os.path.join(
    os.path.dirname(__file__),
    "..", "custom_components", "aquarea", "__init__.py",
)


# --- stub aioaquarea (only what async_setup_entry touches) ------------------
class ClientError(Exception):
    """Base exception for all aioaquarea client errors."""


class RequestFailedError(ClientError):
    """Request to the server failed."""


class ApiError(ClientError):
    def __init__(self, error_code, error_message):
        super().__init__()
        self.error_code = error_code
        self.error_message = error_message

    def __str__(self):
        return f"API error: {self.error_code} - {self.error_message}"


class AuthenticationError(ApiError):
    def __str__(self):
        return f"Authentication error: {self.error_code} - {self.error_message}"


class AuthenticationErrorCodes:
    """Mirrors aioaquarea.errors.AuthenticationErrorCodes."""

    SESSION_CLOSED = "1001-0001"
    INVALID_USERNAME_OR_PASSWORD = "1001-1401"
    INVALID_CREDENTIALS = "1000-1401"
    API_ERROR = "API_ERROR"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"


aioaquarea = types.ModuleType("aioaquarea")
aioaquarea.ClientError = ClientError
aioaquarea.RequestFailedError = RequestFailedError
aioaquarea.ApiError = ApiError
aioaquarea.AuthenticationError = AuthenticationError
aioaquarea.AuthenticationErrorCodes = AuthenticationErrorCodes
aioaquarea.Client = object
sys.modules["aioaquarea"] = aioaquarea


# --- stub aiohttp ------------------------------------------------------------
class AiohttpClientError(Exception):
    """Stands in for aiohttp.ClientError."""


class ClientConnectorDNSError(AiohttpClientError):
    """Stands in for aiohttp.client_exceptions.ClientConnectorDNSError."""


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = AiohttpClientError
aiohttp.ClientConnectorDNSError = ClientConnectorDNSError
sys.modules["aiohttp"] = aiohttp


# --- stub Home Assistant exceptions -----------------------------------------
class HomeAssistantError(Exception):
    pass


class ConfigEntryError(HomeAssistantError):
    pass


class ConfigEntryAuthFailed(ConfigEntryError):
    """Raised when credentials are rejected; triggers the reauth flow."""


class ConfigEntryNotReady(ConfigEntryError):
    """Raised on transient failures; HA retries with backoff."""


# --- pull the real async_setup_entry out of __init__.py ---------------------
def _load_setup_entry(namespace_extra=None):
    with open(INIT, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"
    )
    namespace = {
        "aioaquarea": aioaquarea,
        "aiohttp": aiohttp,
        "ConfigEntryAuthFailed": ConfigEntryAuthFailed,
        "ConfigEntryNotReady": ConfigEntryNotReady,
        "DOMAIN": "aquarea",
        "CLIENT": "client",
        "DEVICES": "devices",
        "PLATFORMS": ["sensor"],
        "_LOGGER": _NullLogger(),
        "AquareaDataUpdateCoordinator": _Coordinator,
    }
    namespace.update(namespace_extra or {})
    exec(compile(ast.Module([node], []), INIT, "exec"), namespace)
    return namespace["async_setup_entry"]


# --- fakes -------------------------------------------------------------------
class _NullLogger:
    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Coordinator:
    """Stands in for AquareaDataUpdateCoordinator."""

    def __init__(self, hass=None, entry=None, client=None, device_info=None):
        self.device_info = device_info

    async def async_config_entry_first_refresh(self):
        return None


class _Device:
    def __init__(self, device_id="dev-1"):
        self.device_id = device_id


class _ConfigEntries:
    def __init__(self):
        self.forwarded = False

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded = True


class _Hass:
    def __init__(self):
        self.data = {}
        self.config_entries = _ConfigEntries()


class _Entry:
    entry_id = "test-entry"
    data = {"username": "u", "password": "p"}


class _Client:
    """Fake aioaquarea client; raises on login/get_devices when configured."""

    def __init__(self, login_exc=None, devices_exc=None, devices=None):
        self._login_exc = login_exc
        self._devices_exc = devices_exc
        self._devices = devices if devices is not None else [_Device()]

    async def login(self):
        if self._login_exc is not None:
            raise self._login_exc

    async def get_devices(self):
        if self._devices_exc is not None:
            raise self._devices_exc
        return self._devices


def _run(client):
    """Invoke the real async_setup_entry with a fake client.

    Returns (outcome, hass) where outcome is either ("returned", value) or
    ("raised", exception_instance).
    """
    hass = _Hass()
    setup_entry = _load_setup_entry(
        {"_create_client": lambda hass, entry: client}
    )
    try:
        result = asyncio.run(setup_entry(hass, _Entry()))
    except BaseException as err:  # noqa: BLE001 - we classify it below
        return ("raised", err), hass
    return ("returned", result), hass


def _auth_error(code):
    return AuthenticationError(code, "boom")


def main():
    C = AuthenticationErrorCodes

    # (name, client, expectation)
    #   expectation is ("raises", ExcType) or ("returns", value)
    cases = [
        # --- Bug 1: transient errors must be retryable, not fatal -----------
        (
            "DNS failure on login -> ConfigEntryNotReady",
            _Client(login_exc=ClientConnectorDNSError(
                "Cannot connect to host accsmart.panasonic.com:443"
            )),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "generic aiohttp error on login -> ConfigEntryNotReady",
            _Client(login_exc=AiohttpClientError("connection reset")),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "TimeoutError on login -> ConfigEntryNotReady",
            _Client(login_exc=TimeoutError("timed out")),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "ApiError on login -> ConfigEntryNotReady",
            _Client(login_exc=ApiError("500", "server got itself in trouble")),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "RequestFailedError on login -> ConfigEntryNotReady",
            _Client(login_exc=RequestFailedError("bad response")),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "DNS failure on get_devices -> ConfigEntryNotReady",
            _Client(devices_exc=ClientConnectorDNSError("no route")),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "ApiError on get_devices -> ConfigEntryNotReady",
            _Client(devices_exc=ApiError("500", "boom")),
            ("raises", ConfigEntryNotReady),
        ),

        # --- Bug 2: unlisted auth codes must not be swallowed ---------------
        (
            "auth SESSION_CLOSED -> ConfigEntryNotReady",
            _Client(login_exc=_auth_error(C.SESSION_CLOSED)),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "auth API_ERROR -> ConfigEntryNotReady",
            _Client(login_exc=_auth_error(C.API_ERROR)),
            ("raises", ConfigEntryNotReady),
        ),
        (
            "auth TOKEN_EXPIRED -> ConfigEntryNotReady",
            _Client(login_exc=_auth_error(C.TOKEN_EXPIRED)),
            ("raises", ConfigEntryNotReady),
        ),

        # --- regression guards: existing behaviour must be preserved -------
        (
            "auth INVALID_CREDENTIALS -> ConfigEntryAuthFailed",
            _Client(login_exc=_auth_error(C.INVALID_CREDENTIALS)),
            ("raises", ConfigEntryAuthFailed),
        ),
        (
            "auth INVALID_USERNAME_OR_PASSWORD -> ConfigEntryAuthFailed",
            _Client(login_exc=_auth_error(C.INVALID_USERNAME_OR_PASSWORD)),
            ("raises", ConfigEntryAuthFailed),
        ),
        (
            "happy path -> returns True",
            _Client(),
            ("returns", True),
        ),
    ]

    failures = 0
    for name, client, (kind, expected) in cases:
        (outcome, value), hass = _run(client)

        if kind == "raises":
            ok = outcome == "raised" and isinstance(value, expected)
            got = (
                f"raised {type(value).__name__}"
                if outcome == "raised"
                else f"returned {value!r}"
            )
            want = f"raises {expected.__name__}"
        else:
            ok = outcome == "returned" and value == expected
            got = (
                f"returned {value!r}"
                if outcome == "returned"
                else f"raised {type(value).__name__}"
            )
            want = f"returns {expected!r}"

        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<52} want={want:<34} got={got}")

    # A failed setup must never report success to Home Assistant.
    print()
    (outcome, value), hass = _run(
        _Client(login_exc=_auth_error(AuthenticationErrorCodes.SESSION_CLOSED))
    )
    forwarded = hass.config_entries.forwarded
    ok = outcome == "raised" and not forwarded
    failures += not ok
    print(
        f"[{'PASS' if ok else 'FAIL'}] "
        f"{'failed setup does not forward platforms':<52} "
        f"want={'no forward + raise':<34} "
        f"got={'forwarded=' + str(forwarded)}, {outcome}"
    )

    # The happy path must still forward platforms.
    (outcome, value), hass = _run(_Client())
    ok = outcome == "returned" and hass.config_entries.forwarded
    failures += not ok
    print(
        f"[{'PASS' if ok else 'FAIL'}] "
        f"{'happy path forwards platforms':<52} "
        f"want={'forwarded=True':<34} "
        f"got=forwarded={hass.config_entries.forwarded}"
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
