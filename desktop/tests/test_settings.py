from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_SETTINGS,
    InvalidSettingsError,
    SettingsRepository,
    UnsupportedSettingsVersionError,
)


def test_missing_settings_are_created_with_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.json"
    repository = SettingsRepository(path)

    loaded = repository.load_or_create()

    assert loaded == DEFAULT_SETTINGS
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULT_SETTINGS


def test_save_is_atomic_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    repository = SettingsRepository(path)

    repository.save(DEFAULT_SETTINGS)

    assert repository.load() == DEFAULT_SETTINGS
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


def test_unversioned_settings_migrate_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"language": "en-US"}', encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert loaded["ui"]["language"] == "en-US"
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_schema_v1_migrates_to_pet_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"schema_version": 1, "ui": {"language": "zh-CN"}}',
        encoding="utf-8",
    )

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == 2
    assert loaded["pet"] == DEFAULT_SETTINGS["pet"]


@pytest.mark.parametrize("scale", [49, 201, True, "100"])
def test_invalid_pet_scale_is_rejected(tmp_path: Path, scale: object) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["pet"]["scale_percent"] = scale

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


def test_valid_pet_position_round_trips(tmp_path: Path) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["pet"]["position"] = {
        "screen_id": "serial:test",
        "x_ratio": 0.75,
        "y_ratio": 1.0,
    }
    repository = SettingsRepository(tmp_path / "settings.json")

    repository.save(document)

    assert repository.load() == document


def test_newer_schema_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = '{"schema_version": 99, "ui": {"language": "zh-CN"}}'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(UnsupportedSettingsVersionError):
        SettingsRepository(path).load_or_create()

    assert path.read_text(encoding="utf-8") == original


def test_corrupt_json_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = "{not-json"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(path).load_or_create()

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "api-key",
        "authorization",
        "key",
        "password",
        "secret",
        "token",
        "LLM_API_KEY",
        "access_token",
        "client_secret",
    ],
)
def test_sensitive_setting_keys_are_rejected(tmp_path: Path, key: str) -> None:
    document = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "ui": {"language": "zh-CN"},
        "provider": {key: "not-a-real-secret"},
    }

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


def test_credential_reference_is_allowed(tmp_path: Path) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["provider"] = {"credential_ref": "windows-credential-manager-id"}

    SettingsRepository(tmp_path / "settings.json").save(document)
