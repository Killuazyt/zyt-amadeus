from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from amadeus_desktop.provider_config import (
    MULTIMODAL_CREDENTIAL_REF,
    PROVIDER_CREDENTIAL_REF,
    ProviderConfig,
    ProviderPreset,
)
from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_SETTINGS,
    InvalidSettingsError,
    SettingsError,
    SettingsRepository,
    UnsupportedSettingsVersionError,
    validate_settings_document,
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


def test_save_wraps_parent_directory_failure_as_settings_error(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "config"
    blocked_parent.write_text("not-a-directory", encoding="utf-8")

    with pytest.raises(SettingsError, match="saved atomically"):
        SettingsRepository(blocked_parent / "settings.json").save(DEFAULT_SETTINGS)


def test_save_cleanup_failure_does_not_mask_settings_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = SettingsRepository(tmp_path / "settings.json")
    original_unlink = Path.unlink

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("synthetic replace failure")

    def fail_temporary_cleanup(path: Path, *args, **kwargs) -> None:
        if path.name.startswith(".settings.json.") and path.name.endswith(".tmp"):
            raise OSError("synthetic cleanup failure")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("amadeus_desktop.settings.os.replace", fail_replace)
        patch.setattr(Path, "unlink", fail_temporary_cleanup)
        with pytest.raises(SettingsError, match="saved atomically"):
            repository.save(DEFAULT_SETTINGS)

    for temporary in tmp_path.glob(".settings.json.*.tmp"):
        temporary.unlink()


def test_unversioned_settings_migrate_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"language": "en-US"}', encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert loaded["ui"]["language"] == "en-US"
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_schema_v8_to_v9_enables_deep_memory_without_changing_total_switch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    legacy = deepcopy(DEFAULT_SETTINGS)
    legacy["schema_version"] = 8
    legacy["memory"] = {"enabled": False}
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == 9
    assert loaded["memory"] == {
        "enabled": False,
        "deep_memory_enabled": True,
    }
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_schema_v1_migrates_to_pet_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"schema_version": 1, "ui": {"language": "zh-CN"}}',
        encoding="utf-8",
    )

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert loaded["pet"] == DEFAULT_SETTINGS["pet"]


def test_schema_v2_migrates_to_deepseek_provider_default(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "ui": {"language": "zh-CN"},
                "pet": DEFAULT_SETTINGS["pet"],
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert ProviderConfig.from_mapping(loaded["provider"]).preset is ProviderPreset.DEEPSEEK_PAYG
    assert loaded["provider_enabled"] is False
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_schema_v4_migrates_through_p7e_defaults_without_losing_existing_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    provider = ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG, model="mimo-v2.5-pro")
    legacy = {
        "schema_version": 4,
        "ui": {"language": "en-US"},
        "pet": {
            "active_pet_id": "local-pet",
            "scale_percent": 125,
            "position": {
                "screen_id": "serial:test",
                "x_ratio": 0.25,
                "y_ratio": 0.75,
            },
        },
        "provider_enabled": True,
        "provider": provider.to_mapping(),
        "memory": {"enabled": False},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert loaded["ui"] == legacy["ui"]
    assert loaded["pet"] == {
        **legacy["pet"],
        "animation_speed_percent": 100,
    }
    assert loaded["provider_enabled"] is True
    assert loaded["provider"] == legacy["provider"]
    assert loaded["memory"] == {"enabled": False, "deep_memory_enabled": True}
    assert loaded["general"] == DEFAULT_SETTINGS["general"]
    assert loaded["persona"] == DEFAULT_SETTINGS["persona"]
    assert loaded["proactive"] == DEFAULT_SETTINGS["proactive"]
    assert loaded["multimodal"] == DEFAULT_SETTINGS["multimodal"]
    assert loaded["voice"] == DEFAULT_SETTINGS["voice"]
    assert loaded["visual"] == DEFAULT_SETTINGS["visual"]
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_schema_v4_preserves_already_present_p6_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    legacy = deepcopy(DEFAULT_SETTINGS)
    legacy["schema_version"] = 4
    legacy["general"] = {"always_on_top": False, "launch_at_login": True}
    legacy["pet"]["animation_speed_percent"] = 175
    legacy["persona"]["follow_user_language"] = False
    legacy["proactive"] = {
        "mode": "startup_only",
        "quiet_start_minute": 60,
        "quiet_end_minute": 120,
        "daily_limit": 1,
        "paused_local_date": "2026-08-03",
        "ai_greetings_enabled": True,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["general"] == legacy["general"]
    assert loaded["pet"] == legacy["pet"]
    assert loaded["persona"] == legacy["persona"]
    assert loaded["proactive"] == legacy["proactive"]


@pytest.mark.parametrize(
    ("version", "existing_section", "defaulted_sections"),
    (
        (5, None, ("multimodal", "voice", "visual")),
        (6, "multimodal", ("voice", "visual")),
        (7, "voice", ("visual",)),
    ),
)
def test_p7c_to_p7e_settings_migrations_are_missing_only_and_persisted(
    tmp_path: Path,
    version: int,
    existing_section: str | None,
    defaulted_sections: tuple[str, ...],
) -> None:
    path = tmp_path / "settings.json"
    legacy = deepcopy(DEFAULT_SETTINGS)
    legacy["schema_version"] = version
    for section in ("multimodal", "voice", "visual"):
        if section in defaulted_sections:
            legacy.pop(section)
    if existing_section == "multimodal":
        legacy["multimodal"]["enabled"] = True
    elif existing_section == "voice":
        legacy["voice"]["input_device_id"] = "saved-microphone"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = SettingsRepository(path).load()

    assert loaded["schema_version"] == CURRENT_SCHEMA_VERSION
    for section in defaulted_sections:
        assert loaded[section] == DEFAULT_SETTINGS[section]
    if existing_section is not None:
        assert loaded[existing_section] == legacy[existing_section]
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_mimo_credential_reuse_requires_same_payg_security_scope() -> None:
    invalid = deepcopy(DEFAULT_SETTINGS)
    invalid["multimodal"]["reuse_mimo_credential"] = True
    with pytest.raises(InvalidSettingsError, match="reuse scope"):
        validate_settings_document(invalid)

    valid = deepcopy(DEFAULT_SETTINGS)
    valid["provider"] = ProviderConfig.for_preset(
        ProviderPreset.MIMO_PAYG,
        credential_ref=PROVIDER_CREDENTIAL_REF,
    ).to_mapping()
    valid["multimodal"]["provider"] = ProviderConfig.for_preset(
        ProviderPreset.MIMO_PAYG,
        credential_ref=MULTIMODAL_CREDENTIAL_REF,
    ).to_mapping()
    valid["multimodal"]["reuse_mimo_credential"] = True

    validate_settings_document(valid)


def test_enabled_voice_rejects_incompatible_reused_provider_credential() -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["voice"]["enabled"] = True
    document["voice"]["credential_source"] = PROVIDER_CREDENTIAL_REF

    with pytest.raises(InvalidSettingsError, match="Voice cannot reuse"):
        validate_settings_document(document)


@pytest.mark.parametrize("scale", [49, 201, True, "100"])
def test_invalid_pet_scale_is_rejected(tmp_path: Path, scale: object) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["pet"]["scale_percent"] = scale

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


@pytest.mark.parametrize("speed", [49, 201, True, 100.0, "100"])
def test_invalid_pet_animation_speed_is_rejected(tmp_path: Path, speed: object) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["pet"]["animation_speed_percent"] = speed

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("general", "always_on_top", 1),
        ("general", "launch_at_login", "false"),
        ("persona", "follow_user_language", 1),
        ("proactive", "mode", "frequent"),
        ("proactive", "mode", 1),
        ("proactive", "quiet_start_minute", -1),
        ("proactive", "quiet_start_minute", 1440),
        ("proactive", "quiet_end_minute", True),
        ("proactive", "quiet_end_minute", 480.0),
        ("proactive", "daily_limit", 0),
        ("proactive", "daily_limit", 3),
        ("proactive", "daily_limit", True),
        ("proactive", "ai_greetings_enabled", 0),
    ],
)
def test_invalid_p6_setting_values_are_rejected(
    tmp_path: Path,
    section: str,
    key: str,
    value: object,
) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document[section][key] = value

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


