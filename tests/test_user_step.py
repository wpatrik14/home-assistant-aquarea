"""Regression tests for the initial setup step (`async_step_user`).

Background
----------
`async_step_user` is the form shown when the integration is added: it sets a
lower-cased username as the unique ID, aborts if that account is already
configured, then tries to log in via `_validate_input` and either creates the
entry or re-shows the form with an error. `_validate_input` maps aioaquarea
exceptions to the error keys in strings.json:

    AuthenticationError, wrong credentials  -> invalid_auth
    AuthenticationError, session closed or
        token expired (transient)           -> cannot_connect
    AuthenticationError, any other code     -> invalid_auth
    ApiError                                -> api_error   (message shown via `api_error_msg`)
    RequestFailedError                      -> cannot_connect
    aiohttp.ClientError, TimeoutError       -> cannot_connect
    anything else                           -> unknown

Before, every AuthenticationError said "invalid authentication", so a
transient SESSION_CLOSED/TOKEN_EXPIRED told the user their correct password
was wrong. Network failures (aiohttp.ClientError, TimeoutError), which
aioaquarea does not wrap, ended up as "unknown" with a logged traceback.

The order of those `except` clauses matters: in aioaquarea,
`AuthenticationError` is a *subclass* of `ApiError`, so catching `ApiError`
first would turn every wrong password into a generic "API error". The stubs
below reproduce that hierarchy so a reordering is caught.

These tests load the *actual* `async_step_user`, `_validate_input` and the
`async_show_form` override out of config_flow.py (via AST, so they exercise the
shipped code rather than a copy) into a class built on a stub `ConfigFlow`
base, and drive them with a stub aioaquarea client.

Intentionally dependency-free (stdlib only) so it runs without Home Assistant,
aioaquarea, aiohttp, voluptuous or pytest installed:

    python3 tests/test_user_step.py
"""

import __future__

import ast
import asyncio
import os
import sys
import types

CONF_USERNAME = "username"
CONF_PASSWORD = "password"

CONFIG_FLOW = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "aquarea",
    "config_flow.py",
)


# --- stub aioaquarea (mirrors aioaquarea.errors, including the hierarchy) --
class ClientError(Exception):
    """Base exception for all aioaquarea client errors."""


class RequestFailedError(ClientError):
    def __init__(self, response):
        self.response = response
        super().__init__()

    def __str__(self):
        return self.response


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


class _FakeClient:
    """Stands in for aioaquarea.Client; `login` raises `_FakeClient.fails_with`."""

    fails_with = None
    created = []

    def __init__(self, session, username, password):
        self.session = session
        self.username = username
        self.password = password
        self.logged_in = False
        self.refresh_token = None
        _FakeClient.created.append(self)

    async def login(self):
        if _FakeClient.fails_with is not None:
            raise _FakeClient.fails_with
        self.logged_in = True


class AuthenticationErrorCodes:
    SESSION_CLOSED = "1001-0001"
    INVALID_USERNAME_OR_PASSWORD = "1001-1401"
    INVALID_CREDENTIALS = "1000-1401"
    API_ERROR = "API_ERROR"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"


class _AiohttpClientError(Exception):
    """Stands in for aiohttp.ClientError."""


class _AiohttpConnectorError(_AiohttpClientError):
    """Stands in for aiohttp.ClientConnectorError (a ClientError subclass)."""


aiohttp = types.SimpleNamespace(ClientError=_AiohttpClientError)

aioaquarea = types.SimpleNamespace(
    Client=_FakeClient,
    AuthenticationError=AuthenticationError,
    AuthenticationErrorCodes=AuthenticationErrorCodes,
    errors=types.SimpleNamespace(
        ApiError=ApiError,
        AuthenticationError=AuthenticationError,
        RequestFailedError=RequestFailedError,
    ),
)


