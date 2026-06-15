#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026-present Brian Wang <wangbuke@gmail.com>
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Build the static catalog directory by fetching metadata from NPM."""

from __future__ import annotations

import base64
import binascii
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
TARBALL_VERIFY_TIMEOUT_SECONDS = read_int_env("CHOYSUM_TARBALL_VERIFY_TIMEOUT_SECONDS", 30)
TARBALL_MAX_BYTES = read_int_env("CHOYSUM_TARBALL_MAX_BYTES", 50 * 1024 * 1024)
OFFICIAL_PRE1_CLI_RANGE = ">=0.0.0-0 <0.0.0"
RANGE_TOKEN_RE = re.compile(r"^(<=|>=|<|>)(.+)$")
RANGE_OPERATORS = {"<", "<=", ">", ">="}
INTEGRITY_ALGORITHMS = {
    "sha1": 20,
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
}
INTEGRITY_ALGORITHM_PRIORITY = {
    "sha1": 1,
    "sha256": 2,
    "sha384": 3,
    "sha512": 4,
}
ALLOWED_TARBALL_SCHEMES = {"https", "http"}
TARBALL_CACHE_DIR_ENV = "CHOYSUM_CACHE_DIR"

ERROR_MODULE_NAME_MISSING = "CATALOG_E_MODULE_NAME_MISSING"
ERROR_MODULE_NAME_MISMATCH = "CATALOG_E_MODULE_NAME_MISMATCH"
ERROR_INTEGRITY_FORMAT = "CATALOG_E_INTEGRITY_FORMAT"
ERROR_INTEGRITY_UNSUPPORTED_ALGORITHM = "CATALOG_E_INTEGRITY_UNSUPPORTED_ALGORITHM"
ERROR_INTEGRITY_MISMATCH = "CATALOG_E_INTEGRITY_MISMATCH"
ERROR_TARBALL_DOWNLOAD = "CATALOG_E_TARBALL_DOWNLOAD"
ERROR_TARBALL_TOO_LARGE = "CATALOG_E_TARBALL_TOO_LARGE"
ERROR_TARBALL_URL_SCHEME = "CATALOG_E_TARBALL_URL_SCHEME"
ERROR_DEPENDS_INVALID_ID = "CATALOG_E_DEPENDS_INVALID_ID"
ERROR_DEPENDS_BROKEN_LINK = "CATALOG_E_DEPENDS_BROKEN_LINK"
ERROR_DEPENDS_DUPLICATE = "CATALOG_E_DEPENDS_DUPLICATE"
ERROR_DEPENDS_SELF_REFERENCE = "CATALOG_E_DEPENDS_SELF_REFERENCE"
ERROR_OFFICIAL_PRE1_CLI_RANGE = "CATALOG_E_OFFICIAL_PRE1_CLI_RANGE"
ERROR_MODULE_VERSION_INVALID = "CATALOG_E_MODULE_VERSION_INVALID"


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


def build_error(code: str, message: str) -> str:
    return f"[{code}] {message}"


def value_error(code: str, message: str) -> ValueError:
    return ValueError(build_error(code, message))

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    write_text(path, text)

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_numeric_identifier(value: str, field_name: str, version_text: str) -> int:
    if not value:
        raise ValueError(f"Invalid SemVer version '{version_text}'.")
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"Invalid SemVer version '{version_text}'.")
    if len(value) > 1 and value.startswith("0"):
        raise ValueError(
            f"Invalid SemVer version '{version_text}': {field_name} has a leading zero."
        )
    return int(value)


def validate_prerelease_identifiers(prerelease: str, version_text: str) -> tuple[str, ...]:
    identifiers = prerelease.split(".")
    normalized: list[str] = []
    for identifier in identifiers:
        if not identifier:
            raise ValueError(f"Invalid SemVer version '{version_text}'.")
        if not all((ch.isascii() and ch.isalnum()) or ch == "-" for ch in identifier):
            raise ValueError(f"Invalid SemVer version '{version_text}'.")
        if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
            raise ValueError(
                f"Invalid SemVer version '{version_text}': prerelease identifier "
                f"'{identifier}' has a leading zero."
            )
        normalized.append(identifier)
    return tuple(normalized)


def validate_build_identifiers(build_meta: str, version_text: str) -> None:
    for identifier in build_meta.split("."):
        if not identifier:
            raise ValueError(f"Invalid SemVer version '{version_text}'.")
        if not all((ch.isascii() and ch.isalnum()) or ch == "-" for ch in identifier):
            raise ValueError(f"Invalid SemVer version '{version_text}'.")


def parse_semver(version_text: str) -> SemVer:
    source = version_text.strip()
    if not source:
        raise ValueError(f"Invalid SemVer version '{version_text}'.")

    if source.startswith("v"):
        source = source[1:]
    if not source:
        raise ValueError(f"Invalid SemVer version '{version_text}'.")

    core_and_pre, sep_build, build_meta = source.partition("+")
    if sep_build:
        validate_build_identifiers(build_meta, version_text)

    core, sep_pre, prerelease_text = core_and_pre.partition("-")
    core_parts = core.split(".")
    if len(core_parts) != 3:
        raise ValueError(f"Invalid SemVer version '{version_text}'.")

    major = parse_numeric_identifier(core_parts[0], "major", version_text)
    minor = parse_numeric_identifier(core_parts[1], "minor", version_text)
    patch = parse_numeric_identifier(core_parts[2], "patch", version_text)

    prerelease: tuple[str, ...] = ()
    if sep_pre:
        prerelease = validate_prerelease_identifiers(prerelease_text, version_text)

    return SemVer(
        major=major,
        minor=minor,
        patch=patch,
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


def validate_module_name(
    choysum_meta: dict[str, Any],
    module_id: str,
    package_name: str,
    version: str,
) -> None:
    module_name = choysum_meta.get("moduleName")
    if not isinstance(module_name, str) or not module_name.strip():
        raise value_error(
            ERROR_MODULE_NAME_MISSING,
            f"Module '{module_id}' version '{version}' is missing required field "
            f"'choysum.moduleName' (package: '{package_name}').",
        )

    normalized = module_name.strip()
    if normalized != module_id:
        raise value_error(
            ERROR_MODULE_NAME_MISMATCH,
            f"Module '{module_id}' version '{version}' has choysum.moduleName "
            f"'{normalized}', expected '{module_id}' (package: '{package_name}').",
        )


def parse_integrity_value(integrity: str, package_name: str, version: str) -> tuple[str, bytes]:
    tokens = integrity.strip().split()
    if not tokens:
        raise value_error(
            ERROR_INTEGRITY_FORMAT,
            f"Package '{package_name}' version '{version}' has an empty integrity value.",
        )

    candidates: list[tuple[str, bytes]] = []
    has_supported_algorithm = False

    for token in tokens:
        algorithm, sep, digest_b64 = token.partition("-")
        if not sep or not algorithm or not digest_b64:
            continue

        normalized_algorithm = algorithm.lower()
        expected_length = INTEGRITY_ALGORITHMS.get(normalized_algorithm)
        if expected_length is None:
            continue
        has_supported_algorithm = True

        try:
            digest = base64.b64decode(digest_b64, validate=True)
        except (ValueError, binascii.Error):
            continue

        if len(digest) != expected_length:
            continue
        candidates.append((normalized_algorithm, digest))

    if not candidates:
        if not has_supported_algorithm:
            raise value_error(
                ERROR_INTEGRITY_UNSUPPORTED_ALGORITHM,
                f"Package '{package_name}' version '{version}' has no supported integrity "
                f"algorithms in '{integrity}'.",
            )

        raise value_error(
            ERROR_INTEGRITY_FORMAT,
            f"Package '{package_name}' version '{version}' has no valid integrity digest "
            f"in '{integrity}'.",
        )

    candidates.sort(
        key=lambda item: INTEGRITY_ALGORITHM_PRIORITY[item[0]],
        reverse=True,
    )
    return candidates[0]


def resolve_tarball_cache_file(algorithm: str, expected_digest: bytes) -> Path | None:
    cache_dir_raw = os.getenv(TARBALL_CACHE_DIR_ENV)
    if not cache_dir_raw:
        return None

    cache_dir = Path(cache_dir_raw).expanduser()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    return cache_dir / f"{algorithm}-{expected_digest.hex()}.tar"


def verify_cached_tarball(
    cache_file: Path,
    algorithm: str,
    expected_digest: bytes,
) -> bool:
    if not cache_file.is_file():
        return False

    hasher = hashlib.new(algorithm)
    total_bytes = 0

    try:
        with cache_file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                total_bytes += len(chunk)
                if total_bytes > TARBALL_MAX_BYTES:
                    try:
                        cache_file.unlink()
                    except OSError:
                        pass
                    return False
                hasher.update(chunk)
    except OSError:
        return False

    if hasher.digest() == expected_digest:
        return True

    try:
        cache_file.unlink()
    except OSError:
        pass
    return False


def verify_tarball_integrity(
    tarball_url: str,
    integrity: str,
    package_name: str,
    version: str,
) -> None:
    parsed_url = urllib.parse.urlparse(tarball_url)
    if parsed_url.scheme.lower() not in ALLOWED_TARBALL_SCHEMES or not parsed_url.netloc:
        raise value_error(
            ERROR_TARBALL_URL_SCHEME,
            f"Package '{package_name}' version '{version}' has disallowed tarball URL "
            f"scheme in '{tarball_url}'. Allowed schemes: {sorted(ALLOWED_TARBALL_SCHEMES)}.",
        )

    algorithm, expected_digest = parse_integrity_value(integrity, package_name, version)
    cache_file = resolve_tarball_cache_file(algorithm, expected_digest)
    if cache_file is not None and verify_cached_tarball(cache_file, algorithm, expected_digest):
        return

    hasher = hashlib.new(algorithm)
    req = urllib.request.Request(
        tarball_url,
        headers={"User-Agent": "Choysum-Catalog-Builder/1.0"},
    )
    total_bytes = 0
    temp_cache_file: Path | None = None
    cache_handle = None
    completed = False

    if cache_file is not None:
        temp_cache_file = cache_file.with_name(
            f"{cache_file.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        try:
            cache_handle = temp_cache_file.open("wb")
        except OSError:
            temp_cache_file = None

    try:
        with urllib.request.urlopen(req, timeout=TARBALL_VERIFY_TIMEOUT_SECONDS) as response:
            content_length_header = response.headers.get("Content-Length")
            if content_length_header:
                try:
                    content_length = int(content_length_header)
                except ValueError:
                    content_length = -1
                if content_length > TARBALL_MAX_BYTES:
                    raise value_error(
                        ERROR_TARBALL_TOO_LARGE,
                        f"Package '{package_name}' version '{version}' tarball content-length "
                        f"{content_length} exceeds max size {TARBALL_MAX_BYTES} bytes.",
                    )

            for chunk in iter(lambda: response.read(65536), b""):
                total_bytes += len(chunk)
                if total_bytes > TARBALL_MAX_BYTES:
                    raise value_error(
                        ERROR_TARBALL_TOO_LARGE,
                        f"Package '{package_name}' version '{version}' tarball exceeds "
                        f"max size {TARBALL_MAX_BYTES} bytes.",
                    )
                hasher.update(chunk)
                if cache_handle is not None:
                    cache_handle.write(chunk)

        actual_digest = hasher.digest()
        if actual_digest != expected_digest:
            expected_b64 = base64.b64encode(expected_digest).decode("ascii")
            actual_b64 = base64.b64encode(actual_digest).decode("ascii")
            raise value_error(
                ERROR_INTEGRITY_MISMATCH,
                f"Package '{package_name}' version '{version}' tarball integrity mismatch "
                f"for {algorithm}: expected '{expected_b64}', got '{actual_b64}'.",
            )
        completed = True
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            build_error(
                ERROR_TARBALL_DOWNLOAD,
                f"Failed to download tarball for package '{package_name}' version '{version}' "
                f"from '{tarball_url}' (status: {exc.code}).",
            )
        ) from exc
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        socket.timeout,
        TimeoutError,
        ConnectionError,
    ) as exc:
        raise RuntimeError(
            build_error(
                ERROR_TARBALL_DOWNLOAD,
                f"Failed to download tarball for package '{package_name}' version '{version}' "
                f"from '{tarball_url}': {exc}",
            )
        ) from exc
    finally:
        if cache_handle is not None:
            try:
                cache_handle.close()
            except OSError:
                pass
        if temp_cache_file is not None and temp_cache_file.exists():
            if completed and cache_file is not None:
                try:
                    temp_cache_file.replace(cache_file)
                except OSError:
                    try:
                        temp_cache_file.unlink()
                    except OSError:
                        pass
            else:
                try:
                    temp_cache_file.unlink()
                except OSError:
                    pass


def validate_official_pre1_cli_range(
    module_id: str,
    package_name: str,
    version: str,
    trust: Any,
    normalized_cli_range: str,
) -> None:
    if trust != "official":
        return

    try:
        parsed_version = parse_semver(version)
    except ValueError as exc:
        raise value_error(
            ERROR_MODULE_VERSION_INVALID,
            f"Official module '{module_id}' has invalid version key '{version}' "
            f"(package: '{package_name}').",
        ) from exc

    if parsed_version.major == 0 and parsed_version.minor == 0 and parsed_version.patch == 0:
        if normalized_cli_range != OFFICIAL_PRE1_CLI_RANGE:
            raise value_error(
                ERROR_OFFICIAL_PRE1_CLI_RANGE,
                f"Official module '{module_id}' version '{version}' must use "
                f"choysum.cli '{OFFICIAL_PRE1_CLI_RANGE}', got '{normalized_cli_range}' "
                f"(package: '{package_name}').",
            )


def validate_runtime_contracts(modules: dict[str, dict[str, Any]]) -> None:
    known_modules = set(modules.keys())
    errors: list[str] = []

    for module_id in sorted(modules.keys()):
        module_payload = modules[module_id]
        package_name = module_payload.get("package")
        versions = module_payload.get("versions")
        if not isinstance(versions, dict):
            continue

        for version in sorted(versions.keys()):
            version_payload = versions[version]
            if not isinstance(version_payload, dict):
                continue

            depends = version_payload.get("depends")
            if isinstance(depends, list):
                seen_deps: set[str] = set()
                for dep in depends:
                    if not isinstance(dep, str) or not dep.strip():
                        errors.append(
                            build_error(
                                ERROR_DEPENDS_INVALID_ID,
                                f"Module '{module_id}' version '{version}' has invalid "
                                f"depends entry {dep!r} (package: '{package_name}').",
                            )
                        )
                        continue
                    normalized_dep = dep.strip()
                    if normalized_dep == module_id:
                        errors.append(
                            build_error(
                                ERROR_DEPENDS_SELF_REFERENCE,
                                f"Module '{module_id}' version '{version}' depends on itself "
                                f"(package: '{package_name}').",
                            )
                        )
                        continue
                    if normalized_dep in seen_deps:
                        errors.append(
                            build_error(
                                ERROR_DEPENDS_DUPLICATE,
                                f"Module '{module_id}' version '{version}' has duplicate depends "
                                f"entry '{normalized_dep}' (package: '{package_name}').",
                            )
                        )
                        continue
                    seen_deps.add(normalized_dep)
                    if normalized_dep not in known_modules:
                        errors.append(
                            build_error(
                                ERROR_DEPENDS_BROKEN_LINK,
                                f"Module '{module_id}' version '{version}' depends on "
                                f"unknown module '{normalized_dep}' (package: '{package_name}').",
                            )
                        )

    if errors:
        details = "\n".join(f"  - {err}" for err in errors)
        raise RuntimeError("Runtime contract validation failed:\n" + details)


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
    trust = entry.get("trust")
    
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
        validate_module_name(choysum_meta, module_id, package_name, ver)

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
        verify_tarball_integrity(tarball_url, integrity, package_name, ver)

        normalized_cli_range, cli_major = resolve_choysum_cli_range(
            choysum_meta=choysum_meta,
            module_id=module_id,
            package_name=package_name,
            version=ver,
        )
        validate_official_pre1_cli_range(
            module_id=module_id,
            package_name=package_name,
            version=ver,
            trust=trust,
            normalized_cli_range=normalized_cli_range,
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
        "trust": trust,
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
    validate_runtime_contracts(modules)
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
