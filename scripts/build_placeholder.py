#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Build minimal static directory artifacts for bootstrapping."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRUST_TIERS = ("official", "verified", "community")
CATALOG_ROOT = ROOT / "modules"
DIST_ROOT = ROOT / "dist"
V1_ROOT = DIST_ROOT / "v1"
SCHEMA_SRC = ROOT / "schemas"
SCHEMA_OUT = V1_ROOT / "schema"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    write_text(path, text)


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def collect_modules() -> list[dict[str, Any]]:
    modules: list[dict[str, Any]] = []
    for trust in TRUST_TIERS:
        tier_dir = CATALOG_ROOT / trust
        if not tier_dir.is_dir():
            continue

        for entry_file in sorted(tier_dir.glob("*.json")):
            entry = load_json(entry_file)
            if not isinstance(entry, dict):
                raise ValueError(f"Catalog entry must be an object: {entry_file}")

            modules.append(
                {
                    "id": entry_file.stem,
                    "package": entry.get("package"),
                    "trust": entry.get("trust"),
                    "maintainers": entry.get("maintainers", []),
                }
            )
    return modules


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build() -> None:
    V1_ROOT.mkdir(parents=True, exist_ok=True)
    SCHEMA_OUT.mkdir(parents=True, exist_ok=True)

    generated_at = utc_now_iso()
    modules = collect_modules()
    index_payload = {
        "generatedAt": generated_at,
        "modules": modules,
        "version": 1,
    }
    canonical_index = json.dumps(index_payload, sort_keys=True, separators=(",", ":"))
    index_hash = hashlib.sha256(canonical_index.encode("utf-8")).hexdigest()

    index_path = V1_ROOT / "index.json"
    index_hashed_path = V1_ROOT / f"index.{index_hash}.json"
    write_text(index_path, canonical_index + "\n")
    write_text(index_hashed_path, canonical_index + "\n")

    meta_payload = {
        "generatedAt": generated_at,
        "indexHash": index_hash,
        "indexPath": f"/v1/index.{index_hash}.json",
    }
    meta_path = V1_ROOT / "meta.json"
    write_json(meta_path, meta_payload)

    copied_schema_paths: list[Path] = []
    for schema_file in sorted(SCHEMA_SRC.glob("*.json")):
        target = SCHEMA_OUT / schema_file.name
        target.write_text(schema_file.read_text(encoding="utf-8"), encoding="utf-8")
        copied_schema_paths.append(target)

    checksum_targets = [index_path, index_hashed_path, meta_path, *copied_schema_paths]
    checksum_lines = [
        f"{sha256_of_file(path)}  /{path.relative_to(DIST_ROOT).as_posix()}"
        for path in checksum_targets
    ]
    write_text(V1_ROOT / "checksums.txt", "\n".join(checksum_lines) + "\n")

    headers_content = (
        "/v1/index.json\n"
        "  Cache-Control: public, max-age=300, stale-while-revalidate=3600\n\n"
        "/v1/index.*.json\n"
        "  Cache-Control: public, max-age=31536000, immutable\n\n"
        "/v1/meta.json\n"
        "  Cache-Control: public, max-age=60, stale-while-revalidate=300\n\n"
        "/v1/schema/*\n"
        "  Cache-Control: public, max-age=86400\n\n"
        "/v1/checksums.txt\n"
        "  Cache-Control: public, max-age=300\n"
    )
    write_text(DIST_ROOT / "_headers", headers_content)
    write_text(DIST_ROOT / "_redirects", "/ /v1/index.json 302\n")

    print("Built placeholder artifacts under dist/")


if __name__ == "__main__":
    build()