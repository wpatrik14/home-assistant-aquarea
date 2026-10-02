"""Regression test: every translation has the config-flow messages strings.json defines.

Background
----------
#65 added `config.abort.reauth_successful` (shown when a reauth completes) to
strings.json and en.json only, so the ten other languages fell back to
English for it. This test compares the `config.abort` and `config.error` keys
of strings.json with each file in translations/, so a message added to one
place but not the others is caught.

Intentionally dependency-free (stdlib only):

    python3 tests/test_translations.py
"""
import glob
import json
import os
import sys

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")
SECTIONS = ("abort", "error")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<40} {detail}")

    strings = _load(os.path.join(BASE, "strings.json"))
    files = sorted(glob.glob(os.path.join(BASE, "translations", "*.json")))
    check("found the translation files", len(files) >= 11, f"count={len(files)}")

    for path in files:
        translation = _load(path)
        name = os.path.basename(path)
        for section in SECTIONS:
            wanted = set(strings["config"].get(section, {}))
            have = translation.get("config", {}).get(section, {})
            missing = sorted(wanted - set(have))
            empty = sorted(k for k in wanted & set(have) if not str(have[k]).strip())
            check(f"{name}: config.{section} complete", not missing and not empty,
                  f"missing={missing} empty={empty}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
