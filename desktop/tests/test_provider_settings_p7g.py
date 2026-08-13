from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest

import amadeus_desktop.provider_profiles as provider_profiles_module
import amadeus_desktop.ui.provider_settings as provider_settings_module
from amadeus_desktop.chat_provider import ConnectionTestResult
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.provider_catalog import ProviderRole, load_provider_catalog
from amadeus_desktop.provider_profiles import (
    MAX_PROVIDER_PROFILES,
    ProviderProfile,
    ProviderProfileError,
    ProviderSettings,
    transient_credential_fingerprint,
)
from amadeus_desktop.ui.provider_settings import ProviderSettingsChange, ProviderSettingsPage


class _Tester:
    async def test_profile(self, snapshot, _secret, _cancellation, **_kwargs):
        return ConnectionTestResult(snapshot.catalog_id, snapshot.model, 1)


def _settings() -> ProviderSettings:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="ui-main")
    return ProviderSettings(
        (profile,),
        MappingProxyType({role: profile.profile_id for role in ProviderRole}),
    ).validated(catalog)


def test_profile_copy_has_new_stable_id_and_does_not_copy_secret_or_tests(qtbot) -> None:
    stores: dict[str, InMemoryCredentialStore] = {}

    def factory(profile):
        return stores.setdefault(profile.profile_id, InMemoryCredentialStore())

    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=factory,
        tester=_Tester(),
    )
    qtbot.addWidget(page)
    factory(page._profiles[0]).write_secret("invalid-fake-original-key")

    page._copy_profile()

    assert len(page._profiles) == 2
    copied = page._profiles[1]
    assert copied.profile_id != "ui-main"
    assert not copied.enabled
    assert all(not value for value in copied.test_fingerprints.values())
    assert copied.profile_id not in stores or not stores[copied.profile_id].has_secret()


def test_deleting_an_unsaved_copy_does_not_schedule_a_persisted_credential_delete(
    qtbot, monkeypatch
) -> None:
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: InMemoryCredentialStore(),
        tester=_Tester(),
    )
    qtbot.addWidget(page)
    page._copy_profile()
    copied_id = page._profiles[1].profile_id
    page._assignments = {role: None for role in ProviderRole}
    page.profile_list.setCurrentRow(1)
    monkeypatch.setattr(
        provider_settings_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: provider_settings_module.QMessageBox.StandardButton.Yes,
    )

    page._delete_profile()

    assert all(profile.profile_id != copied_id for profile in page._profiles)
    assert page._deleted_profiles == []


def test_assigned_profile_cannot_be_deleted_and_profile_limit_is_enforced(qtbot) -> None:
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: InMemoryCredentialStore(),
        tester=_Tester(),
    )
    qtbot.addWidget(page)

    page._delete_profile()
    assert len(page._profiles) == 1
    assert "重新分配" in page.status.text()

    source = page._profiles[0]
    page._profiles = [
        replace(source, profile_id=f"profile-{index}") for index in range(MAX_PROVIDER_PROFILES)
    ]
    page._add_profile()
    assert len(page._profiles) == MAX_PROVIDER_PROFILES
    assert "32" in page.status.text()


def test_last_profile_can_be_deleted_after_every_task_is_cancelled(qtbot, monkeypatch) -> None:
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: InMemoryCredentialStore(),
        tester=_Tester(),
    )
    qtbot.addWidget(page)
    page._assignments = {role: None for role in ProviderRole}
    monkeypatch.setattr(
        provider_settings_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: provider_settings_module.QMessageBox.StandardButton.Yes,
    )

    page._delete_profile()

    candidate = (
        ProviderSettings(
            tuple(page._profiles),
            MappingProxyType(dict(page._assignments)),
        )
        .validated(load_provider_catalog())
        .require_assigned_tests()
    )
    assert candidate.profiles == ()
    assert all(profile_id is None for profile_id in candidate.assignments.values())
    assert [profile.profile_id for profile in page._deleted_profiles] == ["ui-main"]


def test_profile_test_fingerprint_invalidates_on_request_field_or_secret_change() -> None:
    profile = _settings().profiles[0]
    tested = profile.mark_tested(ProviderRole.CONVERSATION)

    assert tested.is_tested(ProviderRole.CONVERSATION)
    assert not replace(tested, temperature=0.2).is_tested(ProviderRole.CONVERSATION)
    assert not tested.invalidate_tests().is_tested(ProviderRole.CONVERSATION)


def test_connection_result_is_discarded_if_credential_changes_while_test_runs(qtbot) -> None:
    store = InMemoryCredentialStore("invalid-fake-first-key")
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: store,
        tester=_Tester(),
    )
    qtbot.addWidget(page)
    profile = page._profiles[0]
    request_fingerprint = profile.test_fingerprint(ProviderRole.CONVERSATION)
    credential_fingerprint = transient_credential_fingerprint("invalid-fake-first-key")
    store.write_secret("invalid-fake-replacement-key")

    page._test_succeeded(
        profile.profile_id,
        ProviderRole.CONVERSATION.value,
        request_fingerprint,
        credential_fingerprint,
        1,
    )

    assert not page._profiles[0].is_tested(ProviderRole.CONVERSATION)
    assert "丢弃" in page.status.text()


