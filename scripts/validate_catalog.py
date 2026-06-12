#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Validate catalog pointers and repository structure for Phase 0."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

TRUST_TIERS = ("official", "verified", "community")
ALLOWED_KEYS = {"$schema", "package", "trust", "maintainers"}
GITHUB_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "modules"
SCHEMA_PATH = ROOT / "schemas" / "catalog-entry.schema.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_repo_structure(errors: list[str]) -> None:
    for trust in TRUST_TIERS:
        tier_dir = CATALOG_ROOT / trust
        if not tier_dir.is_dir():
            errors.append(f"Missing directory: {tier_dir.relative_to(ROOT)}")

    if not SCHEMA_PATH.is_file():
        errors.append(f"Missing schema file: {SCHEMA_PATH.relative_to(ROOT)}")
        return

    try:
        schema_data = load_json(SCHEMA_PATH)
    except json.JSONDecodeError as exc:
        errors.append(
            f"Schema JSON parse error in {SCHEMA_PATH.relative_to(ROOT)}: {exc.msg}"
        )
        return

    if not isinstance(schema_data, dict):
        errors.append("Schema root must be a JSON object")


def collect_entry_files() -> list[Path]:
    paths: list[Path] = []
    for trust in TRUST_TIERS:
        tier_dir = CATALOG_ROOT / trust
        if tier_dir.is_dir():
            paths.extend(sorted(tier_dir.glob("*.json")))
    return paths


def validate_entry(path: Path, errors: list[str]) -> None:
    relative = path.relative_to(ROOT)
    try:
        entry = load_json(path)
    except json.JSONDecodeError as exc:
        errors.append(f"JSON parse error in {relative}: {exc.msg}")
        return

    if not isinstance(entry, dict):
        errors.append(f"{relative}: root must be a JSON object")
        return

    missing = sorted({"package", "trust", "maintainers"} - set(entry.keys()))
    if missing:
        errors.append(f"{relative}: missing required fields: {', '.join(missing)}")

    unknown_keys = sorted(set(entry.keys()) - ALLOWED_KEYS)
    if unknown_keys:
        errors.append(f"{relative}: unknown fields: {', '.join(unknown_keys)}")

    package = entry.get("package")
    if not isinstance(package, str) or not package.strip():
        errors.append(f"{relative}: package must be a non-empty string")
    elif package.startswith("http://") or package.startswith("https://"):
        errors.append(f"{relative}: package must be an npm package coordinate, not a URL")

    trust = entry.get("trust")
    if trust not in TRUST_TIERS:
        errors.append(f"{relative}: trust must be one of {', '.join(TRUST_TIERS)}")
    else:
        expected_trust = path.parent.name
        if trust != expected_trust:
            errors.append(
                f"{relative}: trust '{trust}' does not match directory tier '{expected_trust}'"
            )

    module_id = path.stem
    if not MODULE_ID_RE.fullmatch(module_id):
        errors.append(
            f"{relative}: module id '{module_id}' must match pattern {MODULE_ID_RE.pattern}"
        )

    schema_ref = entry.get("$schema")
    if schema_ref is not None and not isinstance(schema_ref, str):
        errors.append(f"{relative}: $schema must be a string when provided")

    maintainers = entry.get("maintainers")
    if not isinstance(maintainers, list) or not maintainers:
        errors.append(f"{relative}: maintainers must be a non-empty array")
        return

    for index, maintainer in enumerate(maintainers):
        marker = f"{relative}: maintainers[{index}]"
        if not isinstance(maintainer, dict):
            errors.append(f"{marker} must be an object")
            continue

        if "github" not in maintainer:
            errors.append(f"{marker} missing required field 'github'")
            continue

        github = maintainer.get("github")
        if not isinstance(github, str) or not GITHUB_ID_RE.fullmatch(github):
            errors.append(
                f"{marker}.github must match pattern {GITHUB_ID_RE.pattern}"
            )

        extra = sorted(set(maintainer.keys()) - {"github"})
        if extra:
            errors.append(f"{marker} has unsupported fields: {', '.join(extra)}")


def main() -> int:
    errors: list[str] = []
    validate_repo_structure(errors)

    entry_files = collect_entry_files()
    for path in entry_files:
        validate_entry(path, errors)

    if errors:
        print("Catalog validation failed:")
        for item in errors:
            print(f"- {item}")
        return 1

    print(f"Catalog validation passed ({len(entry_files)} pointer file(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())