"""Fail-closed P7G task routing with immutable provider request snapshots."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from threading import RLock

from amadeus_desktop.chat_models import (
    ChatRequest,
    GenerationPurpose,
    ImagePart,
    ProviderCapability,
    ProviderRoute,
)
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ChatProvider,
    ChatProviderError,
    ProviderErrorCode,
    build_profile_provider,
)
from amadeus_desktop.credential_store import CredentialStore
from amadeus_desktop.provider_catalog import ProviderCatalog, ProviderRole
from amadeus_desktop.provider_profiles import (
    ProviderRequestSnapshot,
    ProviderSettings,
    request_snapshot,
)

CredentialStoreFactory = Callable[[object], CredentialStore]


class ProviderRouter:
    """Choose one role/profile; never retry or fall back to a different profile."""

    def __init__(
        self,
        text_provider: ChatProvider | None = None,
        multimodal_provider: ChatProvider | None = None,
        *,
        provider_settings: ProviderSettings | None = None,
        catalog: ProviderCatalog | None = None,
        credential_store_factory: CredentialStoreFactory | None = None,
    ) -> None:
        self._lock = RLock()
        self._providers: dict[ProviderCapability, ChatProvider | None] = {
            ProviderCapability.TEXT: text_provider,
            ProviderCapability.MULTIMODAL: multimodal_provider,
        }
        self._provider_settings = provider_settings
        self._catalog = catalog
        self._credential_store_factory = credential_store_factory
        self._last_snapshot_by_request: dict[str, ProviderRequestSnapshot] = {}

    def replace_profiles(
        self,
        provider_settings: ProviderSettings,
        catalog: ProviderCatalog,
        credential_store_factory: CredentialStoreFactory,
    ) -> None:
        """Atomically replace task assignments after durable settings commit."""

        provider_settings.runtime_validated(catalog)
        with self._lock:
            self._provider_settings = provider_settings
            self._catalog = catalog
            self._credential_store_factory = credential_store_factory

    def set_provider(
        self,
        capability: ProviderCapability,
        provider: ChatProvider | None,
    ) -> None:
        if capability is ProviderCapability.TEXT and provider is None:
            raise ValueError("text provider cannot be removed")
        with self._lock:
            self._providers[ProviderCapability(capability)] = provider

    def provider(self, capability: ProviderCapability) -> ChatProvider | None:
        with self._lock:
            return self._providers[ProviderCapability(capability)]

    def captured_snapshot(self, request_id: str) -> ProviderRequestSnapshot | None:
        with self._lock:
            return self._last_snapshot_by_request.get(request_id)

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        role = required_role(request)
        provider, routed_request = self._capture_provider(request, role)
        async for chunk in provider.stream(routed_request, cancellation):
            yield chunk

    def _capture_provider(
        self,
        request: ChatRequest,
        role: ProviderRole,
    ) -> tuple[ChatProvider, ChatRequest]:
        with self._lock:
            settings = self._provider_settings
            catalog = self._catalog
            factory = self._credential_store_factory
            if settings is None or catalog is None or factory is None:
                capability = (
                    ProviderCapability.MULTIMODAL
                    if role is ProviderRole.VISION
                    else ProviderCapability.TEXT
                )
                provider = self._providers[capability]
                if provider is None:
                    raise ChatProviderError(ProviderErrorCode.NOT_CONFIGURED)
                return provider, request
            profile = settings.assigned_profile(role)
            if profile is None or not profile.enabled or not profile.is_tested(role):
                raise ChatProviderError(ProviderErrorCode.NOT_CONFIGURED)
            if profile.catalog_id not in catalog.entries:
                raise ChatProviderError(ProviderErrorCode.NOT_CONFIGURED)
            snapshot = request_snapshot(profile, role, catalog)
            credential_store = factory(profile)
            provider = build_profile_provider(snapshot, credential_store)
            self._last_snapshot_by_request[request.request_id] = snapshot
            if len(self._last_snapshot_by_request) > 256:
                oldest = next(iter(self._last_snapshot_by_request))
                self._last_snapshot_by_request.pop(oldest, None)
        routed_request = replace(
            request,
            provider_role=role.value,
        )
        return provider, routed_request


def required_role(request: ChatRequest) -> ProviderRole:
    """Apply the locked P7G routing table with visual evidence taking priority."""

    contains_image = any(
        isinstance(message.content, tuple)
        and any(isinstance(part, ImagePart) for part in message.content)
        for message in request.messages
    )
    if contains_image or request.provider_route is ProviderRoute.MULTIMODAL:
        return ProviderRole.VISION
    if request.provider_role is not None:
        try:
            explicit = ProviderRole(request.provider_role)
        except ValueError:
            raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER) from None
        if explicit is ProviderRole.VISION:
            return ProviderRole.VISION
        return explicit
    purpose = request.options.purpose
    if purpose is GenerationPurpose.CONVERSATION_SUMMARY:
        return ProviderRole.SUMMARY
    if purpose in {
        GenerationPurpose.MEMORY_EXTRACTION,
        GenerationPurpose.STRUCTURE_REPAIR,
        GenerationPurpose.MEMORY_EVIDENCE,
        GenerationPurpose.REFLECTION_SYNTHESIS,
        GenerationPurpose.PERSONA_PROMOTION,
    }:
        return ProviderRole.MEMORY
    return ProviderRole.CONVERSATION


def _required_route(request: ChatRequest) -> ProviderRoute:
    """Compatibility helper retained for P7C callers and tests."""

    return (
        ProviderRoute.MULTIMODAL
        if required_role(request) is ProviderRole.VISION
        else ProviderRoute.TEXT
    )
