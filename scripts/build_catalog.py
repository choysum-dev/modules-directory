#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Build the static catalog directory by fetching metadata from NPM."""

from __future__ import annotations

import hashlib
import json
import urllib.request
import urllib.error
import concurrent.futures
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

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)

def fetch_npm_meta(package_name: str) -> dict:
    import urllib.parse
    quoted_name = urllib.parse.quote(package_name, safe="@")
    url = f"https://registry.npmjs.org/{quoted_name}"
    req = urllib.request.Request(url, headers={"User-Agent": "Choysum-Catalog-Builder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to fetch {package_name} from NPM: {e}")

def process_module(entry_file: Path) -> tuple[str, dict[str, Any]]:
    entry = load_json(entry_file)
    if not isinstance(entry, dict):
        raise ValueError(f"Catalog entry must be a JSON object: {entry_file}")
    module_id = entry_file.stem
    package_name = entry.get("package")
    if not isinstance(package_name, str) or not package_name.strip():
        raise ValueError(f"Invalid or missing 'package' field in {entry_file}")
    
    print(f"Fetching NPM metadata for {package_name} (module: {module_id})...")
    npm_data = fetch_npm_meta(package_name)
    
    versions_out = {}
    for ver, v_data in (npm_data.get("versions") or {}).items():
        if not isinstance(v_data, dict):
            continue
        choysum_meta = v_data.get("choysum")
        if not isinstance(choysum_meta, dict):
            choysum_meta = {}
        dist_meta = v_data.get("dist")
        if not isinstance(dist_meta, dict):
            dist_meta = {}
        
        # Tarball redirect format matching the Phase 4.3 specification
        tarball_url = f"https://registry.choysum.dev/v1/tarballs/{package_name}/{ver}.tgz"
        
        v_entry = {
            "tarball": tarball_url,
            "integrity": dist_meta.get("integrity", ""),
            "depends": choysum_meta.get("depends", []),
            "peerDependencies": v_data.get("peerDependencies") or {}
        }
        if "compatibility" in choysum_meta:
            v_entry["compatibility"] = choysum_meta["compatibility"]
            
        versions_out[ver] = v_entry
        
    return module_id, {
        "moduleId": module_id,
        "package": package_name,
        "trust": entry.get("trust"),
        "maintainers": entry.get("maintainers", []),
        "versions": versions_out
    }

def collect_modules() -> dict[str, dict[str, Any]]:
    modules: dict[str, dict[str, Any]] = {}
    tasks = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for trust in TRUST_TIERS:
            tier_dir = CATALOG_ROOT / trust
            if not tier_dir.is_dir():
                continue
                
            for entry_file in sorted(tier_dir.glob("*.json")):
                tasks.append(executor.submit(process_module, entry_file))
                
        for future in concurrent.futures.as_completed(tasks):
            module_id, mod_payload = future.result()
            modules[module_id] = mod_payload
            
    return modules

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def generate_checksums(files: list[Path]) -> str:
    lines = []
    for f in sorted(files):
        rel_path = f"/{f.relative_to(DIST_ROOT).as_posix()}"
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{h}  {rel_path}")
    return "\n".join(lines) + "\n"

def build() -> None:
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
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

    # Copy schemas
    checksum_files = [index_path, index_hashed_path, meta_path]
    for schema_file in sorted(SCHEMA_SRC.glob("*.json")):
        target = SCHEMA_OUT / schema_file.name
        target.write_text(schema_file.read_text(encoding="utf-8"), encoding="utf-8")
        checksum_files.append(target)

    # Note: _headers and _redirects are managed statically in the Git repository now.
    # We will copy them to the dist folder so Cloudflare Pages can use them.
    if (ROOT / "_headers").exists():
        write_text(DIST_ROOT / "_headers", (ROOT / "_headers").read_text(encoding="utf-8"))
    if (ROOT / "_redirects").exists():
        write_text(DIST_ROOT / "_redirects", (ROOT / "_redirects").read_text(encoding="utf-8"))

    # Finally generate checksums
    checksums_path = V1_ROOT / "checksums.txt"
    checksums_content = generate_checksums(checksum_files)
    write_text(checksums_path, checksums_content)

    print(f"Successfully built catalog artifacts under dist/ (total modules: {len(modules)})")

if __name__ == "__main__":
    build()
