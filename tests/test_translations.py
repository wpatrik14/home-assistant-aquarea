"""Regression test: translations match the config-flow strings strings.json defines.

Background
----------
#65 added `config.abort.reauth_successful` (shown when a reauth completes) to
strings.json and en.json only, so the ten other languages fell back to
English for it. This test compares the `config.abort` and `config.error` keys
of strings.json with each file in translations/, so a message added to one
place but not the others is caught.

It also compares the form fields of every config and options step. When the
`scan_interval` option was removed, its label stayed in all eleven
translations; a field must now be labelled in a translation exactly when
strings.json has it, and a translation may not describe (`data_description`)
a field strings.json does not. Descriptions themselves may be missing:
Home Assistant falls back to English for them.

Intentionally dependency-free (stdlib only):

    python3 tests/test_translations.py
"""

import glob
import json
import os
import sys

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")
SECTIONS = ("abort", "error")
FLOWS = ("config", "options")


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
            check(
                f"{name}: config.{section} complete",
                not missing and not empty,
                f"missing={missing} empty={empty}",
            )

        for flow in FLOWS:
            steps = strings.get(flow, {}).get("step", {})
            have_steps = translation.get(flow, {}).get("step", {})
            extra_steps = sorted(set(have_steps) - set(steps))
            check(
                f"{name}: {flow} steps known", not extra_steps, f"extra={extra_steps}"
            )
            for step_id, step in steps.items():
                have = have_steps.get(step_id, {})
                wanted = set(step.get("data", {}))
                got = set(have.get("data", {}))
                check(
                    f"{name}: {flow}.{step_id} fields",
                    wanted == got,
                    f"missing={sorted(wanted - got)} extra={sorted(got - wanted)}",
                )
                stray = sorted(
                    set(have.get("data_description", {}))
                    - set(step.get("data_description", {}))
                )
                check(
                    f"{name}: {flow}.{step_id} descriptions",
                    not stray,
                    f"extra={stray}",
                )

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
