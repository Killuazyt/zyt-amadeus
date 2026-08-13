from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from amadeus_desktop.chat_models import (
    ChatRequest,
    ImagePart,
    PromptMessage,
    PromptRole,
    TextPart,
)
from amadeus_desktop.chat_provider import (
    _MAX_RESPONSE_BYTES,
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
    ThinkingSafeProvider,
    build_profile_provider,
)
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.provider_catalog import (
    CachePolicy,
    ProviderAuth,
    ProviderRole,
    load_provider_catalog,
)
from amadeus_desktop.provider_profiles import ProviderProfile, request_snapshot


def _request(content="hello") -> ChatRequest:
    return ChatRequest(
        request_id="p7g-request",
        turn_id="p7g-turn",
        attempt=1,
        messages=(
            PromptMessage(PromptRole.SYSTEM, "synthetic system"),
            PromptMessage(PromptRole.USER, content),
        ),
    )


def _collect(provider, request: ChatRequest | None = None) -> list[str]:
    async def run() -> list[str]:
        return [
            chunk async for chunk in provider.stream(request or _request(), CancellationToken())
        ]

    return asyncio.run(run())


class _DelayedAnthropicStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.02)
        yield b'data: {"type":"message_start","message":{"id":"msg"}}\n\n'


class _SingleChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes) -> None:
        self._chunk = chunk

    async def __aiter__(self):
        yield self._chunk


@pytest.mark.parametrize(
    "catalog_id",
    [
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
        "custom_openai",
        "ollama",
        "lm_studio",
        "vllm",
    ],
)
def test_every_openai_preset_emits_its_locked_endpoint_auth_and_token_contract(
    catalog_id,
) -> None:
    catalog = load_provider_catalog()
    entry = catalog.entry(catalog_id)
    profile = ProviderProfile.from_catalog(
        entry,
        profile_id=f"contract-{catalog_id.replace('_', '-')}",
    )
    if not profile.model_for(ProviderRole.CONVERSATION):
        profile = replace(
            profile,
            models={**profile.models, ProviderRole.CONVERSATION: "contract-model"},
        )
    profile = replace(profile, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    secret = "" if profile.auth is ProviderAuth.NONE else "invalid-fake-provider-key"
    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore(secret) if secret else InMemoryCredentialStore(),
        transport=httpx.MockTransport(handler),
    )

    assert _collect(provider) == ["ok"]
    assert str(captured[0].url) == f"{profile.base_url}/chat/completions"
    body = json.loads(captured[0].content)
    assert body["model"] == profile.model_for(ProviderRole.CONVERSATION)
    assert snapshot.token_limit_field in body
    assert "tools" not in body
    assert "web_search" not in body
    if profile.auth is ProviderAuth.BEARER:
        assert captured[0].headers["authorization"] == "Bearer invalid-fake-provider-key"
    elif profile.auth is ProviderAuth.API_KEY:
        assert captured[0].headers["api-key"] == "invalid-fake-provider-key"
    else:
        assert "authorization" not in captured[0].headers
        assert "api-key" not in captured[0].headers


def test_dashscope_cache_header_is_whitelisted_and_disabled_by_default() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("qwen_cn"), profile_id="qwen-cache")
    profile = replace(profile, cache_enabled=True, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.SUMMARY, catalog)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-provider-key"),
        transport=httpx.MockTransport(handler),
    )

    _collect(provider)
    assert snapshot.cache_policy is CachePolicy.DASHSCOPE_SESSION
    assert captured[0].headers["x-dashscope-session-cache"] == "enable"
    assert set(captured[0].headers).isdisjoint({"x-custom-cache", "extra-headers"})

    captured.clear()
    disabled = request_snapshot(
        replace(profile, cache_enabled=False), ProviderRole.SUMMARY, catalog
    )
    _collect(
        build_profile_provider(
            disabled,
            InMemoryCredentialStore("invalid-fake-provider-key"),
            transport=httpx.MockTransport(handler),
        )
    )
    assert "x-dashscope-session-cache" not in captured[0].headers


