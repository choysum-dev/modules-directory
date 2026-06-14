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
import re
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
RANGE_TOKEN_RE = re.compile(r"^(<=|>=|<|>)(.+)$")
RANGE_OPERATORS = {"<", "<=", ">", ">="}


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]


@dataclass(frozen=True)
class Bound:
    version: SemVer
    inclusive: bool

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    write_text(path, text)

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_semver(version_text: str) -> SemVer:
    match = SEMVER_RE.fullmatch(version_text.strip())
    if not match:
        raise ValueError(f"Invalid SemVer version '{version_text}'.")

    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return SemVer(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        prerelease=prerelease,
    )


def format_semver(version: SemVer) -> str:
    rendered = f"{version.major}.{version.minor}.{version.patch}"
    if version.prerelease:
        rendered += "-" + ".".join(version.prerelease)
    return rendered


def compare_semver(left: SemVer, right: SemVer) -> int:
    if left.major != right.major:
        return -1 if left.major < right.major else 1
    if left.minor != right.minor:
        return -1 if left.minor < right.minor else 1
    if left.patch != right.patch:
        return -1 if left.patch < right.patch else 1

    if not left.prerelease and not right.prerelease:
        return 0
    if not left.prerelease:
        return 1
    if not right.prerelease:
        return -1

    length = min(len(left.prerelease), len(right.prerelease))
    for index in range(length):
        left_id = left.prerelease[index]
        right_id = right.prerelease[index]
        if left_id == right_id:
            continue

        left_is_num = left_id.isdigit()
        right_is_num = right_id.isdigit()

        if left_is_num and right_is_num:
            left_num = int(left_id)
            right_num = int(right_id)
            return -1 if left_num < right_num else 1
        if left_is_num != right_is_num:
            return -1 if left_is_num else 1
        return -1 if left_id < right_id else 1

    if len(left.prerelease) == len(right.prerelease):
        return 0
    return -1 if len(left.prerelease) < len(right.prerelease) else 1


def max_lower_bound(left: Bound, right: Bound) -> Bound:
    cmp_result = compare_semver(left.version, right.version)
    if cmp_result > 0:
        return left
    if cmp_result < 0:
        return right
    return Bound(version=left.version, inclusive=left.inclusive and right.inclusive)


def min_upper_bound(left: Bound, right: Bound) -> Bound:
    cmp_result = compare_semver(left.version, right.version)
    if cmp_result < 0:
        return left
    if cmp_result > 0:
        return right
    return Bound(version=left.version, inclusive=left.inclusive and right.inclusive)


def ensure_non_empty_interval(lower: Bound, upper: Bound, range_text: str) -> None:
    cmp_result = compare_semver(lower.version, upper.version)
    if cmp_result > 0:
        raise ValueError(f"choysum.cli range '{range_text}' has no satisfiable versions.")
    if cmp_result == 0 and not (lower.inclusive and upper.inclusive):
        raise ValueError(f"choysum.cli range '{range_text}' has no satisfiable versions.")


def normalize_range_tokens(range_text: str) -> list[str]:
    parts = range_text.strip().split()
    if not parts:
        return []

    tokens: list[str] = []
    cursor = 0
    while cursor < len(parts):
        current = parts[cursor]
        if current in RANGE_OPERATORS:
            if cursor + 1 >= len(parts):
                raise ValueError(
                    f"Invalid choysum.cli range '{range_text}': missing version after '{current}'."
                )
            tokens.append(current + parts[cursor + 1])
            cursor += 2
            continue
        tokens.append(current)
        cursor += 1

    return tokens


