from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest

from amadeus_desktop.chat_models import (
    ChatRequest,
    GenerationOptions,
    GenerationPurpose,
    PromptMessage,
    PromptRole,
)
from amadeus_desktop.chat_provider import (
    _MAX_RESPONSE_BYTES,
    _MAX_SSE_EVENT_CHARS,
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    OpenAICompatibleChatProvider,
    ProviderConnectionTester,
    ProviderErrorCode,
    UnconfiguredChatProvider,
)
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.provider_config import (
    AuthMode,
    ProviderConfig,
    ProviderPreset,
    TokenLimitField,
)


def request() -> ChatRequest:
    return ChatRequest(
        request_id="fake-request",
        turn_id="fake-turn",
        attempt=1,
        messages=(
            PromptMessage(PromptRole.SYSTEM, "fake system"),
            PromptMessage(PromptRole.USER, "fake user"),
        ),
    )


def sse(*events: str, done: bool = True) -> bytes:
    parts = [f"data: {event}\n\n" for event in events]
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


SSE_HEADERS = {"content-type": "text/event-stream; charset=utf-8"}


def collect(provider, token: CancellationToken | None = None) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in provider.stream(request(), token or CancellationToken())]

    return asyncio.run(run())


@pytest.mark.parametrize(
    "options",
    [
        {"temperature": True},
        {"temperature": float("nan")},
        {"temperature": "0.1"},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
        {"max_output_tokens": 0},
    ],
)
def test_generation_options_reject_non_strict_numbers(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        GenerationOptions(**options)  # type: ignore[arg-type]


def test_background_generation_options_override_within_configured_cap() -> None:
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
    )
    background_request = replace(
        request(),
        options=GenerationOptions(
            purpose=GenerationPurpose.MEMORY_EXTRACTION,
            temperature=0.1,
            max_output_tokens=256,
        ),
    )

    payload = provider._build_payload(background_request)

    assert payload["temperature"] == 0.1
    assert payload[ProviderConfig.default().token_limit_field.value] == 256


@pytest.mark.parametrize(
    ("preset", "auth_header", "limit_field"),
    [
        (ProviderPreset.DEEPSEEK_PAYG, "Authorization", "max_tokens"),
        (ProviderPreset.MIMO_PAYG, "api-key", "max_completion_tokens"),
    ],
)
def test_builtin_payload_auth_limit_and_thinking_disabled(
    preset: ProviderPreset,
    auth_header: str,
    limit_field: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["headers"] = http_request.headers
        captured["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(json.dumps({"choices": [{"delta": {"content": "好"}}]})),
        )

    config = ProviderConfig.for_preset(preset)
    provider = OpenAICompatibleChatProvider(
        config,
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )

    assert collect(provider) == ["好"]
    headers = captured["headers"]
    payload = captured["payload"]
    assert isinstance(headers, httpx.Headers)
    assert isinstance(payload, dict)
    assert auth_header in headers
    assert headers["Accept-Encoding"] == "identity"
    assert limit_field in payload
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][0]["role"] == "system"


def test_custom_payload_has_no_private_thinking_parameter() -> None:
    captured: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(
            200,
            headers=SSE_HEADERS,
            content=sse(json.dumps({"choices": [{"delta": {"content": "ok"}}]})),
        )

    config = ProviderConfig.for_preset(
        ProviderPreset.CUSTOM_OPENAI,
        auth_mode=AuthMode.API_KEY,
        token_limit_field=TokenLimitField.MAX_COMPLETION_TOKENS,
    )
    provider = OpenAICompatibleChatProvider(
        config,
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )

    assert collect(provider) == ["ok"]
    assert "thinking" not in captured
    assert "max_completion_tokens" in captured


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class ObservableBlockingStream(httpx.AsyncByteStream):
    def __init__(self, prefix: tuple[bytes, ...] = ()) -> None:
        self.prefix = prefix
        self.blocked = threading.Event()
        self.close_count = 0

    async def __aiter__(self):
        for chunk in self.prefix:
            yield chunk
        self.blocked.set()
        await asyncio.Future()

    async def aclose(self) -> None:
        self.close_count += 1


class ObservableTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self.stream = stream
        self.request_count = 0
        self.close_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        return httpx.Response(200, headers=SSE_HEADERS, stream=self.stream, request=request)

    async def aclose(self) -> None:
        self.close_count += 1


def test_sse_utf8_fragments_ignore_heartbeat_reasoning_and_usage() -> None:
    body = sse(
        json.dumps({"choices": [{"delta": {"role": "assistant", "content": ""}}]}),
        json.dumps({"choices": [{"delta": {"reasoning_content": "hidden"}}]}),
        json.dumps({"choices": [{"delta": {"content": "中文"}}]}, ensure_ascii=False),
        json.dumps({"choices": [], "usage": {"total_tokens": 3}}),
        json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    )
    marker = body.index("中".encode()) + 1
    chunks = (b": heartbeat\n\n" + body[:marker], body[marker : marker + 1], body[marker + 1 :])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=SSE_HEADERS, stream=FragmentedStream(chunks))

    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )
    assert collect(provider) == ["中文"]


def test_non_stream_completion_parses_visible_content_only() -> None:
    config = replace(ProviderConfig.default(), stream_enabled=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "完整回答", "reasoning_content": "hidden"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = OpenAICompatibleChatProvider(
        config,
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )
    assert collect(provider) == ["完整回答"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderErrorCode.AUTHENTICATION),
        (402, ProviderErrorCode.INSUFFICIENT_BALANCE),
        (400, ProviderErrorCode.MODEL_OR_PARAMETER),
        (408, ProviderErrorCode.TIMEOUT),
        (429, ProviderErrorCode.RATE_LIMIT),
        (503, ProviderErrorCode.SERVER),
    ],
)
def test_http_statuses_are_normalized_without_raw_body(
    status: int,
    expected: ProviderErrorCode,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "private raw body"}})

    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ChatProviderError) as caught:
        collect(provider)
    assert caught.value.code is expected
    assert "private raw body" not in str(caught.value)


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"delta": {}, "finish_reason": "length"}, ProviderErrorCode.MODEL_OR_PARAMETER),
        ({"delta": {}, "finish_reason": "content_filter"}, ProviderErrorCode.CONTENT_FILTER),
        (
            {"delta": {"tool_calls": [{"id": "fake"}]}},
            ProviderErrorCode.PROTOCOL,
        ),
        ({"delta": {"function_call": {"name": "fake"}}}, ProviderErrorCode.PROTOCOL),
        ({"delta": {"refusal": "blocked"}}, ProviderErrorCode.CONTENT_FILTER),
    ],
)
def test_unsafe_finish_reasons_fail_closed(choice: dict, expected: ProviderErrorCode) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=SSE_HEADERS,
            content=sse(json.dumps({"choices": [choice]})),
        )

    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ChatProviderError) as caught:
        collect(provider)
    assert caught.value.code is expected


def test_missing_done_and_malformed_json_are_protocol_errors() -> None:
    for body in (sse("not-json"), sse(json.dumps({"choices": [{"delta": {}}]}), done=False)):
        provider = OpenAICompatibleChatProvider(
            ProviderConfig.default(),
            InMemoryCredentialStore("invalid-fake-key"),
            transport=httpx.MockTransport(
                lambda _, response_body=body: httpx.Response(
                    200,
                    headers=SSE_HEADERS,
                    content=response_body,
                )
            ),
        )
        with pytest.raises(ChatProviderError) as caught:
            collect(provider)
        assert caught.value.code is ProviderErrorCode.PROTOCOL


def test_cross_thread_cancel_interrupts_blocked_transport() -> None:
    started = threading.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Future()
        raise AssertionError

    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )
    token = CancellationToken()
    timer = threading.Thread(target=lambda: (started.wait(1), token.cancel()))
    timer.start()
    with pytest.raises(CancellationRequested):
        collect(provider, token)
    timer.join(timeout=1)
    assert not timer.is_alive()


