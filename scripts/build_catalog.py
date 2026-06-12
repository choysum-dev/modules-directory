#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Build the static catalog directory by fetching metadata from NPM."""

from __future__ import annotations

import base64
import concurrent.futures
import http.client
import hashlib
import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def read_float_env(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


ROOT = Path(__file__).resolve().parents[1]
TRUST_TIERS = ("official", "verified", "community")
CATALOG_ROOT = ROOT / "modules"
DIST_ROOT = ROOT / "dist"
V1_ROOT = DIST_ROOT / "v1"
SCHEMA_SRC = ROOT / "schemas"
SCHEMA_OUT = V1_ROOT / "schema"
NPM_FETCH_TIMEOUT_SECONDS = read_int_env("CHOYSUM_NPM_FETCH_TIMEOUT_SECONDS", 10)
NPM_FETCH_MAX_RETRIES = read_int_env("CHOYSUM_NPM_FETCH_MAX_RETRIES", 3)
NPM_FETCH_BACKOFF_SECONDS = read_float_env("CHOYSUM_NPM_FETCH_BACKOFF_SECONDS", 1.0)
BUILD_CONCURRENCY = read_int_env("CHOYSUM_BUILD_CONCURRENCY", 5)

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    write_text(path, text)

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_integrity(dist_meta: dict[str, Any], package_name: str, version: str) -> str:
    integrity = dist_meta.get("integrity")
    if isinstance(integrity, str) and integrity.strip():
        return integrity

    shasum = dist_meta.get("shasum")
    if isinstance(shasum, str) and shasum.strip():
        shasum_str = shasum.strip()
        if len(shasum_str) != 40:
            raise ValueError(
                f"Invalid shasum length for package '{package_name}' version '{version}'"
            )
        try:
            digest = bytes.fromhex(shasum_str)
        except ValueError as exc:
            raise ValueError(
                f"Invalid shasum hex for package '{package_name}' version '{version}'"
            ) from exc
        return "sha1-" + base64.b64encode(digest).decode("ascii")

    raise ValueError(
        f"Missing integrity hash for package '{package_name}' version '{version}'"
    )


def resolve_tarball(dist_meta: dict[str, Any], package_name: str, version: str) -> str:
    tarball = dist_meta.get("tarball")
    if isinstance(tarball, str) and tarball.strip():
        return tarball.strip()
    raise ValueError(
        f"Missing dist.tarball for package '{package_name}' version '{version}'"
    )


def fetch_npm_meta(package_name: str) -> dict:
    quoted_name = urllib.parse.quote(package_name, safe="@")
    url = f"https://registry.npmjs.org/{quoted_name}"
    req = urllib.request.Request(url, headers={"User-Agent": "Choysum-Catalog-Builder/1.0"})
    last_error: Exception | None = None

    for attempt in range(1, NPM_FETCH_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=NPM_FETCH_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("NPM registry response is not a JSON object")
                return payload
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise RuntimeError(
                    f"Package '{package_name}' fetch failed with status {exc.code}."
                ) from exc
            last_error = exc
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            socket.timeout,
            TimeoutError,
            ConnectionError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc

        if attempt < NPM_FETCH_MAX_RETRIES:
            backoff_seconds = NPM_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1))
            time.sleep(backoff_seconds)

    if last_error is None:
        raise RuntimeError(f"Failed to fetch metadata for {package_name}: unknown error")
    raise RuntimeError(
        f"Failed to fetch or parse {package_name} from NPM "
        f"after {NPM_FETCH_MAX_RETRIES} attempts: {last_error}"
    ) from last_error

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
    versions_raw = npm_data.get("versions")
    if not isinstance(versions_raw, dict):
        versions_raw = {}
    for ver, v_data in versions_raw.items():
        if not isinstance(v_data, dict):
            continue
        choysum_meta = v_data.get("choysum")
        if not isinstance(choysum_meta, dict):
            choysum_meta = {}
        dist_meta = v_data.get("dist")
        if not isinstance(dist_meta, dict):
            dist_meta = {}
        
        tarball_url = resolve_tarball(dist_meta, package_name, ver)
        
        depends = choysum_meta.get("depends")
        if not isinstance(depends, list):
            depends = []

        peer_deps = v_data.get("peerDependencies")
        if not isinstance(peer_deps, dict):
            peer_deps = {}

        integrity = resolve_integrity(dist_meta, package_name, ver)

        v_entry = {
            "tarball": tarball_url,
            "integrity": integrity,
            "depends": depends,
            "peerDependencies": peer_deps
        }
        if "compatibility" in choysum_meta:
            v_entry["compatibility"] = choysum_meta["compatibility"]
            
        versions_out[ver] = v_entry

    if not versions_out:
        raise ValueError(
            f"No valid versions found for package '{package_name}' (module: {module_id})"
        )
        
    return module_id, {
        "moduleId": module_id,
        "package": package_name,
        "trust": entry.get("trust"),
        "maintainers": entry.get("maintainers", []),
        "versions": versions_out
    }

