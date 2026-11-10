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
a field strings.json does not. Descriptions must be translated too: a
translation has exactly the `data_description` keys strings.json has.

Entity names and states (`entity.*`) must have the same keys in every
translation as in strings.json, with no empty values and the same
{placeholders}; they must also be translated: a value identical to the English
one is only accepted for the (file, key) pairs in INTENTIONALLY_ENGLISH. Every
step description must exist in every translation too, as must the exceptions.

Intentionally dependency-free (stdlib only):

    python3 tests/test_translations.py
"""

import glob
import json
import os
import re
import sys

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "aquarea")
SECTIONS = ("abort", "error")
FLOWS = ("config", "options")

# Strings that are the same as the English one on purpose: a brand or mode name
# Panasonic uses untranslated, or a word that is spelled the same in the language.
INTENTIONALLY_ENGLISH = {
    ("de.json", "entity.binary_sensor.status.name"),
    ("nl.json", "entity.binary_sensor.status.name"),
    ("pl.json", "entity.binary_sensor.status.name"),
    ("nl.json", "entity.water_heater.tank.name"),
    ("es.json", "entity.select.powerful_time.name"),
    ("it.json", "entity.select.powerful_time.name"),
    ("lt.json", "entity.select.powerful_time.name"),
    ("it.json", "config.step.user.data.password"),
    ("it.json", "config.step.reauth_confirm.data.password"),
    ("it.json", "config.step.reconfigure.data.password"),
}


def _leaves(node, path=""):
    """Map the dotted path of every string below `node` to its value."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            out.update(_leaves(value, f"{path}.{key}" if path else key))
        return out
    return {path: node}


def _placeholders(text):
    return set(re.findall(r"\{(\w+)\}", str(text)))


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
                wanted_desc = set(step.get("data_description", {}))
                got_desc = set(have.get("data_description", {}))
                check(
                    f"{name}: {flow}.{step_id} descriptions",
                    wanted_desc == got_desc,
                    f"missing={sorted(wanted_desc - got_desc)} "
                    f"extra={sorted(got_desc - wanted_desc)}",
                )

    # Repair issues: every translation has a title and a description with the
    # same {placeholders} as strings.json.
    issues = strings.get("issues", {})
    for path in files:
        translation = _load(path)
        name = os.path.basename(path)
        for issue_id, issue in issues.items():
            have = translation.get("issues", {}).get(issue_id, {})
            for field in ("title", "description"):
                wanted = set(re.findall(r"\{(\w+)\}", issue.get(field, "")))
                got = set(re.findall(r"\{(\w+)\}", have.get(field, "")))
                check(
                    f"{name}: issues.{issue_id}.{field}",
                    bool(have.get(field, "").strip()) and wanted == got,
                    f"placeholders {sorted(wanted)} vs {sorted(got)}",
                )

    # Entity names/states and exceptions: same keys, no empty values, same
    # placeholders as strings.json, and translated.
    for section in ("entity", "exceptions"):
        wanted = _leaves(strings.get(section, {}), section)
        for path in files:
            name = os.path.basename(path)
            have = _leaves(_load(path).get(section, {}), section)
            missing = sorted(set(wanted) - set(have))
            extra = sorted(set(have) - set(wanted))
            check(
                f"{name}: {section} keys",
                not missing and not extra,
                f"missing={missing} extra={extra}",
            )
            empty = sorted(
                k for k in set(have) & set(wanted) if not str(have[k]).strip()
            )
            check(f"{name}: {section} not empty", not empty, f"empty={empty}")
            bad = sorted(
                k
                for k in set(have) & set(wanted)
                if _placeholders(have[k]) != _placeholders(wanted[k])
            )
            check(f"{name}: {section} placeholders", not bad, f"mismatch={bad}")

    # Every translation (not English itself) translates every string it shares
    # with the English file, and describes every field strings.json describes.
    english = _leaves(_load(os.path.join(BASE, "translations", "en.json")))
    for path in files:
        name = os.path.basename(path)
        if name == "en.json":
            continue
        have = _leaves(_load(path))
        untranslated = sorted(
            k
            for k, v in have.items()
            if english.get(k) == v
            and isinstance(v, str)
            and (name, k) not in INTENTIONALLY_ENGLISH
            and not re.fullmatch(r"[\W\d_]*|\{\w+\}", v)
        )
        check(
            f"{name}: no untranslated strings",
            not untranslated,
            f"english={untranslated}",
        )
        lacking = sorted(k for k in english if k not in have)
        check(f"{name}: has every en.json string", not lacking, f"missing={lacking}")

    print()
    print("ALL PASSED" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
