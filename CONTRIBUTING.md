# Contributing

Thanks for helping. Bug reports and feature ideas go in
[issues](https://github.com/wpatrik14/home-assistant-aquarea/issues); security
problems follow [SECURITY.md](SECURITY.md).

## Development setup

Python 3.14 is required (Home Assistant 2026.x). The tests do not run on native
Windows; use WSL2 or a container (see "Running the tests" in the README).

```bash
python3.14 -m venv .venv
. .venv/bin/activate
pip install -r requirements_test.txt -r requirements_lint.txt -r requirements_typing.txt
```

## Checks

CI (`.github/workflows/`) runs the following, so run them before opening a PR:

```bash
ruff check .
ruff format --check .          # `ruff format .` fixes it
mypy custom_components/aquarea
pytest                          # tests/ha/, needs 100% coverage of config_flow.py
for t in tests/test_*.py; do python "$t" || echo "FAILED: $t"; done   # stdlib scripts
```

The translation tests (`tests/test_translations.py`, `tests/test_icons.py`) are
among the stdlib scripts: when you add a string to `strings.json`, add the key
to `translations/en.json` and every other language file (the
tests tell you what is missing). hassfest and HACS
validation run on every push as well.

## Pull requests

- One concern per PR; keep the diff small.
- Add or update a test for behaviour changes.
- Do not bump the version in `manifest.json` or the pinned requirements in a
  feature PR. Releases (version, tag, GitHub release) are done by the maintainer
  only; the `aioaquarea-ng` bump is automated by `bump-aioaquarea.yml`.
- Explain what changed and why in the PR description.