def collect_modules() -> dict[str, dict[str, Any]]:
    modules: dict[str, dict[str, Any]] = {}
    module_sources: dict[str, Path] = {}
    tasks: dict[concurrent.futures.Future[tuple[str, dict[str, Any]]], Path] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=BUILD_CONCURRENCY) as executor:
        for trust in TRUST_TIERS:
            tier_dir = CATALOG_ROOT / trust
            if not tier_dir.is_dir():
                continue

            for entry_file in sorted(tier_dir.glob("*.json")):
                future = executor.submit(process_module, entry_file)
                tasks[future] = entry_file

        errors: list[str] = []
        for future in concurrent.futures.as_completed(tasks):
            entry_file = tasks[future]
            try:
                module_id, mod_payload = future.result()
                if module_id in modules:
                    existing_file = module_sources[module_id]
                    errors.append(
                        f"  - Duplicate module ID '{module_id}' between "
                        f"{existing_file.relative_to(ROOT)} and {entry_file.relative_to(ROOT)}"
                    )
                    continue
                modules[module_id] = mod_payload
                module_sources[module_id] = entry_file
            except Exception as e:
                errors.append(f"  - {entry_file.relative_to(ROOT)}: {e}")

    if errors:
        raise RuntimeError("Failed to collect all modules due to the following errors:\n" + "\n".join(errors))

    return modules

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def generate_checksums(files: list[Path]) -> str:
    lines = []
    for f in sorted(files, key=lambda p: p.relative_to(DIST_ROOT).as_posix()):
        rel_path = f"/{f.relative_to(DIST_ROOT).as_posix()}"
        hasher = hashlib.sha256()
        with f.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
        lines.append(f"{hasher.hexdigest()}  {rel_path}")
    return "\n".join(lines) + "\n"

def build() -> None:
    modules = collect_modules()
    generated_at = utc_now_iso()

    if DIST_ROOT.is_symlink():
        DIST_ROOT.unlink()
    elif DIST_ROOT.exists() and DIST_ROOT.is_dir():
        shutil.rmtree(DIST_ROOT)
    elif DIST_ROOT.exists():
        DIST_ROOT.unlink()
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    V1_ROOT.mkdir(parents=True, exist_ok=True)
    SCHEMA_OUT.mkdir(parents=True, exist_ok=True)

    index_payload = {
        "generatedAt": generated_at,
        "modules": modules,
        "version": 1,
    }
    canonical_index = json.dumps(index_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    index_hash = hashlib.sha256(canonical_index.encode("utf-8")).hexdigest()

    index_path = V1_ROOT / "index.json"
    index_hashed_path = V1_ROOT / f"index.{index_hash}.json"
    write_text(index_path, canonical_index)
    write_text(index_hashed_path, canonical_index)

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

    headers_src = ROOT / "_headers"
    redirects_src = ROOT / "_redirects"
    if not headers_src.is_file():
        raise RuntimeError(f"Missing required static file: {headers_src.relative_to(ROOT)}")
    if not redirects_src.is_file():
        raise RuntimeError(f"Missing required static file: {redirects_src.relative_to(ROOT)}")

    write_text(DIST_ROOT / "_headers", headers_src.read_text(encoding="utf-8"))
    write_text(DIST_ROOT / "_redirects", redirects_src.read_text(encoding="utf-8"))

    # Finally generate checksums
    checksums_path = V1_ROOT / "checksums.txt"
    checksums_content = generate_checksums(checksum_files)
    write_text(checksums_path, checksums_content)

    print(f"Successfully built catalog artifacts under dist/ (total modules: {len(modules)})")

if __name__ == "__main__":
    build()