def test_cancel_before_send_opens_no_client_or_connection() -> None:
    transport = ObservableTransport(ObservableBlockingStream())
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=transport,
    )
    token = CancellationToken()
    token.cancel()

    with pytest.raises(CancellationRequested):
        collect(provider, token)

    assert transport.request_count == 0
    assert transport.close_count == 0


@pytest.mark.parametrize("with_partial", [False, True])
def test_cancel_closes_stream_and_client_before_or_after_first_content(
    with_partial: bool,
) -> None:
    prefix = ()
    if with_partial:
        prefix = (sse(json.dumps({"choices": [{"delta": {"content": "部分"}}]}), done=False),)
    stream = ObservableBlockingStream(prefix)
    transport = ObservableTransport(stream)
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=transport,
    )
    token = CancellationToken()
    chunks: list[str] = []
    timer = threading.Thread(target=lambda: (stream.blocked.wait(1), token.cancel()))
    timer.start()

    async def run() -> None:
        async for chunk in provider.stream(request(), token):
            chunks.append(chunk)

    started = time.perf_counter()
    with pytest.raises(CancellationRequested):
        asyncio.run(run())
    elapsed = time.perf_counter() - started
    timer.join(timeout=1)

    assert chunks == (["部分"] if with_partial else [])
    assert not timer.is_alive()
    assert elapsed < 2
    assert stream.close_count == 1
    assert transport.close_count == 1


def test_redirect_is_not_followed_and_wrong_content_type_fails_closed() -> None:
    seen_hosts: list[str] = []

    def redirect_handler(http_request: httpx.Request) -> httpx.Response:
        seen_hosts.append(http_request.url.host)
        return httpx.Response(307, headers={"location": "https://redirect.invalid/private"})

    redirecting = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(redirect_handler),
    )
    with pytest.raises(ChatProviderError) as redirect_error:
        collect(redirecting)
    assert redirect_error.value.code is ProviderErrorCode.PROTOCOL
    assert seen_hosts == ["api.deepseek.com"]

    wrong_type = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=sse(json.dumps({"choices": [{"delta": {"content": "no"}}]})),
            )
        ),
    )
    with pytest.raises(ChatProviderError) as content_type_error:
        collect(wrong_type)
    assert content_type_error.value.code is ProviderErrorCode.PROTOCOL


def test_empty_events_and_multiline_data_are_parsed_without_leaking_control_fields() -> None:
    body = (
        b"event: ping\ndata:\n\n"
        b'data: {"choices":\n'
        b'data: [{"delta": {"content": "multi"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=body)
        ),
    )
    assert collect(provider) == ["multi"]


def test_remote_protocol_error_after_partial_content_closes_response() -> None:
    class BrokenStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            yield sse(
                json.dumps({"choices": [{"delta": {"content": "partial"}}]}),
                done=False,
            )
            raise httpx.RemoteProtocolError("invalid-test-disconnect")

        async def aclose(self) -> None:
            self.closed = True

    stream = BrokenStream()
    transport = ObservableTransport(stream)
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=transport,
    )
    chunks: list[str] = []

    async def run() -> None:
        async for chunk in provider.stream(request(), CancellationToken()):
            chunks.append(chunk)

    with pytest.raises(ChatProviderError) as caught:
        asyncio.run(run())
    assert caught.value.code is ProviderErrorCode.PROTOCOL
    assert chunks == ["partial"]
    assert stream.closed
    assert transport.close_count == 1


@pytest.mark.parametrize(
    ("transport_error", "expected"),
    [
        (httpx.ReadTimeout("fake timeout"), ProviderErrorCode.TIMEOUT),
        (httpx.ConnectError("fake network"), ProviderErrorCode.NETWORK),
    ],
)
def test_transport_failures_are_normalized(
    transport_error: Exception,
    expected: ProviderErrorCode,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise transport_error

    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ChatProviderError) as caught:
        collect(provider)
    assert caught.value.code is expected
    assert "fake" not in str(caught.value)


def test_missing_credential_fails_before_transport() -> None:
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore(),
        transport=httpx.MockTransport(lambda _: pytest.fail("network must not run")),
    )
    with pytest.raises(ChatProviderError) as caught:
        collect(provider)
    assert caught.value.code is ProviderErrorCode.CREDENTIAL


