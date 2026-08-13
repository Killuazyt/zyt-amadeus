from __future__ import annotations

from dataclasses import replace

import pytest

import amadeus_desktop.credential_store as credential_module
from amadeus_desktop.credential_store import (
    WINCRED_MIMO_SPEECH_TARGET_NAME,
    WINCRED_MULTIMODAL_TARGET_NAME,
    WINCRED_PROFILE_TARGET_PREFIX,
    WINCRED_TARGET_NAME,
    delete_all_amadeus_credentials,
    profile_credential_target,
    validate_owned_profile_target,
)
from amadeus_desktop.provider_catalog import ProviderAuth, load_provider_catalog
from amadeus_desktop.provider_profiles import ProviderProfile


def _profile() -> ProviderProfile:
    catalog = load_provider_catalog()
    return ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="stable-profile")


def test_dynamic_target_uses_stable_profile_and_scope_but_not_model() -> None:
    profile = _profile()
    first = profile_credential_target(profile)
    changed_model = replace(
        profile,
        models={role: f"changed-{role.value}" for role in profile.models},
    )
    changed_endpoint = replace(profile, base_url="https://api.openai.com/v1/alternate")

    assert profile_credential_target(changed_model) == first
    assert profile_credential_target(changed_endpoint) != first
    assert first.startswith(WINCRED_PROFILE_TARGET_PREFIX)
    assert "stable-profile" not in first
    assert "changed" not in first


def test_dynamic_target_changes_with_protocol_auth_scope() -> None:
    profile = _profile()
    bearer = profile_credential_target(profile)
    header_scope = profile_credential_target(replace(profile, auth=ProviderAuth.API_KEY))

    assert bearer != header_scope
    assert profile.profile_id not in bearer
    assert profile.profile_id not in header_scope


@pytest.mark.parametrize(
    "target",
    [
        "OtherProduct/ModelProvider/abc/def",
        f"{WINCRED_PROFILE_TARGET_PREFIX}short/short",
        f"{WINCRED_PROFILE_TARGET_PREFIX}{'0' * 32}/{'z' * 24}",
    ],
)
def test_dynamic_target_validator_rejects_non_amadeus_or_malformed_targets(target) -> None:
    with pytest.raises(ValueError):
        validate_owned_profile_target(target)


def test_factory_reset_cleanup_deletes_only_fixed_and_enumerated_amadeus_targets(
    monkeypatch,
) -> None:
    dynamic = f"{WINCRED_PROFILE_TARGET_PREFIX}{'a' * 32}/{'b' * 24}"
    external = "OtherProduct/Credential"
    deleted: list[str] = []
    existing = {
        WINCRED_TARGET_NAME,
        WINCRED_MULTIMODAL_TARGET_NAME,
        WINCRED_MIMO_SPEECH_TARGET_NAME,
        dynamic,
        external,
    }

    monkeypatch.setattr(
        credential_module,
        "_enumerate_dynamic_profile_targets",
        lambda: (dynamic,) if dynamic in existing else (),
    )

    def delete(target: str) -> None:
        deleted.append(target)
        existing.discard(target)

    monkeypatch.setattr(credential_module, "_delete_secret", delete)
    monkeypatch.setattr(
        credential_module,
        "_read_secret",
        lambda target: "invalid-fake" if target in existing else None,
    )

    delete_all_amadeus_credentials()

    assert set(deleted) == {
        WINCRED_TARGET_NAME,
        WINCRED_MULTIMODAL_TARGET_NAME,
        WINCRED_MIMO_SPEECH_TARGET_NAME,
        dynamic,
    }
    assert external in existing
