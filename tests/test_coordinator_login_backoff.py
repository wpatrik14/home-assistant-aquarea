"""Tests for the login back-off after Panasonic refuses the login.

Background
----------
Panasonic's edge security can answer the login with an HTTP 403 "request is
blocked" page (wpatrik14/home-assistant-aquarea#165). The library raises that
as `AuthenticationError(API_ERROR)`, which the coordinator maps to
`UpdateFailed`, so every 60 second poll started another login from the same
blocked address. `_async_login` now waits 5 minutes after the first such
failure, doubling to at most 30 minutes, without touching the network in
between. Wrong credentials, MFA and network errors do not start a back-off.

The real `_async_login` is loaded out of coordinator.py via AST (so the
shipped code is exercised, not a copy) and driven with a stub client and a
controllable clock.

Intentionally dependency-free (stdlib only):

    python3 tests/test_coordinator_login_backoff.py
"""

import __future__

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import os
import sys
import types

COORDINATOR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "aquarea", "coordinator.py"
)


class ClientError(Exception):
    pass


class RequestFailedError(ClientError):
    pass


class ApiError(ClientError):
    def __init__(self, error_code=None, error_message=""):
        super().__init__(error_message)
        self.error_code = error_code


class AuthenticationError(ApiError):
    pass


class AuthenticationErrorCodes:
    INVALID_USERNAME_OR_PASSWORD = "1001-1401"
    API_ERROR = "API_ERROR"


aioaquarea = types.SimpleNamespace(
    AuthenticationError=AuthenticationError,
    AuthenticationErrorCodes=AuthenticationErrorCodes,
)


class UpdateFailed(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.kwargs = kwargs


class _Clock:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


class _Client:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.logins = 0

    async def login(self):
        self.logins += 1
        if self.errors:
            raise self.errors.pop(0)


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, *args, **kwargs):
        self.warnings.append(args)


LOGGER = _Logger()


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
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_login"
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
    namespace = {
        "aioaquarea": aioaquarea,
        "UpdateFailed": UpdateFailed,
        "DOMAIN": "aquarea",
        "dt_util": types.SimpleNamespace(
            utcnow=lambda: _Clock.now, as_local=lambda moment: moment
        ),
        "LOGIN_BACKOFF_INITIAL": timedelta(minutes=5),
        "LOGIN_BACKOFF_MAX": timedelta(minutes=30),
        "_LOGGER": LOGGER,
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


def _coordinator(client):
    obj = Extracted()
    obj._client = client
    obj._login_failures = 0
    obj._login_retry_at = None
    return obj


def _login(obj):
    try:
        asyncio.run(obj._async_login())
    except (UpdateFailed, AuthenticationError, ClientError) as err:
        return type(err).__name__
    return "ok"


def _blocked():
    return AuthenticationError(
        AuthenticationErrorCodes.API_ERROR, "Error in authorize: Unexpected status 403"
    )


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    # A refused login starts the back-off and is re-raised unchanged.
    _Clock.now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    client = _Client([_blocked(), _blocked(), _blocked(), _blocked(), _blocked()])
    obj = _coordinator(client)
    check("first refusal is raised", _login(obj) == "AuthenticationError")
    check(
        "back-off of 5 minutes starts",
        obj._login_retry_at == _Clock.now + timedelta(minutes=5)
        and obj._login_failures == 1,
        f"retry_at={obj._login_retry_at}",
    )
    check("one warning logged", len(LOGGER.warnings) == 1)

    # While it lasts, no login reaches the network.
    _Clock.now += timedelta(minutes=1)
    check("poll during back-off: UpdateFailed", _login(obj) == "UpdateFailed")
    check("poll during back-off: no login call", client.logins == 1)
    _Clock.now += timedelta(minutes=3, seconds=59)
    check("just before the end: still waiting", _login(obj) == "UpdateFailed")
    check("just before the end: still no login call", client.logins == 1)

    # After it, one more attempt; a second refusal doubles the wait.
    _Clock.now += timedelta(seconds=1)
    check("back-off over: tries again", _login(obj) == "AuthenticationError")
    check("second attempt reached the network", client.logins == 2)
    check(
        "second refusal doubles the wait to 10 minutes",
        obj._login_retry_at == _Clock.now + timedelta(minutes=10),
        f"retry_at={obj._login_retry_at}",
    )

    # The wait is capped at 30 minutes.
    for expected in (20, 30, 30):
        _Clock.now = obj._login_retry_at
        _login(obj)
        check(
            f"wait after failure {obj._login_failures} is {expected} minutes",
            obj._login_retry_at == _Clock.now + timedelta(minutes=expected),
            f"retry_at={obj._login_retry_at}",
        )

    # A success clears the state.
    _Clock.now = obj._login_retry_at
    obj._client = _Client()
    check("success after the back-off", _login(obj) == "ok")
    check(
        "success resets the counter and the deadline",
        obj._login_failures == 0 and obj._login_retry_at is None,
    )

    # Other failures do not start a back-off.
    for name, error in (
        (
            "wrong credentials",
            AuthenticationError(AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD),
        ),
        ("network error", RequestFailedError("down")),
    ):
        client = _Client([error])
        obj = _coordinator(client)
        result = _login(obj)
        check(
            f"{name}: raised, no back-off",
            result in ("AuthenticationError", "RequestFailedError")
            and obj._login_retry_at is None
            and obj._login_failures == 0,
            f"result={result}",
        )
        check(f"{name}: next poll logs in again", _login(obj) == "ok")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