def test_connection_result_is_discarded_if_profile_no_longer_exists(qtbot) -> None:
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: InMemoryCredentialStore(
            "invalid-fake-provider-key"
        ),
        tester=_Tester(),
    )
    qtbot.addWidget(page)
    profile = page._profiles.pop()

    page._test_succeeded(
        profile.profile_id,
        ProviderRole.CONVERSATION.value,
        profile.test_fingerprint(ProviderRole.CONVERSATION),
        transient_credential_fingerprint("invalid-fake-provider-key"),
        1,
    )

    assert "已不存在" in page.status.text()


def test_tested_transient_secret_survives_an_unrelated_profile_name_edit(qtbot) -> None:
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: InMemoryCredentialStore("invalid-fake-old-key"),
        tester=_Tester(),
    )
    qtbot.addWidget(page)
    page._secret_updates["ui-main"] = "invalid-fake-tested-replacement-key"
    page._profiles[0] = page._profiles[0].mark_tested(ProviderRole.CONVERSATION)

    page.name_edit.setText("Renamed Profile")

    assert page._secret_updates["ui-main"] == "invalid-fake-tested-replacement-key"
    assert page._profiles[0].is_tested(ProviderRole.CONVERSATION)


def test_explicitly_clearing_the_password_editor_discards_its_transient_update(qtbot) -> None:
    page = ProviderSettingsPage(
        _settings(),
        load_provider_catalog(),
        credential_store_factory=lambda _profile: InMemoryCredentialStore("invalid-fake-old-key"),
        tester=_Tester(),
    )
    qtbot.addWidget(page)

    page.secret_edit.setText("invalid-fake-candidate-key")
    assert page._secret_updates["ui-main"] == "invalid-fake-candidate-key"
    page.secret_edit.clear()

    assert "ui-main" not in page._secret_updates


def test_same_profile_same_text_model_can_share_one_test_but_vision_cannot() -> None:
    profile = _settings().profiles[0]
    same_models = replace(
        profile,
        models=MappingProxyType({role: "same-model" for role in ProviderRole}),
    )
    tested = same_models.mark_tested(ProviderRole.CONVERSATION)

    assert tested.is_tested(ProviderRole.SUMMARY)
    assert tested.is_tested(ProviderRole.MEMORY)
    assert not tested.is_tested(ProviderRole.VISION)


def test_connection_test_fingerprint_is_invalid_on_another_machine(monkeypatch) -> None:
    profile = _settings().profiles[0]
    monkeypatch.setattr(
        provider_profiles_module,
        "current_machine_binding_digest",
        lambda: "a" * 64,
    )
    tested = profile.mark_tested(ProviderRole.CONVERSATION)
    assert tested.is_tested(ProviderRole.CONVERSATION)

    monkeypatch.setattr(
        provider_profiles_module,
        "current_machine_binding_digest",
        lambda: "b" * 64,
    )
    assert not tested.is_tested(ProviderRole.CONVERSATION)


def test_assigned_enabled_model_requires_current_test() -> None:
    settings = _settings()
    enabled = replace(settings.profiles[0], enabled=True)
    candidate = ProviderSettings((enabled,), settings.assignments)

    with pytest.raises(ProviderProfileError):
        candidate.require_assigned_tests()


def test_unassigned_task_is_an_explicit_fail_closed_state() -> None:
    settings = _settings()
    assignments = {role: None for role in ProviderRole}

    candidate = ProviderSettings(
        settings.profiles,
        MappingProxyType(assignments),
    ).require_assigned_tests()

    assert candidate.assigned_profile(ProviderRole.VISION) is None


def test_assigned_disabled_profile_is_rejected_even_with_a_migrated_test_fingerprint() -> None:
    settings = _settings()
    profile = settings.profiles[0]
    migrated = replace(
        profile,
        test_fingerprints=MappingProxyType(
            {role: profile.test_fingerprint(role) for role in ProviderRole}
        ),
    )

    with pytest.raises(ProviderProfileError):
        ProviderSettings((migrated,), settings.assignments).require_assigned_tests()


def test_settings_change_contains_only_profile_settings_and_transient_secret_updates() -> None:
    change = ProviderSettingsChange(
        _settings(),
        MappingProxyType({"ui-main": "invalid-fake-key"}),
        MappingProxyType({"ui-main": transient_credential_fingerprint("invalid-fake-key")}),
        (),
    )

    assert "invalid-fake-key" not in str(change.settings.to_mapping())
    assert "invalid-fake-key" not in repr(change)
    assert change.secret_updates["ui-main"] == "invalid-fake-key"


def test_unknown_profile_in_degraded_catalog_requires_explicit_recovery_choice(qtbot) -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="unknown-main")
    degraded = replace(
        catalog,
        entries=MappingProxyType(
            {
                key: value
                for key, value in catalog.entries.items()
                if key in {"custom_openai", "custom_anthropic"}
            }
        ),
        degraded=True,
    )
    page = ProviderSettingsPage(
        ProviderSettings(
            (profile,),
            MappingProxyType({role: profile.profile_id for role in ProviderRole}),
        ),
        degraded,
        credential_store_factory=lambda _profile: InMemoryCredentialStore(),
        tester=_Tester(),
    )
    qtbot.addWidget(page)

    assert page.catalog_combo.currentIndex() == -1
    with pytest.raises(ProviderProfileError):
        page._candidate(invalidate=True)
