"""Versioned, non-sensitive local settings."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from amadeus_desktop.provider_config import (
    MIMO_SPEECH_CREDENTIAL_REF,
    MULTIMODAL_CREDENTIAL_REF,
    PROVIDER_CREDENTIAL_REF,
    ProviderConfig,
    ProviderConfigError,
    ProviderPreset,
)

CURRENT_SCHEMA_VERSION = 9

_SAFE_PET_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": CURRENT_SCHEMA_VERSION,
    "ui": {
        "language": "zh-CN",
    },
    "general": {
        "always_on_top": True,
        "launch_at_login": False,
    },
    "pet": {
        "active_pet_id": "builtin-amadeus",
        "scale_percent": 100,
        "animation_speed_percent": 100,
        "position": None,
    },
    "provider_enabled": False,
    "provider": ProviderConfig.default().to_mapping(),
    "multimodal": {
        "enabled": False,
        "reuse_mimo_credential": False,
        "provider": ProviderConfig.for_preset(
            ProviderPreset.MIMO_PAYG,
            credential_ref=MULTIMODAL_CREDENTIAL_REF,
        ).to_mapping(),
    },
    "voice": {
        "enabled": False,
        "input_device_id": "",
        "output_device_id": "",
        "hands_free_enabled": False,
        "base_url": "https://api.xiaomimimo.com/v1",
        "asr_model": "mimo-v2.5-asr",
        "tts_model": "mimo-v2.5-tts",
        "tts_voice": "mimo_default",
        "tts_format": "wav",
        "connect_timeout_seconds": 15,
        "request_timeout_seconds": 90,
        "credential_ref": MIMO_SPEECH_CREDENTIAL_REF,
        "credential_source": "independent",
    },
    "visual": {
        "active_vision_enabled": False,
        "latest_frame_fps": 1,
        "preferred_source": "screen",
        "screen_id": "",
        "window_id": "",
        "camera_id": "",
    },
    "memory": {
        "enabled": True,
        "deep_memory_enabled": True,
    },
    "persona": {
        "follow_user_language": True,
    },
    "proactive": {
        "mode": "restrained",
        "quiet_start_minute": 23 * 60,
        "quiet_end_minute": 8 * 60,
        "daily_limit": 2,
        "paused_local_date": None,
        "ai_greetings_enabled": False,
    },
}

_TOP_LEVEL_FIELDS = frozenset(DEFAULT_SETTINGS)
_UI_FIELDS = frozenset(DEFAULT_SETTINGS["ui"])
_GENERAL_FIELDS = frozenset(DEFAULT_SETTINGS["general"])
_PET_FIELDS = frozenset(DEFAULT_SETTINGS["pet"])
_MEMORY_FIELDS = frozenset(DEFAULT_SETTINGS["memory"])
_PERSONA_FIELDS = frozenset(DEFAULT_SETTINGS["persona"])
_PROACTIVE_FIELDS = frozenset(DEFAULT_SETTINGS["proactive"])
_MULTIMODAL_FIELDS = frozenset(DEFAULT_SETTINGS["multimodal"])
_VOICE_FIELDS = frozenset(DEFAULT_SETTINGS["voice"])
_VISUAL_FIELDS = frozenset(DEFAULT_SETTINGS["visual"])
_PROACTIVE_MODES = frozenset({"restrained", "startup_only", "off"})

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


@dataclass(frozen=True, slots=True)
class SettingsFileSnapshot:
    """Opaque pre-transaction bytes used only for exact atomic rollback."""

    existed: bool
    content: bytes | None


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


def _migrate_v1_to_v2(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 2
    migrated.setdefault("pet", deepcopy(DEFAULT_SETTINGS["pet"]))
    return migrated


def _migrate_v2_to_v3(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 3
    migrated.setdefault("provider_enabled", False)
    migrated.setdefault("provider", deepcopy(DEFAULT_SETTINGS["provider"]))
    return migrated


def _migrate_v3_to_v4(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 4
    migrated.setdefault("memory", deepcopy(DEFAULT_SETTINGS["memory"]))
    return migrated


def _migrate_v4_to_v5(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 5

    pet = migrated.get("pet")
    if not isinstance(pet, dict):
        raise InvalidSettingsError("The pet settings section must be an object.")
    pet.setdefault(
        "animation_speed_percent",
        DEFAULT_SETTINGS["pet"]["animation_speed_percent"],
    )

    for section in ("general", "persona", "proactive"):
        defaults = DEFAULT_SETTINGS[section]
        existing = migrated.setdefault(section, deepcopy(defaults))
        if not isinstance(existing, dict):
            raise InvalidSettingsError(f"The {section} settings section must be an object.")
        for key, value in defaults.items():
            existing.setdefault(key, deepcopy(value))
    return migrated


def _migrate_v5_to_v6(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 6
    migrated.setdefault("multimodal", deepcopy(DEFAULT_SETTINGS["multimodal"]))
    return migrated


def _migrate_v6_to_v7(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 7
    migrated.setdefault("voice", deepcopy(DEFAULT_SETTINGS["voice"]))
    return migrated


def _migrate_v7_to_v8(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 8
    migrated.setdefault("visual", deepcopy(DEFAULT_SETTINGS["visual"]))
    return migrated


def _migrate_v8_to_v9(source: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(source)
    migrated["schema_version"] = 9
    memory = migrated.setdefault("memory", deepcopy(DEFAULT_SETTINGS["memory"]))
    if not isinstance(memory, dict):
        raise InvalidSettingsError("The memory settings section must be an object.")
    memory.setdefault("deep_memory_enabled", True)
    return migrated


_MIGRATIONS: Mapping[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _migrate_v0_to_v1,
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
    7: _migrate_v7_to_v8,
    8: _migrate_v8_to_v9,
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

        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            raise SettingsError("Settings could not be saved atomically.") from exc

    def capture_snapshot(self) -> SettingsFileSnapshot:
        """Capture the exact current file without parsing or normalizing it."""

        try:
            if not self.path.exists():
                return SettingsFileSnapshot(False, None)
            return SettingsFileSnapshot(True, self.path.read_bytes())
        except OSError as exc:
            raise SettingsError("Settings could not be snapshotted safely.") from exc

    def restore_snapshot(self, snapshot: SettingsFileSnapshot) -> None:
        """Restore exact pre-transaction bytes using an atomic replacement."""

        if not isinstance(snapshot, SettingsFileSnapshot):
            raise SettingsError("Settings snapshot is invalid.")
        if not snapshot.existed:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                raise SettingsError("Settings snapshot could not be restored.") from exc
            return
        if snapshot.content is None:
            raise SettingsError("Settings snapshot is incomplete.")

        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".rollback.tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(snapshot.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        except OSError as exc:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            raise SettingsError("Settings snapshot could not be restored.") from exc

    def load_provider_config(self) -> ProviderConfig:
        """Load the active non-sensitive provider configuration."""

        return ProviderConfig.from_mapping(self.load()["provider"])

    def save_provider_config(self, config: ProviderConfig) -> dict[str, Any]:
        """Atomically replace only the active provider configuration."""

        settings = self.load_or_create()
        settings["provider"] = config.validated().to_mapping()
        self.save(settings)
        return settings

    @classmethod
    def _validate(cls, settings: Mapping[str, Any]) -> None:
        version = settings.get("schema_version")
        if version != CURRENT_SCHEMA_VERSION:
            raise InvalidSettingsError(
                f"Settings must use schema version {CURRENT_SCHEMA_VERSION}."
            )
        cls._reject_sensitive_keys(settings)
        cls._require_exact_fields(settings, _TOP_LEVEL_FIELDS, "The settings document")

        ui = settings.get("ui")
        if not isinstance(ui, Mapping):
            raise InvalidSettingsError("The ui settings section must be an object.")
        cls._require_exact_fields(ui, _UI_FIELDS, "The ui settings section")
        language = ui.get("language")
        if not isinstance(language, str) or not language.strip():
            raise InvalidSettingsError("ui.language must be a non-empty string.")

        general = settings.get("general")
        if not isinstance(general, Mapping):
            raise InvalidSettingsError("The general settings section must be an object.")
        cls._require_exact_fields(general, _GENERAL_FIELDS, "The general settings section")
        for key in _GENERAL_FIELDS:
            if not isinstance(general.get(key), bool):
                raise InvalidSettingsError(f"general.{key} must be a boolean.")

        pet = settings.get("pet")
        if not isinstance(pet, Mapping):
            raise InvalidSettingsError("The pet settings section must be an object.")
        cls._require_exact_fields(pet, _PET_FIELDS, "The pet settings section")
        pet_id = pet.get("active_pet_id")
        if not isinstance(pet_id, str) or _SAFE_PET_ID.fullmatch(pet_id) is None:
            raise InvalidSettingsError("pet.active_pet_id is invalid.")
        scale = pet.get("scale_percent")
        if isinstance(scale, bool) or not isinstance(scale, int) or not 50 <= scale <= 200:
            raise InvalidSettingsError("pet.scale_percent must be between 50 and 200.")
        animation_speed = pet.get("animation_speed_percent")
        if (
            isinstance(animation_speed, bool)
            or not isinstance(animation_speed, int)
            or not 50 <= animation_speed <= 200
        ):
            raise InvalidSettingsError("pet.animation_speed_percent must be between 50 and 200.")
        position = pet.get("position")
        if position is not None:
            cls._validate_pet_position(position)

        try:
            provider_config = ProviderConfig.from_mapping(settings.get("provider"))
        except ProviderConfigError as exc:
            raise InvalidSettingsError("The provider settings section is invalid.") from exc
        if provider_config.credential_ref != PROVIDER_CREDENTIAL_REF:
            raise InvalidSettingsError("The provider credential reference is invalid.")
        if not isinstance(settings.get("provider_enabled"), bool):
            raise InvalidSettingsError("provider_enabled must be a boolean.")

        multimodal = settings.get("multimodal")
        if not isinstance(multimodal, Mapping):
            raise InvalidSettingsError("The multimodal settings section must be an object.")
        cls._require_exact_fields(
            multimodal,
            _MULTIMODAL_FIELDS,
            "The multimodal settings section",
        )
        if not isinstance(multimodal.get("enabled"), bool) or not isinstance(
            multimodal.get("reuse_mimo_credential"), bool
        ):
            raise InvalidSettingsError("multimodal flags must be booleans.")
        try:
            multimodal_provider = ProviderConfig.from_mapping(multimodal.get("provider"))
        except ProviderConfigError as exc:
            raise InvalidSettingsError("The multimodal provider settings are invalid.") from exc
        if multimodal_provider.credential_ref != MULTIMODAL_CREDENTIAL_REF:
            raise InvalidSettingsError("The multimodal credential reference is invalid.")
        if multimodal.get("reuse_mimo_credential") is True and not (
            _is_mimo_payg(provider_config)
            and _is_mimo_payg(multimodal_provider)
            and provider_config.credential_scope == multimodal_provider.credential_scope
        ):
            raise InvalidSettingsError("The multimodal credential reuse scope is invalid.")

        voice = settings.get("voice")
        if not isinstance(voice, Mapping):
            raise InvalidSettingsError("The voice settings section must be an object.")
        cls._require_exact_fields(voice, _VOICE_FIELDS, "The voice settings section")
        for key in ("input_device_id", "output_device_id"):
            value = voice.get(key)
            if not isinstance(value, str) or len(value) > 512 or "\x00" in value:
                raise InvalidSettingsError(f"voice.{key} is invalid.")
        if not isinstance(voice.get("enabled"), bool) or not isinstance(
            voice.get("hands_free_enabled"), bool
        ):
            raise InvalidSettingsError("voice flags must be booleans.")
        required_voice_values = {
            "asr_model": "mimo-v2.5-asr",
            "tts_model": "mimo-v2.5-tts",
            "tts_voice": "mimo_default",
            "tts_format": "wav",
            "base_url": "https://api.xiaomimimo.com/v1",
            "credential_ref": MIMO_SPEECH_CREDENTIAL_REF,
        }
        if any(voice.get(key) != value for key, value in required_voice_values.items()):
            raise InvalidSettingsError("The MiMo voice contract is invalid.")
        if voice.get("credential_source") not in {
            "independent",
            PROVIDER_CREDENTIAL_REF,
            MULTIMODAL_CREDENTIAL_REF,
        }:
            raise InvalidSettingsError("voice.credential_source is invalid.")
        if voice.get("enabled") is True:
            source = voice.get("credential_source")
            if source == PROVIDER_CREDENTIAL_REF and not _is_mimo_payg(provider_config):
                raise InvalidSettingsError("Voice cannot reuse the text provider credential.")
            if source == MULTIMODAL_CREDENTIAL_REF and not _is_mimo_payg(multimodal_provider):
                raise InvalidSettingsError("Voice cannot reuse the multimodal credential.")
        for key in ("connect_timeout_seconds", "request_timeout_seconds"):
            value = voice.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 300:
                raise InvalidSettingsError(f"voice.{key} is invalid.")

        visual = settings.get("visual")
        if not isinstance(visual, Mapping):
            raise InvalidSettingsError("The visual settings section must be an object.")
        cls._require_exact_fields(visual, _VISUAL_FIELDS, "The visual settings section")
        if not isinstance(visual.get("active_vision_enabled"), bool):
            raise InvalidSettingsError("visual.active_vision_enabled must be a boolean.")
        if visual.get("latest_frame_fps") != 1:
            raise InvalidSettingsError("visual.latest_frame_fps must remain 1.")
        if visual.get("preferred_source") not in {"screen", "window", "camera"}:
            raise InvalidSettingsError("visual.preferred_source is invalid.")
        for key in ("screen_id", "window_id", "camera_id"):
            value = visual.get(key)
            if not isinstance(value, str) or len(value) > 512 or "\x00" in value:
                raise InvalidSettingsError(f"visual.{key} is invalid.")
        window_id = visual.get("window_id")
        if window_id and (not str(window_id).isdigit() or len(str(window_id)) > 32):
            raise InvalidSettingsError("visual.window_id is invalid.")

        memory = settings.get("memory")
        if not isinstance(memory, Mapping):
            raise InvalidSettingsError("The memory settings section must be an object.")
        cls._require_exact_fields(memory, _MEMORY_FIELDS, "The memory settings section")
        if not isinstance(memory.get("enabled"), bool):
            raise InvalidSettingsError("memory.enabled must be a boolean.")
        if not isinstance(memory.get("deep_memory_enabled"), bool):
            raise InvalidSettingsError("memory.deep_memory_enabled must be a boolean.")

        persona = settings.get("persona")
        if not isinstance(persona, Mapping):
            raise InvalidSettingsError("The persona settings section must be an object.")
        cls._require_exact_fields(persona, _PERSONA_FIELDS, "The persona settings section")
        if not isinstance(persona.get("follow_user_language"), bool):
            raise InvalidSettingsError("persona.follow_user_language must be a boolean.")

        proactive = settings.get("proactive")
        if not isinstance(proactive, Mapping):
            raise InvalidSettingsError("The proactive settings section must be an object.")
        cls._require_exact_fields(proactive, _PROACTIVE_FIELDS, "The proactive settings section")
        mode = proactive.get("mode")
        if not isinstance(mode, str) or mode not in _PROACTIVE_MODES:
            raise InvalidSettingsError("proactive.mode is invalid.")
        for key in ("quiet_start_minute", "quiet_end_minute"):
            value = proactive.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1439:
                raise InvalidSettingsError(f"proactive.{key} must be between 0 and 1439.")
        daily_limit = proactive.get("daily_limit")
        if (
            isinstance(daily_limit, bool)
            or not isinstance(daily_limit, int)
            or not 1 <= daily_limit <= 2
        ):
            raise InvalidSettingsError("proactive.daily_limit must be between 1 and 2.")
        paused_local_date = proactive.get("paused_local_date")
        if paused_local_date is not None:
            cls._validate_local_date(paused_local_date)
        if not isinstance(proactive.get("ai_greetings_enabled"), bool):
            raise InvalidSettingsError("proactive.ai_greetings_enabled must be a boolean.")

    @staticmethod
    def _require_exact_fields(
        section: Mapping[str, Any],
        expected: frozenset[str],
        description: str,
    ) -> None:
        if set(section) != expected:
            raise InvalidSettingsError(f"{description} contains unsupported or missing fields.")

    @staticmethod
    def _validate_local_date(value: Any) -> None:
        if not isinstance(value, str) or len(value) != 10:
            raise InvalidSettingsError("proactive.paused_local_date must use YYYY-MM-DD.")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidSettingsError("proactive.paused_local_date must use YYYY-MM-DD.") from exc
        if parsed.isoformat() != value:
            raise InvalidSettingsError("proactive.paused_local_date must use YYYY-MM-DD.")

    @staticmethod
    def _validate_pet_position(position: Any) -> None:
        if not isinstance(position, Mapping):
            raise InvalidSettingsError("pet.position must be null or an object.")
        if set(position) != {"screen_id", "x_ratio", "y_ratio"}:
            raise InvalidSettingsError("pet.position contains unsupported fields.")
        screen_id = position.get("screen_id")
        if not isinstance(screen_id, str) or not screen_id.strip():
            raise InvalidSettingsError("pet.position.screen_id must be a non-empty string.")
        for key in ("x_ratio", "y_ratio"):
            value = position.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidSettingsError(f"pet.position.{key} must be numeric.")
            if not 0.0 <= float(value) <= 1.0:
                raise InvalidSettingsError(f"pet.position.{key} must be between 0 and 1.")

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


def validate_settings_document(settings: Mapping[str, Any]) -> None:
    """Validate one current settings document without migrating or writing it."""

    if not isinstance(settings, Mapping):
        raise InvalidSettingsError("Settings must be a JSON object.")
    SettingsRepository._validate(settings)


def _is_mimo_payg(config: ProviderConfig) -> bool:
    return bool(
        config.preset is ProviderPreset.MIMO_PAYG
        and config.base_url == "https://api.xiaomimimo.com/v1"
        and config.auth_mode.value == "api_key"
    )
