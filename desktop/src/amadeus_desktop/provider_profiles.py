"""P7G provider profiles, task assignments, validation and runtime snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Mapping, Self
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit
from uuid import uuid4

from amadeus_desktop.provider_catalog import (
    CachePolicy,
    ProviderAuth,
    ProviderCatalog,
    ProviderCatalogError,
    ProviderCatalogEntry,
    ProviderProtocol,
    ProviderRole,
    ReasoningPolicy,
    load_provider_catalog,
)
from amadeus_desktop.provider_config import (
    MULTIMODAL_CREDENTIAL_REF,
    PROVIDER_CREDENTIAL_REF,
    ProviderConfig,
    ProviderPreset,
)

MAX_PROVIDER_PROFILES = 32
LEGACY_CHAT_PROFILE_ID = "legacy-chat"
LEGACY_VISION_PROFILE_ID = "legacy-vision"
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "catalog_id",
        "display_name",
        "enabled",
        "protocol",
        "base_url",
        "auth",
        "credential_slot",
        "models",
        "connect_timeout_seconds",
        "request_timeout_seconds",
        "first_chunk_timeout_seconds",
        "idle_timeout_seconds",
        "max_output_tokens",
        "temperature",
        "top_p",
        "stream_enabled",
        "cache_enabled",
        "test_fingerprints",
    }
)
_SETTINGS_FIELDS = frozenset({"profiles", "assignments"})
_ROLE_FIELDS = frozenset(role.value for role in ProviderRole)
_CREDENTIAL_SLOTS = frozenset({"dynamic", "legacy_chat", "legacy_vision"})


class ProviderProfileError(ValueError):
    """Raised when a profile, assignment or endpoint is unsafe."""


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    profile_id: str
    catalog_id: str
    display_name: str
    enabled: bool
    protocol: ProviderProtocol
    base_url: str
    auth: ProviderAuth
    credential_slot: str
    models: Mapping[ProviderRole, str]
    connect_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 90.0
    first_chunk_timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 30.0
    max_output_tokens: int = 4_096
    temperature: float = 0.7
    top_p: float = 0.9
    stream_enabled: bool = True
    cache_enabled: bool = False
    test_fingerprints: Mapping[ProviderRole, str] = field(
        default_factory=lambda: MappingProxyType({role: "" for role in ProviderRole})
    )

    def __post_init__(self) -> None:
        self.validated()

    @classmethod
    def from_catalog(
        cls,
        entry: ProviderCatalogEntry,
        *,
        profile_id: str | None = None,
        display_name: str | None = None,
    ) -> Self:
        identifier = profile_id or f"profile-{uuid4().hex}"
        endpoint = entry.default_endpoint
        auth = entry.auth
        if entry.custom_endpoint and not entry.endpoints:
            if entry.protocol is ProviderProtocol.OPENAI_CHAT_COMPLETIONS:
                endpoint = "http://localhost:11434/v1"
                auth = ProviderAuth.NONE
            else:
                endpoint = "https://api.example.invalid/v1"
        return cls(
            profile_id=identifier,
            catalog_id=entry.catalog_id,
            display_name=display_name or entry.display_name,
            enabled=False,
            protocol=entry.protocol,
            base_url=endpoint,
            auth=auth,
            credential_slot="dynamic",
            models=MappingProxyType(dict(entry.models)),
            max_output_tokens=entry.capabilities.suggested_output_tokens,
            first_chunk_timeout_seconds=entry.capabilities.first_chunk_timeout_seconds,
            idle_timeout_seconds=entry.capabilities.idle_timeout_seconds,
            stream_enabled=entry.capabilities.streaming,
        )

    @classmethod
    def from_mapping(
        cls,
        source: object,
        catalog: ProviderCatalog,
    ) -> Self:
        if not isinstance(source, Mapping) or set(source) != _PROFILE_FIELDS:
            raise ProviderProfileError("Provider profile fields are invalid.")
        try:
            models_raw = source["models"]
            tests_raw = source["test_fingerprints"]
            if not isinstance(models_raw, Mapping) or set(models_raw) != _ROLE_FIELDS:
                raise ProviderProfileError("Provider model slots are invalid.")
            if not isinstance(tests_raw, Mapping) or set(tests_raw) != _ROLE_FIELDS:
                raise ProviderProfileError("Provider test slots are invalid.")
            profile = cls(
                profile_id=source["profile_id"],
                catalog_id=source["catalog_id"],
                display_name=source["display_name"],
                enabled=source["enabled"],
                protocol=ProviderProtocol(source["protocol"]),
                base_url=source["base_url"],
                auth=ProviderAuth(source["auth"]),
                credential_slot=source["credential_slot"],
                models=MappingProxyType(
                    {ProviderRole(role): models_raw[role] for role in _ROLE_FIELDS}
                ),
                connect_timeout_seconds=source["connect_timeout_seconds"],
                request_timeout_seconds=source["request_timeout_seconds"],
                first_chunk_timeout_seconds=source["first_chunk_timeout_seconds"],
                idle_timeout_seconds=source["idle_timeout_seconds"],
                max_output_tokens=source["max_output_tokens"],
                temperature=source["temperature"],
                top_p=source["top_p"],
                stream_enabled=source["stream_enabled"],
                cache_enabled=source["cache_enabled"],
                test_fingerprints=MappingProxyType(
                    {ProviderRole(role): tests_raw[role] for role in _ROLE_FIELDS}
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderProfileError("Provider profile values are invalid.") from None
        if not catalog.degraded or profile.catalog_id in catalog.entries:
            try:
                profile.validated(catalog)
            except ProviderCatalogError:
                raise ProviderProfileError("Provider catalog type is unknown.") from None
        return profile

    def to_mapping(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "catalog_id": self.catalog_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "protocol": self.protocol.value,
            "base_url": self.base_url,
            "auth": self.auth.value,
            "credential_slot": self.credential_slot,
            "models": {role.value: self.models.get(role, "") for role in ProviderRole},
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "first_chunk_timeout_seconds": self.first_chunk_timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stream_enabled": self.stream_enabled,
            "cache_enabled": self.cache_enabled,
            "test_fingerprints": {
                role.value: self.test_fingerprints.get(role, "") for role in ProviderRole
            },
        }

    def validated(self, catalog: ProviderCatalog | None = None) -> Self:
        if not isinstance(self.profile_id, str) or PROFILE_ID_PATTERN.fullmatch(self.profile_id) is None:
            raise ProviderProfileError("Provider profile id is invalid.")
        if not isinstance(self.catalog_id, str) or len(self.catalog_id) > 48:
            raise ProviderProfileError("Provider catalog id is invalid.")
        _validate_text(self.display_name, 80, "Provider profile name")
        if not isinstance(self.enabled, bool):
            raise ProviderProfileError("Provider enabled state is invalid.")
        normalized = normalize_provider_base_url(self.base_url)
        if normalized != self.base_url:
            raise ProviderProfileError("Provider URL must already be normalized.")
        if self.credential_slot not in _CREDENTIAL_SLOTS:
            raise ProviderProfileError("Provider credential slot is invalid.")
        if self.credential_slot == "legacy_chat" and self.profile_id != LEGACY_CHAT_PROFILE_ID:
            raise ProviderProfileError("Legacy chat credential slot is bound to its migration profile.")
        if self.credential_slot == "legacy_vision" and self.profile_id != LEGACY_VISION_PROFILE_ID:
            raise ProviderProfileError("Legacy vision credential slot is bound to its migration profile.")
        if not isinstance(self.models, Mapping) or set(self.models) != set(ProviderRole):
            raise ProviderProfileError("Provider model slots are invalid.")
        for model in self.models.values():
            if model:
                _validate_text(model, 192, "Provider model")
        for label, value, minimum, maximum in (
            ("Connect timeout", self.connect_timeout_seconds, 1.0, 60.0),
            ("Request timeout", self.request_timeout_seconds, 5.0, 300.0),
            ("First chunk timeout", self.first_chunk_timeout_seconds, 1.0, 120.0),
            ("Idle timeout", self.idle_timeout_seconds, 1.0, 120.0),
        ):
            _validate_number(value, label, minimum, maximum)
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ProviderProfileError("Request timeout is shorter than connect timeout.")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 131_072
        ):
            raise ProviderProfileError("Provider output limit is invalid.")
        _validate_number(self.temperature, "Temperature", 0.0, 2.0)
        _validate_number(self.top_p, "Top P", 0.000001, 1.0)
        if not isinstance(self.stream_enabled, bool) or not isinstance(self.cache_enabled, bool):
            raise ProviderProfileError("Provider stream/cache state is invalid.")
        if not isinstance(self.test_fingerprints, Mapping) or set(self.test_fingerprints) != set(
            ProviderRole
        ):
            raise ProviderProfileError("Provider test fingerprints are invalid.")
        for fingerprint in self.test_fingerprints.values():
            if not isinstance(fingerprint, str):
                raise ProviderProfileError("Provider test fingerprint is invalid.")
            if fingerprint and (
                len(fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint)
            ):
                raise ProviderProfileError("Provider test fingerprint is invalid.")
        if self.auth is ProviderAuth.NONE:
            hostname = urlsplit(self.base_url).hostname
            if hostname is None or hostname.rstrip(".").casefold() not in _LOOPBACK_HOSTS:
                raise ProviderProfileError("Unauthenticated providers must use a loopback endpoint.")
        if catalog is not None:
            entry = catalog.entry(self.catalog_id)
            if self.protocol is not entry.protocol:
                raise ProviderProfileError("Catalog protocol cannot be changed.")
            if entry.custom_endpoint:
                allowed_auth = (
                    {ProviderAuth.BEARER, ProviderAuth.API_KEY, ProviderAuth.NONE}
                    if entry.protocol is ProviderProtocol.OPENAI_CHAT_COMPLETIONS
                    else {
                        ProviderAuth.ANTHROPIC_X_API_KEY,
                        ProviderAuth.BEARER,
                        ProviderAuth.NONE,
                    }
                )
                if self.auth not in allowed_auth:
                    raise ProviderProfileError("Custom provider authentication is invalid.")
            elif self.auth is not entry.auth:
                raise ProviderProfileError("Built-in authentication cannot be changed.")
            if not entry.custom_endpoint:
                if self.base_url not in entry.endpoints:
                    raise ProviderProfileError("Built-in endpoint is not in the catalog.")
            if self.stream_enabled and not entry.capabilities.streaming:
                raise ProviderProfileError("Provider does not support streaming.")
            if self.models.get(ProviderRole.VISION) and not entry.capabilities.image_input:
                raise ProviderProfileError("Provider does not support image input.")
            if self.cache_enabled and entry.cache_policy not in {
                CachePolicy.DASHSCOPE_SESSION,
                CachePolicy.ANTHROPIC_EPHEMERAL,
            }:
                raise ProviderProfileError("Provider has no Amadeus-controlled cache mode.")
        return self

    @property
    def credential_scope_digest(self) -> str:
        material = "\0".join((self.protocol.value, self.base_url, self.auth.value))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def model_for(self, role: ProviderRole) -> str:
        return self.models.get(role, "")

    def test_fingerprint(self, role: ProviderRole) -> str:
        model = self.model_for(role)
        if not model:
            return ""
        payload = {
            "profile_id": self.profile_id,
            "catalog_id": self.catalog_id,
            "scope": self.credential_scope_digest,
            # This one-way value is deliberately not part of the credential
            # target.  It only prevents a backed-up connection test from being
            # accepted on another machine where a coincidentally matching
            # Profile/WinCred target might exist.
            "machine_binding": current_machine_binding_digest(),
            # Text tasks using the exact same model and request contract share
            # one minimal test. Vision is always isolated because its test must
            # include the app-generated image fixture.
            "test_kind": "vision" if role is ProviderRole.VISION else "text",
            "model": model,
            "timeouts": [
                self.connect_timeout_seconds,
                self.request_timeout_seconds,
                self.first_chunk_timeout_seconds,
                self.idle_timeout_seconds,
            ],
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stream": self.stream_enabled,
            "cache": self.cache_enabled,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_tested(self, role: ProviderRole) -> bool:
        try:
            expected = self.test_fingerprint(role)
        except ProviderProfileError:
            return False
        if not expected:
            return False
        if self.test_fingerprints.get(role, "") == expected:
            return True
        if role is ProviderRole.VISION:
            return False
        return any(
            other is not ProviderRole.VISION
            and self.model_for(other) == self.model_for(role)
            and self.test_fingerprints.get(other, "") == expected
            for other in ProviderRole
        )

    def invalidate_tests(self, *roles: ProviderRole) -> Self:
        selected = set(roles) or set(ProviderRole)
        fingerprints = dict(self.test_fingerprints)
        for role in selected:
            fingerprints[role] = ""
        return replace(self, test_fingerprints=MappingProxyType(fingerprints))

    def mark_tested(self, role: ProviderRole) -> Self:
        fingerprints = dict(self.test_fingerprints)
        fingerprint = self.test_fingerprint(role)
        for candidate in ProviderRole:
            if candidate is role or (
                role is not ProviderRole.VISION
                and candidate is not ProviderRole.VISION
                and self.model_for(candidate) == self.model_for(role)
            ):
                fingerprints[candidate] = fingerprint
        return replace(self, test_fingerprints=MappingProxyType(fingerprints))

    @property
    def test_connection_state(self) -> str:
        configured = [role for role in ProviderRole if self.model_for(role)]
        if not configured:
            return "not_configured"
        tested = [role for role in configured if self.is_tested(role)]
        if len(tested) == len(configured):
            return "tested"
        return "partial" if tested else "untested"


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    profiles: tuple[ProviderProfile, ...]
    assignments: Mapping[ProviderRole, str | None]

    @classmethod
    def from_mapping(
        cls,
        source: object,
        catalog: ProviderCatalog,
        *,
        strict_tests: bool = False,
    ) -> Self:
        if not isinstance(source, Mapping) or set(source) != _SETTINGS_FIELDS:
            raise ProviderProfileError("Provider settings are invalid.")
        profiles_raw = source["profiles"]
        assignments_raw = source["assignments"]
        if not isinstance(profiles_raw, list) or not isinstance(assignments_raw, Mapping):
            raise ProviderProfileError("Provider settings values are invalid.")
        if set(assignments_raw) != _ROLE_FIELDS:
            raise ProviderProfileError("Provider assignments are invalid.")
        profiles = tuple(ProviderProfile.from_mapping(value, catalog) for value in profiles_raw)
        assignments = MappingProxyType(
            {ProviderRole(role): assignments_raw[role] for role in _ROLE_FIELDS}
        )
        result = cls(profiles, assignments).runtime_validated(catalog)
        return result.require_assigned_tests() if strict_tests else result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "profiles": [profile.to_mapping() for profile in self.profiles],
            "assignments": {
                role.value: self.assignments.get(role) for role in ProviderRole
            },
        }

    def validated(self, catalog: ProviderCatalog) -> Self:
        if not 0 <= len(self.profiles) <= MAX_PROVIDER_PROFILES:
            raise ProviderProfileError("Provider profile count is invalid.")
        identifiers = [profile.profile_id for profile in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ProviderProfileError("Provider profile ids must be unique.")
        profile_map = {profile.profile_id: profile for profile in self.profiles}
        for profile in self.profiles:
            if catalog.degraded and profile.catalog_id not in catalog.entries:
                profile.validated(None)
            else:
                try:
                    profile.validated(catalog)
                except ProviderCatalogError:
                    raise ProviderProfileError("Provider catalog type is unknown.") from None
        if set(self.assignments) != set(ProviderRole):
            raise ProviderProfileError("Provider assignments are incomplete.")
        for role, profile_id in self.assignments.items():
            if profile_id is not None and profile_id not in profile_map:
                raise ProviderProfileError("Provider assignment references a missing profile.")
            if profile_id is not None:
                profile = profile_map[profile_id]
                if not profile.model_for(role):
                    raise ProviderProfileError("Assigned provider has no model for the task.")
                if role is ProviderRole.VISION:
                    entry = catalog.entries.get(profile.catalog_id)
                    if entry is not None and not entry.capabilities.image_input:
                        raise ProviderProfileError("Vision assignment lacks image capability.")
        return self

    def runtime_validated(self, catalog: ProviderCatalog) -> Self:
        """Validate structure while preserving profiles during catalog recovery."""

        return self.validated(catalog)

    def profile(self, profile_id: str | None) -> ProviderProfile | None:
        if profile_id is None:
            return None
        return next((value for value in self.profiles if value.profile_id == profile_id), None)

    def assigned_profile(self, role: ProviderRole) -> ProviderProfile | None:
        return self.profile(self.assignments.get(role))

    def replace_profile(self, candidate: ProviderProfile, catalog: ProviderCatalog) -> Self:
        profiles = tuple(
            candidate if value.profile_id == candidate.profile_id else value
            for value in self.profiles
        )
        if all(value.profile_id != candidate.profile_id for value in self.profiles):
            if len(self.profiles) >= MAX_PROVIDER_PROFILES:
                raise ProviderProfileError("Provider profile limit reached.")
            profiles += (candidate,)
        return ProviderSettings(profiles, self.assignments).validated(catalog)

    def require_assigned_tests(self) -> Self:
        for role in ProviderRole:
            profile = self.assigned_profile(role)
            if profile is None:
                continue
            if not profile.enabled:
                raise ProviderProfileError("Every assigned profile must be enabled.")
            if not profile.is_tested(role):
                raise ProviderProfileError("Every assigned model must pass its current test.")
        return self

    def delete_profile(self, profile_id: str, catalog: ProviderCatalog) -> Self:
        if profile_id in self.assignments.values():
            raise ProviderProfileError("Assigned profile must be reassigned before deletion.")
        remaining = tuple(value for value in self.profiles if value.profile_id != profile_id)
        if len(remaining) == len(self.profiles):
            raise ProviderProfileError("Provider profile was not found.")
        return ProviderSettings(remaining, self.assignments).validated(catalog)


@dataclass(frozen=True, slots=True)
class ProviderRequestSnapshot:
    profile_id: str
    catalog_id: str
    provider_name: str
    role: ProviderRole
    protocol: ProviderProtocol
    base_url: str
    auth: ProviderAuth
    credential_slot: str
    credential_scope_digest: str
    model: str
    connect_timeout_seconds: float
    request_timeout_seconds: float
    first_chunk_timeout_seconds: float
    idle_timeout_seconds: float
    max_output_tokens: int
    token_limit_field: str
    temperature: float
    top_p: float
    stream_enabled: bool
    cache_enabled: bool
    reasoning_policy: ReasoningPolicy
    cache_policy: CachePolicy
    filter_think_tags: bool


def request_snapshot(
    profile: ProviderProfile,
    role: ProviderRole,
    catalog: ProviderCatalog,
) -> ProviderRequestSnapshot:
    profile.validated(catalog)
    entry = catalog.entry(profile.catalog_id)
    model = profile.model_for(role)
    if not model:
        raise ProviderProfileError("Assigned provider has no model for this task.")
    return ProviderRequestSnapshot(
        profile_id=profile.profile_id,
        catalog_id=profile.catalog_id,
        provider_name=profile.display_name,
        role=role,
        protocol=profile.protocol,
        base_url=profile.base_url,
        auth=profile.auth,
        credential_slot=profile.credential_slot,
        credential_scope_digest=profile.credential_scope_digest,
        model=model,
        connect_timeout_seconds=profile.connect_timeout_seconds,
        request_timeout_seconds=profile.request_timeout_seconds,
        first_chunk_timeout_seconds=profile.first_chunk_timeout_seconds,
        idle_timeout_seconds=profile.idle_timeout_seconds,
        max_output_tokens=profile.max_output_tokens,
        token_limit_field=(
            "max_completion_tokens"
            if profile.catalog_id in {"mimo_payg", "openai"}
            else "max_tokens"
        ),
        temperature=profile.temperature,
        top_p=profile.top_p,
        stream_enabled=profile.stream_enabled,
        cache_enabled=profile.cache_enabled,
        reasoning_policy=entry.reasoning_policy,
        cache_policy=entry.cache_policy,
        filter_think_tags=_model_may_leak_think_tags(model, entry.reasoning_policy),
    )


def migrate_legacy_provider_settings(
    source: Mapping[str, Any],
    *,
    trust_local_migration: bool = True,
) -> dict[str, Any]:
    """Build v10 model provider settings without reading or moving any secret."""

    text = ProviderConfig.from_mapping(source["provider"])
    multimodal_root = source["multimodal"]
    multimodal = ProviderConfig.from_mapping(multimodal_root["provider"])
    text_enabled = bool(source.get("provider_enabled"))
    text_entry = _legacy_catalog_id(text)
    text_profile = _legacy_profile(
        text,
        catalog_id=text_entry,
        profile_id=LEGACY_CHAT_PROFILE_ID,
        credential_slot="legacy_chat",
        enabled=text_enabled,
        mark_tested=text_enabled and trust_local_migration,
        vision_model=(
            multimodal.model
            if bool(multimodal_root.get("enabled"))
            and bool(multimodal_root.get("reuse_mimo_credential"))
            and text.preset is ProviderPreset.MIMO_PAYG
            and multimodal.preset is ProviderPreset.MIMO_PAYG
            and text.credential_scope == multimodal.credential_scope
            else ""
        ),
    )
    profiles = [text_profile]
    assignments: dict[ProviderRole, str | None] = {
        ProviderRole.CONVERSATION: LEGACY_CHAT_PROFILE_ID,
        ProviderRole.SUMMARY: LEGACY_CHAT_PROFILE_ID,
        ProviderRole.MEMORY: LEGACY_CHAT_PROFILE_ID,
        ProviderRole.VISION: None,
    }
    if text_profile.model_for(ProviderRole.VISION):
        assignments[ProviderRole.VISION] = LEGACY_CHAT_PROFILE_ID
    elif bool(multimodal_root.get("enabled")):
        vision_profile = _legacy_profile(
            multimodal,
            catalog_id=_legacy_catalog_id(multimodal),
            profile_id=LEGACY_VISION_PROFILE_ID,
            credential_slot="legacy_vision",
            enabled=True,
            mark_tested=trust_local_migration,
            vision_model=multimodal.model,
        )
        profiles.append(vision_profile)
        assignments[ProviderRole.VISION] = LEGACY_VISION_PROFILE_ID
    settings = ProviderSettings(
        tuple(profiles),
        MappingProxyType(assignments),
    )
    catalog = load_provider_catalog()
    if catalog.degraded:
        for profile in settings.profiles:
            profile.validated(None)
        return settings.to_mapping()
    return settings.validated(catalog).to_mapping()


def _legacy_profile(
    config: ProviderConfig,
    *,
    catalog_id: str,
    profile_id: str,
    credential_slot: str,
    enabled: bool,
    mark_tested: bool,
    vision_model: str,
) -> ProviderProfile:
    auth = ProviderAuth.BEARER if config.auth_mode.value == "bearer" else ProviderAuth.API_KEY
    models = MappingProxyType(
        {
            ProviderRole.CONVERSATION: config.model,
            ProviderRole.SUMMARY: config.model,
            ProviderRole.MEMORY: config.model,
            ProviderRole.VISION: vision_model,
        }
    )
    empty_tests = MappingProxyType({role: "" for role in ProviderRole})
    profile = ProviderProfile(
        profile_id=profile_id,
        catalog_id=catalog_id,
        display_name=config.display_name,
        enabled=enabled,
        protocol=ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url=normalize_provider_base_url(config.base_url),
        auth=auth,
        credential_slot=credential_slot,
        models=models,
        connect_timeout_seconds=config.connect_timeout_seconds,
        request_timeout_seconds=config.request_timeout_seconds,
        first_chunk_timeout_seconds=min(config.request_timeout_seconds, 30.0),
        idle_timeout_seconds=min(config.request_timeout_seconds, 30.0),
        max_output_tokens=config.max_output_tokens,
        temperature=config.temperature,
        top_p=config.top_p,
        stream_enabled=config.stream_enabled,
        test_fingerprints=empty_tests,
    )
    if enabled and mark_tested:
        try:
            fingerprints = {
                role: profile.test_fingerprint(role) if profile.model_for(role) else ""
                for role in ProviderRole
            }
        except ProviderProfileError:
            # An unavailable Windows machine binding must fail closed without
            # preventing the rest of the local settings migration.
            fingerprints = {role: "" for role in ProviderRole}
        profile = replace(profile, test_fingerprints=MappingProxyType(fingerprints))
    return profile


@lru_cache(maxsize=1)
def current_machine_binding_digest() -> str:
    """Return a one-way device binding used only by provider test fingerprints."""

    if os.name == "nt":
        try:
            import winreg

            access = winreg.KEY_READ
            access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                access,
            ) as key:
                value, value_type = winreg.QueryValueEx(key, "MachineGuid")
            if value_type != winreg.REG_SZ or not isinstance(value, str) or not value.strip():
                raise OSError("Windows machine binding is invalid")
            material = value.strip().casefold()
        except (ImportError, OSError):
            raise ProviderProfileError("Provider machine binding is unavailable.") from None
    else:  # pragma: no cover - developer/test portability fallback
        material = f"{platform.system()}\0{platform.node()}\0{platform.machine()}"
        if not material.strip("\0"):
            raise ProviderProfileError("Provider machine binding is unavailable.")
    return hashlib.sha256(
        f"Amadeus/P7G/provider-test-binding\0{material}".encode("utf-8")
    ).hexdigest()


def _legacy_catalog_id(config: ProviderConfig) -> str:
    if config.preset is ProviderPreset.DEEPSEEK_PAYG:
        return "deepseek"
    if config.preset is ProviderPreset.MIMO_PAYG:
        return "mimo_payg"
    return "custom_openai"


def normalize_provider_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderProfileError("Provider URL is empty or not normalized.")
    if "\\" in value or any(character.isspace() for character in value):
        raise ProviderProfileError("Provider URL contains forbidden characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ProviderProfileError("Provider URL is invalid.") from None
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or not parsed.hostname:
        raise ProviderProfileError("Provider URL requires an HTTP(S) host.")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ProviderProfileError("Provider URL contains forbidden URL components.")
    if "%" in parsed.netloc:
        raise ProviderProfileError("Provider URL host must not be percent-encoded.")
    hostname = parsed.hostname.rstrip(".").casefold()
    decoded_hostname = unquote(hostname).casefold()
    if parsed.scheme == "http" and hostname not in _LOOPBACK_HOSTS:
        raise ProviderProfileError("HTTP is allowed only for exact loopback hosts.")
    if decoded_hostname.startswith("token-plan-") or ".token-plan-" in decoded_hostname:
        raise ProviderProfileError("Token Plan endpoints are not supported.")
    path = parsed.path.rstrip("/")
    decoded = unquote(path).casefold()
    if decoded.endswith("/chat/completions") or decoded.endswith("/messages"):
        raise ProviderProfileError("Enter a base URL, not a complete request endpoint.")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        if not 1 <= port <= 65535:
            raise ProviderProfileError("Provider URL port is invalid.")
        normalized_host = f"{normalized_host}:{port}"
    normalized = urlunsplit(SplitResult(parsed.scheme, normalized_host, path, "", ""))
    return normalized


def legacy_credential_ref(profile: ProviderProfile) -> str | None:
    if profile.credential_slot == "legacy_chat":
        return PROVIDER_CREDENTIAL_REF
    if profile.credential_slot == "legacy_vision":
        return MULTIMODAL_CREDENTIAL_REF
    return None


def transient_credential_fingerprint(secret: str) -> str:
    """One-way in-memory binding between a connection test and pending secret."""

    if not isinstance(secret, str):
        raise ProviderProfileError("Provider credential fingerprint input is invalid.")
    try:
        encoded = secret.encode("utf-8")
    except UnicodeEncodeError:
        raise ProviderProfileError(
            "Provider credential fingerprint input is invalid."
        ) from None
    return hashlib.sha256(b"Amadeus/P7G/connection-test\0" + encoded).hexdigest()


def _model_may_leak_think_tags(model: str, policy: ReasoningPolicy) -> bool:
    normalized = model.casefold()
    return policy is ReasoningPolicy.MINIMAX_SPLIT or any(
        name in normalized for name in ("qwen3.5", "qwen3.6", "qwen3.7")
    )


def _validate_text(value: object, maximum: int, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProviderProfileError(f"{label} is invalid.")


def _validate_number(value: object, label: str, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderProfileError(f"{label} must be numeric.")
    numeric = float(value)
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        raise ProviderProfileError(f"{label} is outside the supported range.")
