"""Tests for the coordinator's login handling during a poll.

Background
----------
`_async_update_data` logs in first when the client reports it is not logged
in. When `refresh_data` fails with an `AuthenticationError` (an expired
token), it logs in again, fetches a fresh device object and retries the
refresh once. If the retry fails as well, the error goes through the normal
mapping: bad credentials start reauth (`ConfigEntryAuthFailed`), anything
else is `UpdateFailed`. A failed consumption fetch only logs a warning; the
poll still succeeds with the fresh device data.

These describe current behaviour; they were listed as missing coverage in the
audit (wpatrik14/fleet-backlog#88). The real `_async_update_data` is loaded
out of coordinator.py via AST and driven with a stub client.

Intentionally dependency-free (stdlib only):

    python3 tests/test_coordinator_relogin.py
"""
import __future__
import ast
import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import types

COORDINATOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "coordinator.py"
)


class ClientError(Exception):
    pass


class ApiError(ClientError):
    def __init__(self, error_code=None, error_message=""):
        super().__init__(error_message)
        self.error_code = error_code


class AuthenticationError(ApiError):
    pass


class AuthenticationErrorCodes:
    INVALID_USERNAME_OR_PASSWORD = "1001-1401"
    INVALID_CREDENTIALS = "1000-1401"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"


aioaquarea = types.SimpleNamespace(
    ClientError=ClientError,
    AuthenticationError=AuthenticationError,
    AuthenticationErrorCodes=AuthenticationErrorCodes,
)


class UpdateFailed(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class _Device:
    long_id = "LONG"

    def __init__(self, client, number):
        self.client = client
        self.number = number

    async def refresh_data(self):
        self.client.log.append(f"refresh#{self.number}")
        if self.client.refresh_errors:
            raise self.client.refresh_errors.pop(0)


class _Client:
    def __init__(self, is_logged=True, refresh_errors=(), consumption_error=None):
        self.is_logged = is_logged
        self.refresh_errors = list(refresh_errors)
        self.consumption_error = consumption_error
        self.log = []
        self.devices = 0

    async def login(self):
        self.log.append("login")
        self.is_logged = True

    async def get_device(self, **kwargs):
        self.devices += 1
        self.log.append(f"get_device#{self.devices}")
        return _Device(self, self.devices)

    async def get_device_consumption(self, long_id, date_type, date_str):
        self.log.append("consumption")
        if self.consumption_error is not None:
            raise self.consumption_error
        return ["entry"]


class _Logger:
    def __init__(self):
        self.warnings = []

    def debug(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        self.warnings.append(args)


LOGGER = _Logger()


def _load():
    with open(COORDINATOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaDataUpdateCoordinator"
    )
    node = next(
        n for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_update_data"
    )
    flow = ast.ClassDef(
        name="Extracted", bases=[], keywords=[], body=[node],
        decorator_list=[], type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    namespace = {
        "aioaquarea": aioaquarea,
        "UpdateFailed": UpdateFailed,
        "ConfigEntryAuthFailed": ConfigEntryAuthFailed,
        "dt_util": types.SimpleNamespace(
            now=lambda: datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc),
            get_time_zone=lambda *_: None,
        ),
        "timedelta": timedelta,
        "DateType": types.SimpleNamespace(MONTH="month"),
        "_LOGGER": LOGGER,
    }
    exec(
        compile(
            module, COORDINATOR, "exec",
            flags=__future__.annotations.compiler_flag, dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


Extracted = _load()


def _poll(client):
    obj = Extracted()
    obj._client = client
    obj._device_info = None
    obj._month_consumption = None
    obj._last_monthly_fetch_time = None
    obj.consumption_interval = 60
    obj.hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"))
    try:
        result = asyncio.run(obj._async_update_data())
    except (UpdateFailed, ConfigEntryAuthFailed) as err:
        return obj, type(err).__name__
    return obj, result


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    # Logged in: no login call.
    client = _Client()
    obj, result = _poll(client)
    check("logged-in client: no login",
          client.log == ["get_device#1", "refresh#1", "consumption"],
          f"log={client.log}")
    check("returns the fetched device",
          isinstance(result, _Device) and result.number == 1, f"result={result!r}")
    check("caches the consumption", obj._month_consumption == ["entry"],
          f"cache={obj._month_consumption}")

    # Not logged in: log in before fetching.
    client = _Client(is_logged=False)
    _, result = _poll(client)
    check("logged-out client: login first",
          client.log[:2] == ["login", "get_device#1"], f"log={client.log}")

    # Token expired during refresh: login, fresh device, retry once.
    client = _Client(refresh_errors=[
        AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED)
    ])
    _, result = _poll(client)
    check("expired token: re-login and retry with a fresh device",
          client.log == ["get_device#1", "refresh#1", "login", "get_device#2",
                         "refresh#2", "consumption"],
          f"log={client.log}")
    check("expired token: poll succeeds with the fresh device",
          isinstance(result, _Device) and result.number == 2, f"result={result!r}")

    # Retry fails with the same transient error: UpdateFailed, no third try.
    client = _Client(refresh_errors=[
        AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED),
        AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED),
    ])
    _, result = _poll(client)
    check("retry fails again (transient) -> UpdateFailed",
          result == "UpdateFailed" and client.log.count("login") == 1,
          f"result={result} log={client.log}")

    # Retry fails because the credentials are now invalid: reauth.
    client = _Client(refresh_errors=[
        AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED),
        AuthenticationError(AuthenticationErrorCodes.INVALID_CREDENTIALS),
    ])
    _, result = _poll(client)
    check("retry fails with invalid credentials -> reauth",
          result == "ConfigEntryAuthFailed", f"result={result}")

    # Consumption failure is not fatal.
    LOGGER.warnings.clear()
    client = _Client(consumption_error=ClientError("consumption down"))
    obj, result = _poll(client)
    check("consumption failure: poll still returns the device",
          isinstance(result, _Device), f"result={result!r}")
    check("consumption failure: logged, cache and timestamp untouched",
          len(LOGGER.warnings) == 1 and obj._month_consumption is None
          and obj._last_monthly_fetch_time is None,
          f"warnings={LOGGER.warnings}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