@pytest.mark.parametrize(
    "value",
    ["", "2026-8-03", "2026-02-30", "2026/08/03", 20260803, False],
)
def test_invalid_paused_local_date_is_rejected(tmp_path: Path, value: object) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["proactive"]["paused_local_date"] = value

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


@pytest.mark.parametrize("mode", ["restrained", "startup_only", "off"])
def test_each_proactive_mode_round_trips(tmp_path: Path, mode: str) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["proactive"]["mode"] = mode
    document["proactive"]["paused_local_date"] = "2026-08-03"
    repository = SettingsRepository(tmp_path / "settings.json")

    repository.save(document)

    assert repository.load() == document


@pytest.mark.parametrize(
    "section", [None, "ui", "general", "pet", "memory", "persona", "proactive"]
)
def test_unknown_fields_are_rejected(tmp_path: Path, section: str | None) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    target = document if section is None else document[section]
    target["unsupported"] = True

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


@pytest.mark.parametrize("section", ["ui", "general", "pet", "memory", "persona", "proactive"])
def test_missing_fields_are_rejected(tmp_path: Path, section: str) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document[section].pop(next(iter(document[section])))

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


def test_public_document_validation_is_pure_and_requires_current_schema(tmp_path: Path) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    path = tmp_path / "settings.json"

    validate_settings_document(document)

    assert not path.exists()
    legacy = deepcopy(document)
    legacy["schema_version"] = 4
    with pytest.raises(InvalidSettingsError):
        validate_settings_document(legacy)


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


def test_snapshot_restores_exact_unparsed_bytes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = b'{"schema_version":99,"future":"\xff"}'
    path.write_bytes(original)
    repository = SettingsRepository(path)
    snapshot = repository.capture_snapshot()
    path.write_bytes(b'{"schema_version":3}')

    repository.restore_snapshot(snapshot)

    assert path.read_bytes() == original


def test_snapshot_restore_wraps_parent_directory_failure_as_settings_error(
    tmp_path: Path,
) -> None:
    source = SettingsRepository(tmp_path / "source" / "settings.json")
    source.save(DEFAULT_SETTINGS)
    snapshot = source.capture_snapshot()
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not-a-directory", encoding="utf-8")

    with pytest.raises(SettingsError, match="snapshot could not be restored"):
        SettingsRepository(blocked_parent / "settings.json").restore_snapshot(snapshot)


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

    SettingsRepository(tmp_path / "settings.json").save(document)


def test_arbitrary_credential_reference_is_rejected(tmp_path: Path) -> None:
    document = deepcopy(DEFAULT_SETTINGS)
    document["provider"]["credential_ref"] = "user-controlled-target"

    with pytest.raises(InvalidSettingsError):
        SettingsRepository(tmp_path / "settings.json").save(document)


def test_provider_config_convenience_methods_round_trip(tmp_path: Path) -> None:
    repository = SettingsRepository(tmp_path / "settings.json")
    config = ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG, model="mimo-v2.5-pro")

    saved = repository.save_provider_config(config)

    assert saved["provider"] == config.to_mapping()
    assert repository.load_provider_config() == config