def test_anthropic_messages_converts_system_text_image_and_nonstream_response() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-vision"
    )
    profile = replace(profile, cache_enabled=True, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.VISION, catalog)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "content": [{"type": "text", "text": "seen"}],
                "stop_reason": "end_turn",
            },
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-anthropic-key"),
        transport=httpx.MockTransport(handler),
    )
    content = (
        TextPart("inspect fixture"),
        ImagePart("fixture", "data:image/png;base64,iVBORw0KGgo="),
    )

    assert _collect(provider, _request(content)) == ["seen"]
    assert str(captured[0].url) == "https://api.anthropic.com/v1/messages"
    assert captured[0].headers["x-api-key"] == "invalid-fake-anthropic-key"
    assert captured[0].headers["anthropic-version"] == "2023-06-01"
    body = json.loads(captured[0].content)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    image = body["messages"][0]["content"][1]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert "thinking" not in body
    assert "tools" not in body


@pytest.mark.parametrize("catalog_id", ["openai", "anthropic"])
def test_protocols_reject_malformed_image_base64_before_transport(catalog_id) -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry(catalog_id), profile_id=f"{catalog_id}-bad-image"
    )
    snapshot = request_snapshot(profile, ProviderRole.VISION, catalog)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {}})

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-provider-key"),
        transport=httpx.MockTransport(handler),
    )
    content = (
        TextPart("inspect fixture"),
        ImagePart("fixture", "data:image/png;base64,not*base64"),
    )

    with pytest.raises(ChatProviderError) as caught:
        _collect(provider, _request(content))
    assert caught.value.code is ProviderErrorCode.MODEL_OR_PARAMETER
    assert calls == 0


def test_anthropic_sse_is_converted_to_plain_visible_chunks() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-stream"
    )
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    events = (
        b'data: {"type":"message_start","message":{"id":"msg"}}\n\n'
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text"}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"A"}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"B"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=events,
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-anthropic-key"),
        transport=httpx.MockTransport(handler),
    )

    assert _collect(provider) == ["A", "B"]


def test_anthropic_stream_maps_max_token_termination_to_a_safe_model_error() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-max-tokens"
    )
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    events = (
        b'data: {"type":"message_start","message":{"id":"msg"}}\n\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=events,
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-anthropic-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatProviderError) as caught:
        _collect(provider)
    assert caught.value.code is ProviderErrorCode.MODEL_OR_PARAMETER


def test_anthropic_stream_rejects_content_before_message_start_and_boolean_index() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-state-machine"
    )
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    invalid_streams = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n',
        'data: {"type":"message_start","message":{"id":"msg"}}\n\n'
        'data: {"type":"content_block_start","index":true,'
        '"content_block":{"type":"text"}}\n\n',
    )

    for events in invalid_streams:

        async def handler(
            _request: httpx.Request,
            body: bytes = events.encode(),
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
            )

        provider = build_profile_provider(
            snapshot,
            InMemoryCredentialStore("invalid-fake-anthropic-key"),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(ChatProviderError) as caught:
            _collect(provider)
        assert caught.value.code is ProviderErrorCode.PROTOCOL


def test_anthropic_stream_uses_the_captured_first_chunk_timeout() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-timeout"
    )
    snapshot = replace(
        request_snapshot(profile, ProviderRole.CONVERSATION, catalog),
        first_chunk_timeout_seconds=0.001,
        request_timeout_seconds=1.0,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_DelayedAnthropicStream(),
            request=request,
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-anthropic-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatProviderError) as caught:
        _collect(provider)
    assert caught.value.code is ProviderErrorCode.TIMEOUT


def test_anthropic_nonstream_response_has_the_shared_hard_byte_limit() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("anthropic"), profile_id="anthropic-response-limit"
    )
    profile = replace(profile, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_SingleChunkStream(b"x" * (_MAX_RESPONSE_BYTES + 1)),
            request=request,
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-anthropic-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatProviderError) as caught:
        _collect(provider)
    assert caught.value.code is ProviderErrorCode.PROTOCOL


@pytest.mark.parametrize("catalog_id", ["openai", "anthropic"])
def test_nonstream_protocols_reject_non_json_success_responses(catalog_id) -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry(catalog_id), profile_id=f"{catalog_id}-content-type"
    )
    profile = replace(profile, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"not-a-provider-json-response",
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-provider-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatProviderError) as caught:
        _collect(provider)
    assert caught.value.code is ProviderErrorCode.PROTOCOL


