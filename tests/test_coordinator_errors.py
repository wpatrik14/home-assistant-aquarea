"""Regression tests: every aioaquarea error during a poll becomes a clean result.

Background
----------
`_async_update_data` must turn library errors into `UpdateFailed` (entities go
unavailable, one log line) or `ConfigEntryAuthFailed` (starts reauth). It used
to catch only `AuthenticationError` and `RequestFailedError`. In aioaquarea
`ApiError` (non-auth) and `InvalidData` are siblings of `RequestFailedError`
under `ClientError`, so they escaped to Home Assistant's generic handler, which
logs "Unexpected error fetching ..." with a full traceback on every poll for
the length of a cloud outage. The stand-in exceptions below copy aioaquarea's
hierarchy.

The real `_async_update_data` is loaded out of coordinator.py via AST (so the
shipped code is exercised, not a copy) and driven with a stub client.

Intentionally dependency-free (stdlib only):

    python3 tests/test_coordinator_errors.py
"""

import __future__
import ast
import asyncio
import os
import sys
import types

COORDINATOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "coordinator.py"
)


# --- stub aioaquarea (mirrors aioaquarea.errors, including the hierarchy) --
class ClientError(Exception):
    pass


class RequestFailedError(ClientError):
    pass


class InvalidData(ClientError):
    pass


class ApiError(ClientError):
    def __init__(self, error_code=None, error_message=""):
        super().__init__(error_message)
        self.error_code = error_code


class AuthenticationError(ApiError):
    pass


class AuthenticationErrorCodes:
    INVALID_USERNAME_OR_PASSWORD = "1"
    INVALID_CREDENTIALS = "2"
    TOKEN_EXPIRED = "3"


aioaquarea = types.SimpleNamespace(
    ClientError=ClientError,
    AuthenticationError=AuthenticationError,
    AuthenticationErrorCodes=AuthenticationErrorCodes,
    errors=types.SimpleNamespace(
        ApiError=ApiError,
        RequestFailedError=RequestFailedError,
        InvalidData=InvalidData,
    ),
)


class UpdateFailed(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class _Client:
    is_logged = True

    def __init__(self, exc):
        self.exc = exc

    async def get_device(self, **kwargs):
        raise self.exc


def _load():
    with open(COORDINATOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaDataUpdateCoordinator"
    )
    node = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_update_data"
    )
    flow = ast.ClassDef(
        name="Extracted",
        bases=[],
        keywords=[],
        body=[node],
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow], []))
    anything = types.SimpleNamespace(now=lambda: 0, get_time_zone=lambda *_: None)
    namespace = {
        "aioaquarea": aioaquarea,
        "UpdateFailed": UpdateFailed,
        "ConfigEntryAuthFailed": ConfigEntryAuthFailed,
        "dt_util": anything,
        "timedelta": lambda **kw: 0,
        "DateType": types.SimpleNamespace(MONTH="month"),
        "_LOGGER": types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None
        ),
    }
    exec(
        compile(
            module,
            COORDINATOR,
            "exec",
            flags=__future__.annotations.compiler_flag,
            dont_inherit=True,
        ),
        namespace,
    )
    return namespace["Extracted"]


Extracted = _load()


def _poll(exc):
    obj = Extracted()
    obj._client = _Client(exc)
    obj._device_info = None
    obj.hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"))
    try:
        asyncio.run(obj._async_update_data())
    except (UpdateFailed, ConfigEntryAuthFailed) as err:
        return type(err).__name__
    except Exception as err:  # what Home Assistant would log with a traceback
        return f"ESCAPED {type(err).__name__}"
    return "no error"


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    cases = [
        (
            "invalid credentials -> reauth",
            AuthenticationError(AuthenticationErrorCodes.INVALID_CREDENTIALS),
            "ConfigEntryAuthFailed",
        ),
        (
            "invalid username/password -> reauth",
            AuthenticationError(AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD),
            "ConfigEntryAuthFailed",
        ),
        (
            "other auth error -> UpdateFailed",
            AuthenticationError(AuthenticationErrorCodes.TOKEN_EXPIRED),
            "UpdateFailed",
        ),
        (
            "RequestFailedError -> UpdateFailed",
            RequestFailedError("timeout"),
            "UpdateFailed",
        ),
        ("non-auth ApiError -> UpdateFailed", ApiError("5000", "down"), "UpdateFailed"),
        ("InvalidData -> UpdateFailed", InvalidData("bad payload"), "UpdateFailed"),
    ]
    for name, exc, expected in cases:
        got = _poll(exc)
        check(name, got == expected, f"got={got!r}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
