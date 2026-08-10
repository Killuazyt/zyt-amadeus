"""Fail-closed provider routing for legacy text and structured visual prompts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from threading import RLock

from amadeus_desktop.chat_models import (
    ChatRequest,
    ImagePart,
    ProviderCapability,
    ProviderRoute,
)
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ChatProvider,
    ChatProviderError,
    ProviderErrorCode,
)


class ProviderRouter:
    """Select exactly one configured provider without visual-to-text fallback."""

    def __init__(
        self,
        text_provider: ChatProvider,
        multimodal_provider: ChatProvider | None = None,
    ) -> None:
        self._lock = RLock()
        self._providers: dict[ProviderCapability, ChatProvider | None] = {
            ProviderCapability.TEXT: text_provider,
            ProviderCapability.MULTIMODAL: multimodal_provider,
        }

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

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        required = _required_route(request)
        capability = (
            ProviderCapability.MULTIMODAL
            if required is ProviderRoute.MULTIMODAL
            else ProviderCapability.TEXT
        )
        provider = self.provider(capability)
        if provider is None:
            raise ChatProviderError(ProviderErrorCode.NOT_CONFIGURED)
        async for chunk in provider.stream(request, cancellation):
            yield chunk


def _required_route(request: ChatRequest) -> ProviderRoute:
    contains_image = any(
        isinstance(message.content, tuple)
        and any(isinstance(part, ImagePart) for part in message.content)
        for message in request.messages
    )
    if contains_image:
        return ProviderRoute.MULTIMODAL
    return request.provider_route