def test_custom_anthropic_bearer_contract_and_thinking_blocks_are_not_exposed() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("custom_anthropic"), profile_id="custom-anthropic"
    )
    profile = replace(
        profile,
        base_url="https://anthropic-compatible.example.invalid/v1",
        auth=ProviderAuth.BEARER,
        stream_enabled=False,
    )
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "visible"},
                ],
                "stop_reason": "end_turn",
            },
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-anthropic-key"),
        transport=httpx.MockTransport(handler),
    )

    assert _collect(provider) == ["visible"]
    assert captured[0].headers["authorization"] == "Bearer invalid-fake-anthropic-key"
    assert "x-api-key" not in captured[0].headers


def test_custom_anthropic_loopback_no_auth_emits_no_credential_header() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(
        catalog.entry("custom_anthropic"), profile_id="custom-anthropic-local"
    )
    profile = replace(
        profile,
        base_url="http://127.0.0.1:8000/v1",
        auth=ProviderAuth.NONE,
        stream_enabled=False,
    )
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "content": [{"type": "text", "text": "visible"}],
                "stop_reason": "end_turn",
            },
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore(),
        transport=httpx.MockTransport(handler),
    )

    assert _collect(provider) == ["visible"]
    assert "authorization" not in captured[0].headers
    assert "x-api-key" not in captured[0].headers


@pytest.mark.parametrize(
    "catalog_id,status_code,expected_code",
    [
        ("openai", 401, ProviderErrorCode.AUTHENTICATION),
        ("anthropic", 429, ProviderErrorCode.RATE_LIMIT),
        ("anthropic", 503, ProviderErrorCode.SERVER),
    ],
)
def test_profile_protocols_map_http_failures_without_exposing_provider_bodies(
    catalog_id, status_code, expected_code
) -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry(catalog_id), profile_id="errors-main")
    profile = replace(profile, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": "application/json"},
            json={"error": {"message": "invalid-fake-private-provider-detail"}},
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-provider-key"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatProviderError) as caught:
        _collect(provider)
    assert caught.value.code is expected_code
    assert "private-provider-detail" not in str(caught.value)


def test_profile_provider_honors_preflight_cancellation() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("anthropic"), profile_id="cancel-main")
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)
    token = CancellationToken()
    token.cancel()

    async def run() -> None:
        provider = build_profile_provider(
            snapshot,
            InMemoryCredentialStore("invalid-fake-provider-key"),
            transport=httpx.MockTransport(lambda _request: httpx.Response(500, json={"error": {}})),
        )
        async for _chunk in provider.stream(_request(), token):
            pass

    with pytest.raises(CancellationRequested):
        asyncio.run(run())


def test_openai_independent_reasoning_field_is_ignored() -> None:
    catalog = load_provider_catalog()
    profile = ProviderProfile.from_catalog(catalog.entry("openai"), profile_id="reason-main")
    profile = replace(profile, stream_enabled=False)
    snapshot = request_snapshot(profile, ProviderRole.CONVERSATION, catalog)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [
                    {
                        "message": {
                            "content": "visible",
                            "reasoning_content": "private",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = build_profile_provider(
        snapshot,
        InMemoryCredentialStore("invalid-fake-provider-key"),
        transport=httpx.MockTransport(handler),
    )
    assert _collect(provider) == ["visible"]


def test_thinking_filter_removes_only_a_leading_think_block_across_chunks() -> None:
    class _LeakingProvider:
        async def stream(self, _request, _cancellation):
            for chunk in ("<thi", "nk>private", " chain</think>\n", "visible"):
                yield chunk

    assert _collect(ThinkingSafeProvider(_LeakingProvider())) == ["visible"]


def test_thinking_filter_rejects_an_unfinished_think_prefix() -> None:
    class _TruncatedProvider:
        async def stream(self, _request, _cancellation):
            yield "<thi"

    with pytest.raises(ChatProviderError) as caught:
        _collect(ThinkingSafeProvider(_TruncatedProvider()))
    assert caught.value.code is ProviderErrorCode.PROTOCOL
