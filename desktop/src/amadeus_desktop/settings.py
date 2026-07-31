"""Versioned, non-sensitive local settings."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "ui": {
        "language": "zh-CN",
    },
}

_FORBIDDEN_SETTING_KEYS = {
    "api-key",
    "api_key",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}
_FORBIDDEN_SETTING_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_password",
    "_secret",
    "_token",
)


class SettingsError(RuntimeError):
    """Base class for settings failures that must preserve the source file."""


class InvalidSettingsError(SettingsError):
    """Raised when settings are malformed or contain forbidden material."""


class UnsupportedSettingsVersionError(SettingsError):
    """Raised when settings were written by a newer application version."""


def _migrate_v0_to_v1(source: dict[str, Any]) -> dict[str, Any]:
    language = source.get("language", "zh-CN")
    migrated = deepcopy(source)
    migrated.pop("language", None)
    migrated["schema_version"] = 1
    migrated.setdefault("ui", {})
    if not isinstance(migrated["ui"], dict):
        raise InvalidSettingsError("The ui settings section must be an object.")
    migrated["ui"].setdefault("language", language)
    return migrated


_MIGRATIONS: Mapping[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _migrate_v0_to_v1,
}


class SettingsRepository:
    """Load, migrate, validate, and atomically save JSON settings."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_or_create(self) -> dict[str, Any]:
        if not self.path.exists():
            settings = deepcopy(DEFAULT_SETTINGS)
            self.save(settings)
            return settings
        return self.load()

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidSettingsError("Settings could not be decoded.") from exc

        if not isinstance(loaded, dict):
            raise InvalidSettingsError("Settings must be a JSON object.")

        version = loaded.get("schema_version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise InvalidSettingsError("schema_version must be a non-negative integer.")
        if version > CURRENT_SCHEMA_VERSION:
            raise UnsupportedSettingsVersionError(
                f"Settings schema {version} is newer than {CURRENT_SCHEMA_VERSION}."
            )

        settings = deepcopy(loaded)
        original_version = version
        while version < CURRENT_SCHEMA_VERSION:
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise InvalidSettingsError(f"No migration exists for schema {version}.")
            settings = migration(settings)
            version = settings.get("schema_version")
            if not isinstance(version, int):
                raise InvalidSettingsError("A migration produced an invalid schema version.")

        self._validate(settings)
        if original_version != CURRENT_SCHEMA_VERSION:
            self.save(settings)
        return settings

    def save(self, settings: Mapping[str, Any]) -> None:
        document = deepcopy(dict(settings))
        self._validate(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise SettingsError("Settings could not be saved atomically.") from exc

    @classmethod
    def _validate(cls, settings: Mapping[str, Any]) -> None:
        version = settings.get("schema_version")
        if version != CURRENT_SCHEMA_VERSION:
            raise InvalidSettingsError(
                f"Settings must use schema version {CURRENT_SCHEMA_VERSION}."
            )
        cls._reject_sensitive_keys(settings)

        ui = settings.get("ui")
        if not isinstance(ui, Mapping):
            raise InvalidSettingsError("The ui settings section must be an object.")
        language = ui.get("language")
        if not isinstance(language, str) or not language.strip():
            raise InvalidSettingsError("ui.language must be a non-empty string.")

    @classmethod
    def _reject_sensitive_keys(cls, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).strip().lower()
                normalized_identifier = normalized.replace("-", "_")
                if normalized in _FORBIDDEN_SETTING_KEYS or normalized_identifier.endswith(
                    _FORBIDDEN_SETTING_SUFFIXES
                ):
                    raise InvalidSettingsError("Sensitive values are forbidden in settings.")
                cls._reject_sensitive_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                cls._reject_sensitive_keys(nested)