def parse_cli_range_with_major(range_text: str) -> tuple[str, int]:
    raw = range_text.strip()
    if not raw:
        raise ValueError("choysum.cli range must be a non-empty string.")
    if "||" in raw:
        raise ValueError(
            f"Invalid choysum.cli range '{range_text}': union ranges are not supported."
        )

    tokenized = normalize_range_tokens(raw)
    comparators: list[tuple[str, SemVer]] = []
    for token in tokenized:
        match = RANGE_TOKEN_RE.fullmatch(token)
        if not match:
            raise ValueError(
                f"Invalid choysum.cli range '{range_text}': expected comparators like '>=1.8.0 <2.0.0'."
            )
        operator = match.group(1)
        version = parse_semver(match.group(2))
        comparators.append((operator, version))

    lower_bounds = [
        Bound(version=version, inclusive=(operator == ">="))
        for operator, version in comparators
        if operator in (">", ">=")
    ]
    upper_bounds = [
        Bound(version=version, inclusive=(operator == "<="))
        for operator, version in comparators
        if operator in ("<", "<=")
    ]

    if not lower_bounds or not upper_bounds:
        raise ValueError(
            f"Invalid choysum.cli range '{range_text}': both lower and upper bounds are required."
        )

    lower = lower_bounds[0]
    for candidate in lower_bounds[1:]:
        lower = max_lower_bound(lower, candidate)

    upper = upper_bounds[0]
    for candidate in upper_bounds[1:]:
        upper = min_upper_bound(upper, candidate)

    ensure_non_empty_interval(lower, upper, range_text)

    lower_major = lower.version.major
    upper_major = upper.version.major
    if upper_major == lower_major:
        major = lower_major
    else:
        is_next_major_ceiling = (
            upper_major == lower_major + 1
            and not upper.inclusive
            and upper.version.minor == 0
            and upper.version.patch == 0
            and not upper.version.prerelease
        )
        if not is_next_major_ceiling:
            raise ValueError(
                f"Invalid choysum.cli range '{range_text}': range must stay within a single CLI major."
            )
        major = lower_major

    normalized = " ".join(
        f"{operator}{format_semver(version)}" for operator, version in comparators
    )
    return normalized, major


def resolve_choysum_cli_range(
    choysum_meta: dict[str, Any],
    module_id: str,
    package_name: str,
    version: str,
) -> tuple[str, int]:
    cli_range = choysum_meta.get("cli")
    if not isinstance(cli_range, str) or not cli_range.strip():
        raise ValueError(
            f"Module '{module_id}' version '{version}' is missing required field 'choysum.cli' "
            f"(package: '{package_name}')."
        )

    try:
        return parse_cli_range_with_major(cli_range)
    except ValueError as exc:
        raise ValueError(
            f"Module '{module_id}' version '{version}' has invalid choysum.cli range "
            f"'{cli_range}' (package: '{package_name}'): {exc}"
        ) from exc


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

def process_module(entry_file: Path) -> tuple[str, dict[str, Any], dict[str, int]]:
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
    version_major_map_out: dict[str, int] = {}
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

        normalized_cli_range, cli_major = resolve_choysum_cli_range(
            choysum_meta=choysum_meta,
            module_id=module_id,
            package_name=package_name,
            version=ver,
        )

        v_entry = {
            "tarball": tarball_url,
            "integrity": integrity,
            "depends": depends,
            "peerDependencies": peer_deps,
            "choysum": {
                "cli": normalized_cli_range,
            },
        }
            
        versions_out[ver] = v_entry
        version_major_map_out[ver] = cli_major

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
    }, version_major_map_out

def collect_modules() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, int]]]:
    modules: dict[str, dict[str, Any]] = {}
    module_version_major_map: dict[str, dict[str, int]] = {}
    module_sources: dict[str, Path] = {}
    tasks: dict[concurrent.futures.Future[tuple[str, dict[str, Any], dict[str, int]]], Path] = {}

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
                module_id, mod_payload, version_major_map = future.result()
                if module_id in modules:
                    existing_file = module_sources[module_id]
                    errors.append(
                        f"  - Duplicate module ID '{module_id}' between "
                        f"{existing_file.relative_to(ROOT)} and {entry_file.relative_to(ROOT)}"
                    )
                    continue
                modules[module_id] = mod_payload
                module_version_major_map[module_id] = version_major_map
                module_sources[module_id] = entry_file
            except Exception as e:
                errors.append(f"  - {entry_file.relative_to(ROOT)}: {e}")

    if errors:
        raise RuntimeError("Failed to collect all modules due to the following errors:\n" + "\n".join(errors))

    return modules, module_version_major_map

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


