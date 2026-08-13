from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from amadeus_desktop.provider_catalog import (
    CachePolicy,
    ProviderAuth,
    ProviderProtocol,
    ProviderRole,
    load_provider_catalog,
    parse_provider_catalog,
)
from amadeus_desktop.provider_config import PROVIDER_CREDENTIAL_REF
from amadeus_desktop.provider_profiles import (
    ProviderProfile,
    ProviderProfileError,
    ProviderSettings,
    migrate_legacy_provider_settings,
    normalize_provider_base_url,
    request_snapshot,
)
from amadeus_desktop.settings import DEFAULT_SETTINGS

EXPECTED_CATALOG_IDS = {
    "deepseek",
    "mimo_payg",
    "openai",
    "qwen_cn",
    "qwen_intl",
    "gemini",
    "glm",
    "kimi_payg",
    "doubao_ark",
    "minimax_cn",
    "minimax_intl",
    "siliconflow",
    "stepfun",
    "grok",
    "openrouter",
    "anthropic",
    "custom_openai",
    "custom_anthropic",
    "ollama",
    "lm_studio",
    "vllm",
}


def test_bundled_catalog_has_locked_p7g_range_and_no_excluded_ecosystem() -> None:
    catalog = load_provider_catalog()

    assert not catalog.degraded
    assert set(catalog.entries) == EXPECTED_CATALOG_IDS
    serialized = json.dumps(
        {key: value.display_name for key, value in catalog.entries.items()},
        ensure_ascii=False,
    ).casefold()
    for excluded in ("token plan", "kimi code", "realtime", "tts", "asr", "agent"):
        assert excluded not in serialized
    assert catalog.entry("anthropic").protocol is ProviderProtocol.ANTHROPIC_MESSAGES
    assert catalog.entry("qwen_intl").endpoints == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    )
    assert catalog.entry("deepseek").endpoints == ("https://api.deepseek.com",)
    assert all(
        len(entry.endpoints) <= 1 or entry.catalog_id == "qwen_intl" for entry in catalog.all()
    )
    assert catalog.entry("ollama").default_endpoint == "http://localhost:11434/v1"
    assert catalog.entry("lm_studio").default_endpoint == "http://localhost:1234/v1"
    assert catalog.entry("vllm").default_endpoint == "http://localhost:8000/v1"
    assert catalog.entry("custom_openai").model_for(ProviderRole.VISION) == ""
    assert catalog.entry("ollama").model_for(ProviderRole.VISION) == ""
    for entry in catalog.all():
        assert entry.capabilities.native_files is False
        assert entry.capabilities.suggested_context_tokens >= 1
        assert entry.capabilities.suggested_output_tokens >= 1
        assert entry.capabilities.first_chunk_timeout_seconds >= 1
        assert entry.capabilities.idle_timeout_seconds >= 1
        assert entry.custom_models is True


def test_corrupt_catalog_degrades_to_custom_recovery_without_dropping_profiles(
    tmp_path,
) -> None:
    catalog_path = tmp_path / "providers.json"
    catalog_path.write_text("{broken", encoding="utf-8")
    degraded = load_provider_catalog(catalog_path)
    existing = DEFAULT_SETTINGS["model_providers"]

    settings = ProviderSettings.from_mapping(existing, degraded)

    assert degraded.degraded
    assert set(degraded.entries) == {"custom_openai", "custom_anthropic"}
    assert settings.profiles[0].catalog_id == "deepseek"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1"),
        ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
        ("https://EXAMPLE.invalid/v1/", "https://example.invalid/v1"),
    ],
)
def test_endpoint_normalization_accepts_https_or_exact_loopback(value, expected) -> None:
    assert normalize_provider_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://example.invalid/v1",
        "https://user@example.invalid/v1",
        "https://example.invalid/v1?key=value",
        "https://example.invalid/v1#fragment",
        "https://example.invalid/v1/chat/completions",
        "https://%74oken-plan-api.xiaomimimo.com/v1",
        "https://token-plan-api.xiaomimimo.com/v1",
    ],
)
def test_endpoint_normalization_rejects_unsafe_components(value) -> None:
    with pytest.raises(ProviderProfileError):
        normalize_provider_base_url(value)


@pytest.mark.parametrize("catalog_id", ["custom_openai", "custom_anthropic"])
def test_custom_protocols_can_use_no_auth_only_on_exact_loopback(catalog_id) -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry(catalog_id),
        profile_id="custom-local",
    )
    profile = replace(
        profile,
        base_url="http://localhost:11434/v1",
        auth=ProviderAuth.NONE,
    )

    assert profile.auth is ProviderAuth.NONE
    profile.validated(catalog)
    with pytest.raises(ProviderProfileError):
        replace(
            profile,
            base_url="https://example.invalid/v1",
            auth=ProviderAuth.NONE,
        ).validated(catalog)


