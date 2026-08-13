from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType

import httpx
import pytest

from amadeus_desktop.chat_models import (
    ChatRequest,
    GenerationOptions,
    GenerationPurpose,
    ImagePart,
    PromptMessage,
    PromptRole,
    TextPart,
)
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
)
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.provider_catalog import ProviderAuth, ProviderRole, load_provider_catalog
from amadeus_desktop.provider_profiles import ProviderProfile, ProviderSettings
from amadeus_desktop.provider_router import ProviderRouter, required_role


def _request(
    request_id: str,
    *,
    purpose: GenerationPurpose = GenerationPurpose.MAIN_CONVERSATION,
    content="hello",
    provider_role: str | None = None,
) -> ChatRequest:
    return ChatRequest(
        request_id=request_id,
        turn_id=request_id,
        attempt=1,
        messages=(PromptMessage(PromptRole.USER, content),),
        options=GenerationOptions(purpose=purpose),
        provider_role=provider_role,
    )


@pytest.mark.parametrize(
    "chat_request,expected",
    [
        (_request("conversation"), ProviderRole.CONVERSATION),
        (
            _request("summary", purpose=GenerationPurpose.CONVERSATION_SUMMARY),
            ProviderRole.SUMMARY,
        ),
        (
            _request("memory", purpose=GenerationPurpose.MEMORY_EXTRACTION),
            ProviderRole.MEMORY,
        ),
        (
            _request("reflection", purpose=GenerationPurpose.REFLECTION_SYNTHESIS),
            ProviderRole.MEMORY,
        ),
        (
            _request("greeting", purpose=GenerationPurpose.PROACTIVE_GREETING),
            ProviderRole.CONVERSATION,
        ),
    ],
)
def test_locked_task_routes(chat_request, expected) -> None:
    assert required_role(chat_request) is expected


def test_image_content_has_priority_over_an_explicit_text_role() -> None:
    content = (
        TextPart("look"),
        ImagePart("fixture", "data:image/png;base64,iVBORw0KGgo="),
    )
    request = _request("vision", content=content, provider_role="memory")

    assert required_role(request) is ProviderRole.VISION


def _settings() -> tuple[ProviderSettings, object]:
    catalog = load_provider_catalog()
    conversation = ProviderProfile.from_catalog(
        catalog.entry("custom_openai"), profile_id="conversation-main"
    )
    conversation = replace(
        conversation,
        display_name="Conversation Profile",
        enabled=True,
        base_url="https://conversation.invalid/v1",
        auth=ProviderAuth.BEARER,
        models=MappingProxyType(
            {
                ProviderRole.CONVERSATION: "conversation-model",
                ProviderRole.SUMMARY: "summary-model",
                ProviderRole.MEMORY: "memory-model",
                ProviderRole.VISION: "vision-model",
            }
        ),
    )
    conversation = replace(
        conversation,
        test_fingerprints=MappingProxyType(
            {role: conversation.test_fingerprint(role) for role in ProviderRole}
        ),
    )
    vision = replace(
        conversation,
        profile_id="vision-main",
        display_name="Vision Profile",
        base_url="https://vision.invalid/v1",
    )
    vision = replace(
        vision,
        test_fingerprints=MappingProxyType(
            {role: vision.test_fingerprint(role) for role in ProviderRole}
        ),
    )
    settings = ProviderSettings(
        (conversation, vision),
        MappingProxyType(
            {
                ProviderRole.CONVERSATION: conversation.profile_id,
                ProviderRole.SUMMARY: conversation.profile_id,
                ProviderRole.MEMORY: conversation.profile_id,
                ProviderRole.VISION: vision.profile_id,
            }
        ),
    ).validated(catalog)
    return settings, catalog


def _collect(router: ProviderRouter, request: ChatRequest) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in router.stream(request, CancellationToken())]

    return asyncio.run(run())


def test_route_captures_actual_profile_model_and_never_cross_profile_fails_over(
    monkeypatch,
) -> None:
    settings, catalog = _settings()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503, json={"error": {"type": "server_error"}})

    class _StoreFactory:
        def __call__(self, _profile):
            return InMemoryCredentialStore("invalid-fake-profile-key")

    router = ProviderRouter(
        provider_settings=settings,
        catalog=catalog,
        credential_store_factory=_StoreFactory(),
    )
    # Inject a deterministic transport through the request factory boundary.
    import amadeus_desktop.provider_router as router_module

    def build(snapshot, store):
        from amadeus_desktop.chat_provider import build_profile_provider

        return build_profile_provider(snapshot, store, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(router_module, "build_profile_provider", build)
    with pytest.raises(ChatProviderError) as captured:
        _collect(router, _request("failed-conversation"))

    snapshot = router.captured_snapshot("failed-conversation")
    assert captured.value.code is ProviderErrorCode.SERVER
    assert snapshot is not None
    assert snapshot.profile_id == "conversation-main"
    assert snapshot.provider_name == "Conversation Profile"
    assert snapshot.model == "conversation-model"
    assert calls == ["https://conversation.invalid/v1/chat/completions"]


def test_disabled_or_untested_assignment_is_not_configured_without_fallback() -> None:
    settings, catalog = _settings()
    conversation = settings.assigned_profile(ProviderRole.CONVERSATION)
    assert conversation is not None
    untested = replace(
        conversation,
        test_fingerprints=MappingProxyType({role: "" for role in ProviderRole}),
    )
    blocked = ProviderSettings(
        (untested, settings.profiles[1]),
        settings.assignments,
    ).validated(catalog)
    router = ProviderRouter(
        provider_settings=blocked,
        catalog=catalog,
        credential_store_factory=lambda _profile: InMemoryCredentialStore(
            "invalid-fake-profile-key"
        ),
    )

    with pytest.raises(ChatProviderError) as captured:
        _collect(router, _request("untested"))

    assert captured.value.code is ProviderErrorCode.NOT_CONFIGURED
    assert router.captured_snapshot("untested") is None