def build_index_payload(generated_at: str, modules: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "generatedAt": generated_at,
        "modules": modules,
        "version": 1,
    }


def write_index_artifacts(index_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path, str]:
    canonical_index = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    index_hash = hashlib.sha256(canonical_index.encode("utf-8")).hexdigest()

    index_path = index_dir / "index.json"
    index_hashed_path = index_dir / f"index.{index_hash}.json"
    write_text(index_path, canonical_index)
    write_text(index_hashed_path, canonical_index)
    return index_path, index_hashed_path, index_hash

def build() -> None:
    modules, module_version_major_map = collect_modules()
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

    checksum_files: list[Path] = []
    full_index_payload = build_index_payload(generated_at, modules)
    index_path, index_hashed_path, index_hash = write_index_artifacts(V1_ROOT, full_index_payload)
    checksum_files.extend([index_path, index_hashed_path])

    all_versions = {
        (module_id, version)
        for module_id, module_payload in modules.items()
        for version in module_payload.get("versions", {})
    }
    shard_versions: set[tuple[str, str]] = set()

    cli_major_indexes: dict[str, dict[str, str]] = {}
    all_majors = sorted(
        {
            major
            for version_major_map in module_version_major_map.values()
            for major in version_major_map.values()
        }
    )

    for major in all_majors:
        major_modules: dict[str, dict[str, Any]] = {}
        for module_id, module_payload in modules.items():
            module_versions = module_payload.get("versions")
            if not isinstance(module_versions, dict):
                continue

            major_versions: dict[str, Any] = {}
            for version, version_entry in module_versions.items():
                if module_version_major_map[module_id].get(version) == major:
                    key = (module_id, version)
                    if key in shard_versions:
                        raise RuntimeError(
                            f"Version '{version}' of module '{module_id}' was assigned to multiple CLI major shards."
                        )
                    shard_versions.add(key)
                    major_versions[version] = version_entry

            if major_versions:
                major_module_payload = dict(module_payload)
                major_module_payload["versions"] = major_versions
                major_modules[module_id] = major_module_payload

        major_index_payload = build_index_payload(generated_at, major_modules)
        major_dir = V1_ROOT / "cli" / str(major)
        major_index_path, major_index_hashed_path, major_hash = write_index_artifacts(
            major_dir,
            major_index_payload,
        )
        checksum_files.extend([major_index_path, major_index_hashed_path])
        cli_major_indexes[str(major)] = {
            "indexHash": major_hash,
            "indexPath": f"/v1/cli/{major}/index.{major_hash}.json",
        }

    if shard_versions != all_versions:
        missing = sorted(all_versions - shard_versions)
        extras = sorted(shard_versions - all_versions)
        details: list[str] = []
        if missing:
            details.append(f"missing in shards: {missing}")
        if extras:
            details.append(f"unexpected in shards: {extras}")
        raise RuntimeError(
            "CLI major shard consistency check failed: " + "; ".join(details)
        )

    meta_payload = {
        "generatedAt": generated_at,
        "indexHash": index_hash,
        "indexPath": f"/v1/index.{index_hash}.json",
        "cliMajorIndexes": cli_major_indexes,
    }
    meta_path = V1_ROOT / "meta.json"
    write_json(meta_path, meta_payload)
    checksum_files.append(meta_path)

    # Copy schemas
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

    shard_count = len(cli_major_indexes)
    print(
        "Successfully built catalog artifacts under dist/ "
        f"(total modules: {len(modules)}, cli major shards: {shard_count})"
    )

if __name__ == "__main__":
    build()