def test_connection_tester_uses_candidate_secret_without_persisting() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.headers["Authorization"] == "Bearer invalid-candidate-key"
        return httpx.Response(
            200,
            headers=SSE_HEADERS,
            content=sse(json.dumps({"choices": [{"delta": {"content": "连接正常"}}]})),
        )

    tester = ProviderConnectionTester(transport=httpx.MockTransport(handler))

    async def run():
        return await tester.test(
            ProviderConfig.default(),
            "invalid-candidate-key",
            CancellationToken(),
        )

    result = asyncio.run(run())
    assert result.preset is ProviderPreset.DEEPSEEK_PAYG
    assert result.model == "deepseek-v4-flash"
    assert result.elapsed_ms >= 0


def test_all_error_codes_have_fixed_nonempty_safe_messages() -> None:
    messages = {code: str(ChatProviderError(code)) for code in ProviderErrorCode}
    assert set(messages) == set(ProviderErrorCode)
    assert all(message and "fake" not in message for message in messages.values())


def test_unconfigured_provider_and_legacy_error_text_fail_closed() -> None:
    with pytest.raises(ChatProviderError) as caught:
        collect(UnconfiguredChatProvider())
    assert caught.value.code is ProviderErrorCode.NOT_CONFIGURED

    legacy = ChatProviderError("private raw provider response")
    assert legacy.code is ProviderErrorCode.PROTOCOL
    assert "private raw provider response" not in str(legacy)


def test_nonstream_response_body_has_a_hard_byte_limit() -> None:
    config = replace(ProviderConfig.default(), stream_enabled=False)
    oversized = b"x" * (_MAX_RESPONSE_BYTES + 1)
    provider = OpenAICompatibleChatProvider(
        config,
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=FragmentedStream((oversized,)))
        ),
    )

    with pytest.raises(ChatProviderError) as caught:
        collect(provider)

    assert caught.value.code is ProviderErrorCode.PROTOCOL


def test_stream_event_and_visible_output_have_hard_limits() -> None:
    oversized_event = b"data: " + (b"x" * (_MAX_SSE_EVENT_CHARS + 1)) + b"\n\n"
    event_provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=SSE_HEADERS, content=oversized_event)
        ),
    )
    with pytest.raises(ChatProviderError) as event_error:
        collect(event_provider)
    assert event_error.value.code is ProviderErrorCode.PROTOCOL

    content = "x" * 4_097
    output_provider = OpenAICompatibleChatProvider(
        replace(ProviderConfig.default(), max_output_tokens=1),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers=SSE_HEADERS,
                content=sse(json.dumps({"choices": [{"delta": {"content": content}}]})),
            )
        ),
    )
    with pytest.raises(ChatProviderError) as output_error:
        collect(output_provider)
    assert output_error.value.code is ProviderErrorCode.PROTOCOL


def test_compressed_response_is_rejected_before_body_iteration() -> None:
    class NeverReadCompressedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.iterated = False

        async def __aiter__(self):
            self.iterated = True
            yield b"compressed-body-must-not-be-read"

    stream = NeverReadCompressedStream()
    provider = OpenAICompatibleChatProvider(
        ProviderConfig.default(),
        InMemoryCredentialStore("invalid-fake-key"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={**SSE_HEADERS, "content-encoding": "gzip"},
                stream=stream,
                request=request,
            )
        ),
    )

    with pytest.raises(ChatProviderError) as caught:
        collect(provider)

    assert caught.value.code is ProviderErrorCode.PROTOCOL
    assert not stream.iterated


def test_cancel_tolerates_event_loop_closing_race() -> None:
    class ClosingLoop:
        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, _callback) -> None:
            raise RuntimeError("event loop closed during scheduling")

    class FakeTask:
        def cancel(self) -> None:
            pass

    token = CancellationToken()
    token._tasks[FakeTask()] = (ClosingLoop(), 1)  # type: ignore[assignment]

    token.cancel()

    assert token.is_cancelled
