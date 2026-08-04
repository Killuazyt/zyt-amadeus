"""Strict, deterministic metadata for packaged builds and diagnostics."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from amadeus_desktop import __version__

BUILD_INFO_SCHEMA_VERSION = 1
BUILD_INFO_RESOURCE = Path("amadeus_desktop/resources/build-info.json")
DEVELOPMENT_VALUE = "development"
_MAX_BUILD_INFO_BYTES = 4_096
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class BuildInfoError(RuntimeError):
    """Raised when frozen-build identity cannot be established safely."""


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Content-free build identity safe for diagnostics and logs."""

    schema_version: int
    version: str
    commit_sha: str
    build_date_utc: str


def development_build_info() -> BuildInfo:
    """Return an explicit source-tree fallback without using wall-clock state."""

    return BuildInfo(
        schema_version=BUILD_INFO_SCHEMA_VERSION,
        version=__version__,
        commit_sha=DEVELOPMENT_VALUE,
        build_date_utc=DEVELOPMENT_VALUE,
    )


def load_build_info(
    *,
    frozen: bool | None = None,
    resource_path: Path | None = None,
) -> BuildInfo:
    """Load and strictly validate the packaged build-info manifest.

    Frozen applications fail closed when the manifest is missing or malformed.
    A source checkout without a generated manifest uses a visible deterministic
    development marker rather than inventing a commit or build date.
    """

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    candidate = resource_path or _default_resource_path(is_frozen)
    try:
        raw = candidate.read_bytes()
    except FileNotFoundError:
        if not is_frozen and resource_path is None:
            return development_build_info()
        raise BuildInfoError("Build information is unavailable.") from None
    except OSError:
        raise BuildInfoError("Build information could not be read safely.") from None
    if not raw or len(raw) > _MAX_BUILD_INFO_BYTES:
        raise BuildInfoError("Build information has an invalid size.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BuildInfoError("Build information is not valid UTF-8 JSON.") from None
    return _validate_build_info(payload)


def _default_resource_path(frozen: bool) -> Path:
    if frozen:
        bundle_root = getattr(sys, "_MEIPASS", None)
        if not isinstance(bundle_root, str) or not bundle_root:
            raise BuildInfoError("Frozen build resources are unavailable.")
        return Path(bundle_root) / BUILD_INFO_RESOURCE
    return Path(__file__).resolve().parent / "resources" / "build-info.json"


def _validate_build_info(payload: object) -> BuildInfo:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "version",
        "commit_sha",
        "build_date_utc",
    }:
        raise BuildInfoError("Build information fields are invalid.")
    schema_version = payload["schema_version"]
    version = payload["version"]
    commit_sha = payload["commit_sha"]
    build_date_utc = payload["build_date_utc"]
    if schema_version != BUILD_INFO_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise BuildInfoError("Build information schema is unsupported.")
    if version != __version__:
        raise BuildInfoError("Build information version does not match the application.")
    if not isinstance(commit_sha, str) or _COMMIT_SHA_PATTERN.fullmatch(commit_sha) is None:
        raise BuildInfoError("Build information commit is invalid.")
    if not isinstance(build_date_utc, str) or not _is_iso_date(build_date_utc):
        raise BuildInfoError("Build information date is invalid.")
    return BuildInfo(
        schema_version=schema_version,
        version=version,
        commit_sha=commit_sha,
        build_date_utc=build_date_utc,
    )


def _is_iso_date(value: str) -> bool:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
