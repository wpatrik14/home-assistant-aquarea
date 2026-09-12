"""Regression tests for reauth when no username can be determined.

Background
----------
`_try_get_username` tries four sources in order - a cached `_username`, the
config entry data, the flow handler's `init_data`, and finally the unique ID -
and returns `None` when all four miss. It was annotated `-> str` with a
`# type: ignore[return-value]` on that final `return None`.

The caller, `async_step_reauth`, used the result unconditionally: `None` would
flow into `aioaquarea.Client(session, None, password)` (surfacing as a
misleading `invalid_auth`) and into the reauth form's
`description_placeholders`. It only happens for a config entry that carries
neither a username nor a username-shaped unique ID - plausible only for one
written by a very old version - but the failure was confusing rather than
clear.

The fix: `_try_get_username` is now honestly typed `-> str | None`, and
`async_step_reauth` aborts with `reauth_no_username` when it returns `None`.

These tests load the *actual* `_try_get_username` and `async_step_reauth` out
of config_flow.py (via AST, so they exercise the shipped code rather than a
copy) and drive them with a stub flow handler.

Intentionally dependency-free (stdlib only) so it runs without Home Assistant,
aioaquarea, aiohttp or pytest installed:

    python3 tests/test_reauth_username.py
"""
import ast
import asyncio
import os
import sys

CONF_USERNAME = "username"
CONF_PASSWORD = "password"

CONFIG_FLOW = os.path.join(
    os.path.dirname(__file__),
    "..", "custom_components", "aquarea", "config_flow.py",
)


# --- pull the real methods out of config_flow.py --------------------------
def _load_methods():
    with open(CONFIG_FLOW, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "AquareaConfigFlow"
    )
    wanted = ("_try_get_username", "async_step_reauth")
    nodes = {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in wanted
    }
    namespace = {
        "CONF_USERNAME": CONF_USERNAME,
        "CONF_PASSWORD": CONF_PASSWORD,
    }
    module = ast.Module([nodes[name] for name in wanted], [])
    exec(compile(module, CONFIG_FLOW, "exec"), namespace)
    return namespace["_try_get_username"], namespace["async_step_reauth"]


_try_get_username, async_step_reauth = _load_methods()


# --- fake flow handler ----------------------------------------------------
class _Abort:
    def __init__(self, reason):
        self.type = "abort"
        self.reason = reason


class _Form:
    def __init__(self, username, errors):
        self.type = "form"
        self.username = username
        self.errors = errors


class _FakeFlow:
    """Stands in for AquareaConfigFlow with just what the two methods touch."""

    def __init__(self, *, cached=None, init_data=None, unique_id=None):
        self._username = cached
        self.init_data = init_data
        self.unique_id = unique_id
        self._validated_with = None

    # bound copies of the extracted methods
    _try_get_username = _try_get_username
    async_step_reauth = async_step_reauth

    def async_abort(self, *, reason):
        return _Abort(reason)

    async def _validate_input(self, username, password):
        self._validated_with = (username, password)
        return {}

    async def async_complete_reauth(self, username, password):
        return ("complete", username, password)

    async def async_show_reauth_form(self, username, errors=None):
        return _Form(username, errors)


def _run(coro):
    return asyncio.run(coro)


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<58} {detail}")

    # --- _try_get_username: source precedence ----------------------------
    flow = _FakeFlow(cached="cached-user")
    check(
        "cached _username wins",
        flow._try_get_username({CONF_USERNAME: "entry-user"}) == "cached-user",
    )

    flow = _FakeFlow()
    check(
        "entry data username used",
        flow._try_get_username({CONF_USERNAME: "entry-user"}) == "entry-user",
    )

    flow = _FakeFlow(init_data={CONF_USERNAME: "init-user"})
    check(
        "init_data username used when entry data has none",
        flow._try_get_username({}) == "init-user",
    )

    flow = _FakeFlow(unique_id="unique-user")
    check(
        "unique_id used as last resort",
        flow._try_get_username({}) == "unique-user",
    )

    flow = _FakeFlow()
    got = flow._try_get_username({})
    check("returns None when every source misses", got is None, f"got={got!r}")

    flow = _FakeFlow(unique_id=None)
    got = flow._try_get_username(None)
    check("entry_data=None is tolerated", got is None, f"got={got!r}")

    # --- async_step_reauth: abort vs proceed ----------------------------
    flow = _FakeFlow()
    result = _run(flow.async_step_reauth({}))
    check(
        "reauth aborts with reauth_no_username when no username is known",
        isinstance(result, _Abort) and result.reason == "reauth_no_username",
        f"got={getattr(result, 'reason', result)!r}",
    )

    flow = _FakeFlow(unique_id="unique-user")
    result = _run(flow.async_step_reauth({}))
    check(
        "reauth shows the form (not None) when a username is known",
        isinstance(result, _Form) and result.username == "unique-user",
        f"got={getattr(result, 'username', result)!r}",
    )

    flow = _FakeFlow(cached="cached-user")
    result = _run(flow.async_step_reauth({}, {CONF_PASSWORD: "pw"}))
    check(
        "reauth validates with the resolved username, never None",
        result == ("complete", "cached-user", "pw"),
        f"got={result!r}",
    )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
