from __future__ import annotations

import asyncio

import pytest

from amadeus_desktop.chat_models import (
    ChatRequest,
    ImagePart,
    PromptMessage,
    PromptRole,
    ProviderCapability,
    ProviderRoute,
    TextPart,
)
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
)
from amadeus_desktop.provider_router import ProviderRouter


class _Provider:
    def __init__(self, label: str) -> None:
        self.label = label
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest, _cancellation: CancellationToken):
        self.requests.append(request)
        yield self.label


def _collect(router: ProviderRouter, request: ChatRequest) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in router.stream(request, CancellationToken())]

    return asyncio.run(run())


def _request(content, *, route: ProviderRoute = ProviderRoute.TEXT) -> ChatRequest:
    return ChatRequest(
        request_id="request",
        turn_id="turn",
        attempt=1,
        messages=(PromptMessage(PromptRole.USER, content),),
        provider_route=route,
    )


def test_legacy_text_uses_text_provider() -> None:
    text = _Provider("text")
    multimodal = _Provider("vision")
    router = ProviderRouter(text, multimodal)

    assert _collect(router, _request("hello")) == ["text"]
    assert len(text.requests) == 1
    assert not multimodal.requests


def test_image_content_forces_multimodal_even_if_route_was_text() -> None:
    text = _Provider("text")
    multimodal = _Provider("vision")
    router = ProviderRouter(text, multimodal)
    content = (
        TextPart("what is visible"),
        ImagePart("volatile:image", "data:image/png;base64,iVBORw0KGgo="),
    )

    assert _collect(router, _request(content)) == ["vision"]
    assert not text.requests
    assert len(multimodal.requests) == 1


def test_explicit_multimodal_route_never_falls_back_to_text() -> None:
    text = _Provider("text")
    router = ProviderRouter(text)

    with pytest.raises(ChatProviderError) as captured:
        _collect(router, _request("visual request", route=ProviderRoute.MULTIMODAL))

    assert captured.value.code is ProviderErrorCode.NOT_CONFIGURED
    assert not text.requests


def test_text_provider_cannot_be_removed_but_multimodal_can() -> None:
    text = _Provider("text")
    router = ProviderRouter(text, _Provider("vision"))

    router.set_provider(ProviderCapability.MULTIMODAL, None)
    assert router.provider(ProviderCapability.MULTIMODAL) is None
    with pytest.raises(ValueError):
        router.set_provider(ProviderCapability.TEXT, None)
