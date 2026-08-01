from __future__ import annotations

from dataclasses import replace

import pytest

from amadeus_desktop.provider_config import (
    PROVIDER_CREDENTIAL_REF,
    AuthMode,
    ProviderConfig,
    ProviderConfigError,
    ProviderPreset,
    TokenLimitField,
)


def test_deepseek_is_the_default_preset() -> None:
    config = ProviderConfig.default()

    assert config.preset is ProviderPreset.DEEPSEEK_PAYG
    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-v4-flash"
    assert config.auth_mode is AuthMode.BEARER
    assert config.token_limit_field is TokenLimitField.MAX_TOKENS
    assert config.credential_ref == PROVIDER_CREDENTIAL_REF
    assert config.connect_timeout_seconds == 15.0
    assert config.request_timeout_seconds == 90.0
    assert config.max_output_tokens == 1024
    assert config.stream_enabled is True


def test_mimo_payg_contract_is_distinct() -> None:
    config = ProviderConfig.for_preset(ProviderPreset.MIMO_PAYG)

    assert config.base_url == "https://api.xiaomimimo.com/v1"
    assert config.model == "mimo-v2.5-pro"
    assert config.auth_mode is AuthMode.API_KEY
    assert config.token_limit_field is TokenLimitField.MAX_COMPLETION_TOKENS


def test_custom_openai_configuration_round_trips() -> None:
    config = ProviderConfig.for_preset(
        ProviderPreset.CUSTOM_OPENAI,
        display_name="Example local gateway",
        base_url="https://gateway.example.invalid:8443/openai/v1",
        model="org/example-model",
        auth_mode=AuthMode.API_KEY,
        token_limit_field=TokenLimitField.MAX_COMPLETION_TOKENS,
        stream_enabled=False,
    )

    assert ProviderConfig.from_mapping(config.to_mapping()) == config
    assert config.chat_completions_url == (
        "https://gateway.example.invalid:8443/openai/v1/chat/completions"
    )


def test_credential_scope_changes_with_provider_url_or_authentication() -> None:
    first = ProviderConfig.for_preset(
        ProviderPreset.CUSTOM_OPENAI,
        base_url="https://first.example.invalid/v1",
    )
    same_scope_other_model = replace(first, model="other-model")
    other_host = replace(first, base_url="https://second.example.invalid/v1")
    other_auth = replace(first, auth_mode=AuthMode.API_KEY)

    assert first.credential_scope == same_scope_other_model.credential_scope
    assert first.credential_scope != other_host.credential_scope
    assert first.credential_scope != other_auth.credential_scope


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.invalid/v1",
        "https://user:pass@api.example.invalid/v1",
        "https://api.example.invalid/v1?mode=test",
        "https://api.example.invalid/v1#fragment",
        "https://api.example.invalid/v1/chat/completions",
        "https://api.example.invalid/chat/completions/",
        "https://token-plan-api.xiaomimimo.com/v1",
        "https://token-plan-a.b.xiaomimimo.com/v1",
        "https://%74oken-plan-api.xiaomimimo.com/v1",
        "https://api.example.invalid/v1/%63hat/completions",
        "https://api.example.invalid/v1/",
        "https://API.example.invalid/v1",
        " https://api.example.invalid/v1",
        "https:\\api.example.invalid\\v1",
        "https:///v1",
    ],
)
def test_unsafe_or_non_normalized_base_urls_are_rejected(base_url: str) -> None:
    with pytest.raises(ProviderConfigError):
        ProviderConfig.for_preset(ProviderPreset.CUSTOM_OPENAI, base_url=base_url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "https://other.example.invalid/v1"),
        ("display_name", "Other"),
        ("auth_mode", AuthMode.API_KEY),
        ("token_limit_field", TokenLimitField.MAX_COMPLETION_TOKENS),
    ],
)
def test_built_in_contract_fields_cannot_be_changed(field: str, value: object) -> None:
    with pytest.raises(ProviderConfigError):
        ProviderConfig.for_preset(ProviderPreset.DEEPSEEK_PAYG, **{field: value})


def test_model_can_be_changed_without_weakening_deepseek_contract() -> None:
    config = ProviderConfig.for_preset(
        ProviderPreset.DEEPSEEK_PAYG,
        model="deepseek-v4-pro",
    )

    assert config.model == "deepseek-v4-pro"


def test_credential_reference_is_code_owned() -> None:
    with pytest.raises(ProviderConfigError):
        replace(ProviderConfig.default(), credential_ref="user-selected-target")


def test_mapping_requires_exact_fields() -> None:
    mapping = ProviderConfig.default().to_mapping()
    mapping["future_field"] = True

    with pytest.raises(ProviderConfigError):
        ProviderConfig.from_mapping(mapping)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connect_timeout_seconds", 0),
        ("request_timeout_seconds", 301),
        ("max_output_tokens", True),
        ("max_output_tokens", 0),
        ("temperature", float("nan")),
        ("top_p", 0),
        ("stream_enabled", 1),
    ],
)
def test_invalid_generation_and_timeout_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ProviderConfigError):
        replace(ProviderConfig.default(), **{field: value})