def test_legacy_credential_slots_cannot_be_attached_to_another_profile_id() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="profile-new")

    with pytest.raises(ProviderProfileError):
        replace(profile, credential_slot="legacy_chat")


def test_models_are_editable_but_builtin_endpoint_protocol_and_auth_are_locked() -> None:
    catalog = load_provider_catalog()
    source = ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="openai-main")
    custom_model = replace(
        source,
        models=MappingProxyType({role: f"editable-{role.value}" for role in ProviderRole}),
    )

    custom_model.validated(catalog)
    with pytest.raises(ProviderProfileError):
        replace(source, base_url="https://example.invalid/v1").validated(catalog)
    with pytest.raises(ProviderProfileError):
        replace(source, auth=ProviderAuth.API_KEY).validated(catalog)


def test_cache_controls_are_only_enabled_for_code_whitelisted_contracts() -> None:
    catalog = load_provider_catalog()
    openai = ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="openai-main")
    qwen = ProviderProfile.from_catalog(catalog.entry("qwen_cn"), profile_id="qwen-main")
    anthropic = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-main"
    )

    with pytest.raises(ProviderProfileError):
        replace(openai, cache_enabled=True).validated(catalog)
    assert replace(qwen, cache_enabled=True).validated(catalog).cache_enabled
    assert replace(anthropic, cache_enabled=True).validated(catalog).cache_enabled
    assert catalog.entry("openai").cache_policy is CachePolicy.UPSTREAM_AUTO


def test_v9_migration_reuses_one_mimo_security_domain_without_reading_a_secret() -> None:
    mimo = DEFAULT_SETTINGS["multimodal"]["provider"]
    legacy = {
        "provider_enabled": True,
        "provider": {**mimo, "credential_ref": PROVIDER_CREDENTIAL_REF},
        "multimodal": {
            **DEFAULT_SETTINGS["multimodal"],
            "enabled": True,
            "reuse_mimo_credential": True,
        },
    }

    migrated = migrate_legacy_provider_settings(legacy)
    assignments = migrated["assignments"]

    assert len(migrated["profiles"]) == 1
    assert set(assignments.values()) == {"legacy-chat"}
    assert migrated["profiles"][0]["credential_slot"] == "legacy_chat"
    assert "secret" not in json.dumps(migrated).casefold()


def test_v9_migration_keeps_independent_visual_credential_slot() -> None:
    legacy = {
        "provider_enabled": True,
        "provider": DEFAULT_SETTINGS["provider"],
        "multimodal": {
            **DEFAULT_SETTINGS["multimodal"],
            "enabled": True,
            "reuse_mimo_credential": False,
        },
    }

    migrated = migrate_legacy_provider_settings(legacy)

    assert len(migrated["profiles"]) == 2
    assert migrated["assignments"]["vision"] == "legacy-vision"
    assert migrated["profiles"][1]["credential_slot"] == "legacy_vision"


def test_disabled_v9_text_configuration_still_migrates_to_assigned_disabled_profile() -> None:
    legacy = {
        "provider_enabled": False,
        "provider": DEFAULT_SETTINGS["provider"],
        "multimodal": DEFAULT_SETTINGS["multimodal"],
    }

    migrated = migrate_legacy_provider_settings(legacy)

    assert migrated["assignments"]["conversation"] == "legacy-chat"
    assert migrated["assignments"]["summary"] == "legacy-chat"
    assert migrated["assignments"]["memory"] == "legacy-chat"
    assert migrated["profiles"][0]["enabled"] is False


def test_request_snapshot_freezes_role_model_protocol_and_token_contract() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("deepseek"), profile_id="deepseek-main")

    snapshot = request_snapshot(profile, ProviderRole.MEMORY, catalog)

    assert snapshot.profile_id == "deepseek-main"
    assert snapshot.model == profile.model_for(ProviderRole.MEMORY)
    assert snapshot.protocol is ProviderProtocol.OPENAI_CHAT_COMPLETIONS
    assert snapshot.token_limit_field == "max_tokens"
    assert not hasattr(snapshot, "secret")


def test_static_catalog_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        parse_provider_catalog({"schema_version": 1, "providers": [], "extra": True})


def test_static_catalog_schema_rejects_an_unsafe_built_in_endpoint() -> None:
    document = {
        "schema_version": 1,
        "providers": [
            {
                "id": "unsafe",
                "name": "Unsafe",
                "protocol": "openai_chat_completions",
                "endpoints": ["http://example.invalid/v1"],
                "auth": "bearer",
                "models": {role.value: "model" for role in ProviderRole},
                "stream": True,
                "vision": True,
                "native_files": False,
                "reasoning": False,
                "reasoning_can_disable": False,
                "context_tokens": 32_768,
                "output_tokens": 4_096,
                "first_chunk_timeout_seconds": 30,
                "idle_timeout_seconds": 30,
                "custom_models": True,
                "reasoning_policy": "none",
                "cache_policy": "none",
            }
        ],
    }

    with pytest.raises(ValueError):
        parse_provider_catalog(document)