# --- stub Home Assistant ConfigFlow base -----------------------------------
class _AbortFlow(Exception):
    """Stands in for homeassistant.data_entry_flow.AbortFlow."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Logger:
    """Records log calls instead of printing tracebacks into the test output."""

    def __init__(self):
        self.calls = []

    def error(self, *args):
        self.calls.append(("error", args))

    def exception(self, *args):
        self.calls.append(("exception", args))


class _ConfigFlow:
    """What the extracted methods need from config_entries.ConfigFlow."""

    _session = None
    _api_error_msg = None
    _challenge = None  # no multi-factor challenge pending

    def __init__(self, configured=()):
        self.hass = object()
        self.unique_id = None
        self._configured = set(configured)
        self.sessions_created = 0

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        if self.unique_id in self._configured:
            raise _AbortFlow("already_configured")

    def add_suggested_values_to_schema(self, schema, suggested):
        return (schema, suggested)

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(
        self,
        *,
        step_id=None,
        data_schema=None,
        errors=None,
        description_placeholders=None,
        last_step=None,
        preview=None,
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
            "description_placeholders": description_placeholders,
        }


def _create_clientsession(hass):
    flow = _current_flow[0]
    flow.sessions_created += 1
    return f"session-{flow.sessions_created}"


_current_flow = [None]
STEP_USER_DATA_SCHEMA = "STEP_USER_DATA_SCHEMA"
_LOGGER = _Logger()


# --- pull the real methods out of config_flow.py ---------------------------
def _load_flow_class():
    with open(CONFIG_FLOW, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaConfigFlow"
    )
    wanted = (
        "async_step_user",
        "_validate_input",
        "_entry_data",
        "async_show_form",
    )
    methods = [
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted
    ]
    assert sorted(m.name for m in methods) == sorted(wanted), [m.name for m in methods]
    # The async_show_form override calls zero-argument super(), which needs
    # the methods to live in a class body - so wrap them in one.
    flow_cls = ast.ClassDef(
        name="ExtractedFlow",
        bases=[ast.Name("_ConfigFlow", ast.Load())],
        keywords=[],
        body=methods,
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module([flow_cls], []))
    namespace = {
        "_ConfigFlow": _ConfigFlow,
        "aioaquarea": aioaquarea,
        "MFA_REQUIRED": "MFA_REQUIRED",
        "MFA_EXPIRED": "MFA_EXPIRED",
        "CONF_REFRESH_TOKEN": "refresh_token",
        "aiohttp": aiohttp,
        "async_create_clientsession": _create_clientsession,
        "STEP_USER_DATA_SCHEMA": STEP_USER_DATA_SCHEMA,
        "CONF_USERNAME": CONF_USERNAME,
        "CONF_PASSWORD": CONF_PASSWORD,
        "_LOGGER": _LOGGER,
    }
    # config_flow.py has `from __future__ import annotations`; compile with the
    # same flag so its annotations are never evaluated, on any Python version.
    code = compile(
        module,
        CONFIG_FLOW,
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    exec(code, namespace)
    return namespace["ExtractedFlow"]


ExtractedFlow = _load_flow_class()


# --- helpers ---------------------------------------------------------------
def _new_flow(**kwargs):
    flow = ExtractedFlow(**kwargs)
    _current_flow[0] = flow
    _FakeClient.fails_with = None
    _FakeClient.created = []
    return flow


def _submit(flow, user_input):
    try:
        return asyncio.run(flow.async_step_user(dict(user_input)))
    except _AbortFlow as exc:
        return {"type": "abort", "reason": exc.reason}


USER_INPUT = {
    CONF_USERNAME: "Someone@Example.com",
    CONF_PASSWORD: "secret",
}


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<62} {detail}")

    # --- first render ----------------------------------------------------
    flow = _new_flow()
    result = asyncio.run(flow.async_step_user())
    check(
        "no input shows the empty user form",
        result["type"] == "form"
        and result["step_id"] == "user"
        and result["errors"] == {}
        and result["data_schema"] == (STEP_USER_DATA_SCHEMA, None),
        f"got={result!r}",
    )
    check("no input does not try to log in", _FakeClient.created == [])

    # --- successful login ------------------------------------------------
    flow = _new_flow()
    result = _submit(flow, USER_INPUT)
    check(
        "valid login creates the entry with the submitted data",
        result
        == {
            "type": "create_entry",
            "title": USER_INPUT[CONF_USERNAME],
            "data": USER_INPUT,
        },
        f"got={result!r}",
    )
    check(
        "unique ID is the lower-cased username",
        flow.unique_id == "someone@example.com",
        f"got={flow.unique_id!r}",
    )
    client = _FakeClient.created[-1] if _FakeClient.created else None
    check(
        "client gets the session and the submitted credentials",
        client is not None
        and client.logged_in
        and (client.session, client.username, client.password)
        == ("session-1", USER_INPUT[CONF_USERNAME], USER_INPUT[CONF_PASSWORD]),
        f"got={client and (client.session, client.username, client.password)!r}",
    )

    # --- duplicate account -----------------------------------------------
    flow = _new_flow(configured={"someone@example.com"})
    result = _submit(flow, USER_INPUT)
    check(
        "already-configured account aborts (case-insensitive)",
        result == {"type": "abort", "reason": "already_configured"},
        f"got={result!r}",
    )
    check("duplicate account does not try to log in", _FakeClient.created == [])

    # --- login failures map to the strings.json error keys ---------------
    cases = [
        (
            "AuthenticationError -> invalid_auth (not api_error)",
            AuthenticationError("1001-1401", "Invalid username or password"),
            "invalid_auth",
        ),
        (
            "AuthenticationError, invalid credentials -> invalid_auth",
            AuthenticationError("1000-1401", "Invalid credentials"),
            "invalid_auth",
        ),
        (
            "AuthenticationError, other code -> invalid_auth",
            AuthenticationError("API_ERROR", "Error in login: status 401"),
            "invalid_auth",
        ),
        (
            "AuthenticationError, session closed -> cannot_connect",
            AuthenticationError("1001-0001", "Session closed"),
            "cannot_connect",
        ),
        (
            "AuthenticationError, token expired -> cannot_connect",
            AuthenticationError("TOKEN_EXPIRED", "Token expired"),
            "cannot_connect",
        ),
        (
            "aiohttp.ClientError -> cannot_connect",
            _AiohttpConnectorError("dns failure"),
            "cannot_connect",
        ),
        ("TimeoutError -> cannot_connect", TimeoutError(), "cannot_connect"),
        (
            "ApiError -> api_error",
            ApiError("5000-0001", "Service unavailable"),
            "api_error",
        ),
        (
            "RequestFailedError -> cannot_connect",
            RequestFailedError("timeout"),
            "cannot_connect",
        ),
        ("unexpected exception -> unknown", ValueError("boom"), "unknown"),
    ]
    for name, exc, expected in cases:
        flow = _new_flow()
        _FakeClient.fails_with = exc
        result = _submit(flow, USER_INPUT)
        check(
            name,
            result["type"] == "form" and result["errors"] == {"base": expected},
            f"got={result.get('errors', result)!r}",
        )

    # --- details of the error form ---------------------------------------
    flow = _new_flow()
    _FakeClient.fails_with = ApiError("5000-0001", "Service unavailable")
    result = _submit(flow, USER_INPUT)
    placeholders = result.get("description_placeholders") or {}
    check(
        "api_error shows the API message via api_error_msg",
        placeholders.get("api_error_msg")
        == "API error: 5000-0001 - Service unavailable",
        f"got={placeholders!r}",
    )
    check(
        "error form keeps the submitted values as suggestions",
        result.get("data_schema") == (STEP_USER_DATA_SCHEMA, USER_INPUT),
        f"got={result.get('data_schema')!r}",
    )

    flow = _new_flow()
    _FakeClient.fails_with = AuthenticationError("1001-1401", "Invalid")
    _submit(flow, USER_INPUT)
    _FakeClient.fails_with = None
    result = _submit(flow, USER_INPUT)
    check(
        "retry after a failure succeeds and reuses the session",
        result["type"] == "create_entry"
        and flow.sessions_created == 1
        and [c.session for c in _FakeClient.created] == ["session-1", "session-1"],
        f"got={result['type']}, sessions={flow.sessions_created}",
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
