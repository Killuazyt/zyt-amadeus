"""Cancellable OpenAI-compatible providers and deterministic local simulation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import contextlib
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import httpx

from amadeus_desktop.chat_models import (
    ChatRequest,
    ImagePart,
    PromptContent,
    PromptMessage,
    PromptRole,
    TextPart,
)
from amadeus_desktop.credential_store import CredentialStore
from amadeus_desktop.provider_catalog import (
    CachePolicy,
    ProviderAuth,
    ProviderProtocol,
    ReasoningPolicy,
)
from amadeus_desktop.provider_config import (
    AuthMode,
    ProviderConfig,
    ProviderPreset,
    TokenLimitField,
)
from amadeus_desktop.provider_profiles import ProviderRequestSnapshot


class ProviderErrorCode(StrEnum):
    """Stable error categories safe for UI and diagnostics mapping."""

    NOT_CONFIGURED = "not_configured"
    CREDENTIAL = "credential"
    AUTHENTICATION = "authentication"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    MODEL_OR_PARAMETER = "model_or_parameter"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    CONTENT_FILTER = "content_filter"
    SERVER = "server"


_SAFE_ERROR_MESSAGES = {
    ProviderErrorCode.NOT_CONFIGURED: "尚未配置对话模型。",
    ProviderErrorCode.CREDENTIAL: "无法读取模型凭据，请重新配置。",
    ProviderErrorCode.AUTHENTICATION: "模型服务鉴权失败，请检查密钥。",
    ProviderErrorCode.INSUFFICIENT_BALANCE: "模型服务余额或额度不足。",
    ProviderErrorCode.MODEL_OR_PARAMETER: "模型名称或请求参数不受支持。",
    ProviderErrorCode.RATE_LIMIT: "模型服务请求过于频繁，请稍后重试。",
    ProviderErrorCode.NETWORK: "无法连接模型服务，请检查网络。",
    ProviderErrorCode.TIMEOUT: "模型服务响应超时，请重试。",
    ProviderErrorCode.PROTOCOL: "模型服务返回了无法识别的数据。",
    ProviderErrorCode.CONTENT_FILTER: "模型服务因内容安全限制未返回回答。",
    ProviderErrorCode.SERVER: "模型服务暂时不可用，请稍后重试。",
}

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_SSE_EVENT_CHARS = 256 * 1024
_MAX_VISIBLE_OUTPUT_CHARS = 1024 * 1024


class ChatProviderError(Exception):
    """Normalized provider failure containing only a stable, safe message."""

    def __init__(
        self,
        code: ProviderErrorCode | str,
    ) -> None:
        if isinstance(code, ProviderErrorCode):
            self.code = code
            message = _SAFE_ERROR_MESSAGES[code]
        else:
            # Never treat provider-controlled exception text as safe UI text.
            self.code = ProviderErrorCode.PROTOCOL
            message = _SAFE_ERROR_MESSAGES[self.code]
        self.safe_message = message
        super().__init__(message)


class CancellationRequested(ChatProviderError):
    """Raised when the caller cancels the current provider task."""

    def __init__(self) -> None:
        super().__init__(ProviderErrorCode.NETWORK)


class CancellationToken:
    """Thread-safe cancellation primitive that can cancel bound asyncio tasks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._tasks: dict[asyncio.Task[Any], tuple[asyncio.AbstractEventLoop, int]] = {}

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            tasks = tuple((task, binding[0]) for task, binding in self._tasks.items())
        for task, loop in tasks:
            # The worker loop can close between the check and the cross-thread
            # scheduling call. Cancellation remains best-effort once it is gone.
            with contextlib.suppress(RuntimeError):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)

    def wait(self, timeout_seconds: float | None = None) -> bool:
        """Wait for cancellation and return whether cancellation occurred."""

        return self._event.wait(timeout_seconds)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationRequested

    def bind_current_task(self) -> None:
        """Bind the current task so ``cancel`` can interrupt an active await."""

        loop = asyncio.get_running_loop()
        task = asyncio.current_task(loop)
        if task is None:
            raise RuntimeError("no current asyncio task")
        with self._lock:
            current = self._tasks.get(task)
            count = current[1] + 1 if current is not None else 1
            self._tasks[task] = (loop, count)
            cancelled = self._event.is_set()
        if cancelled:
            loop.call_soon(task.cancel)

    def unbind_current_task(self) -> None:
        """Remove one binding for the current task."""

        task = asyncio.current_task()
        if task is None:
            return
        with self._lock:
            current = self._tasks.get(task)
            if current is None:
                return
            if current[1] <= 1:
                self._tasks.pop(task, None)
            else:
                self._tasks[task] = (current[0], current[1] - 1)


@runtime_checkable
class ChatProvider(Protocol):
    """Asynchronous provider boundary executed exclusively off the UI thread."""

    def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        """Yield user-visible text chunks until completion or cancellation."""


class UnconfiguredChatProvider:
    """Fail-closed production placeholder used until a credential is configured."""

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        del request
        cancellation.raise_if_cancelled()
        raise ChatProviderError(ProviderErrorCode.NOT_CONFIGURED)
        yield ""  # pragma: no cover - makes this an async iterator without doing work


class ScriptedScenario(StrEnum):
    """Deterministic scenarios used by P3/P4 runtime simulation and tests."""

    NORMAL = "normal"
    SLOW_FIRST = "slow_first"
    NEVER = "never"
    PARTIAL_ERROR = "partial_error"
    STALL = "stall"


class ScriptedChatProvider:
    """Local provider that never performs network, database, or secret access."""

    DEFAULT_CHUNKS = ("这是一个", "本地模拟回复", "，用于验证流式对话。")

    def __init__(
        self,
        scenario: ScriptedScenario | str = ScriptedScenario.NORMAL,
        *,
        chunks: tuple[str, ...] = DEFAULT_CHUNKS,
        first_delay_ms: int = 80,
        chunk_delay_ms: int = 45,
        slow_first_delay_ms: int = 500,
    ) -> None:
        self.scenario = ScriptedScenario(scenario)
        if not chunks or any(not isinstance(chunk, str) or not chunk for chunk in chunks):
            raise ValueError("chunks must contain at least one non-empty string")
        for name, value in (
            ("first_delay_ms", first_delay_ms),
            ("chunk_delay_ms", chunk_delay_ms),
            ("slow_first_delay_ms", slow_first_delay_ms),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        self.chunks = chunks
        self.first_delay_ms = first_delay_ms
        self.chunk_delay_ms = chunk_delay_ms
        self.slow_first_delay_ms = slow_first_delay_ms
        self._requests: list[ChatRequest] = []
        self._lock = threading.Lock()

    @property
    def requests(self) -> tuple[ChatRequest, ...]:
        with self._lock:
            return tuple(self._requests)

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._requests)

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        cancellation.bind_current_task()
        try:
            with self._lock:
                self._requests.append(request)

            if self.scenario is ScriptedScenario.NEVER:
                await asyncio.Future()

            first_delay_ms = (
                self.slow_first_delay_ms
                if self.scenario is ScriptedScenario.SLOW_FIRST
                else self.first_delay_ms
            )
            await self._wait_or_cancel(cancellation, first_delay_ms)

            yield self.chunks[0]
            if self.scenario is ScriptedScenario.PARTIAL_ERROR:
                await self._wait_or_cancel(cancellation, self.chunk_delay_ms)
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if self.scenario is ScriptedScenario.STALL:
                await asyncio.Future()

            for chunk in self.chunks[1:]:
                await self._wait_or_cancel(cancellation, self.chunk_delay_ms)
                yield chunk
            cancellation.raise_if_cancelled()
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        finally:
            cancellation.unbind_current_task()

    @staticmethod
    async def _wait_or_cancel(cancellation: CancellationToken, delay_ms: int) -> None:
        cancellation.raise_if_cancelled()
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        cancellation.raise_if_cancelled()


ClientFactory = Callable[..., httpx.AsyncClient]


def _serialize_prompt_content(content: PromptContent) -> object:
    """Serialize structured content without changing legacy text payloads."""

    if isinstance(content, str):
        return content
    serialized: list[dict[str, object]] = []
    for part in content:
        if isinstance(part, TextPart):
            if not isinstance(part.text, str) or not part.text:
                raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
            serialized.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, ImagePart):
            if part.detail not in {"auto", "low", "high"}:
                raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
            _parse_image_data_url(part.data_url)
            serialized.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.data_url, "detail": part.detail},
                }
            )
            continue
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    if not serialized:
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    return serialized


class OpenAICompatibleChatProvider:
    """Strict OpenAI-compatible Chat Completions implementation."""

    def __init__(
        self,
        config: ProviderConfig | ProviderRequestSnapshot,
        credential_store: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        self.config = config.validated() if isinstance(config, ProviderConfig) else config
        if isinstance(self.config, ProviderRequestSnapshot) and (
            self.config.protocol is not ProviderProtocol.OPENAI_CHAT_COMPLETIONS
        ):
            raise ValueError("OpenAI-compatible provider received another protocol")
        self._credential_store = credential_store
        self._transport = transport
        self._client_factory = client_factory
        self._visible_output_limit = min(
            _MAX_VISIBLE_OUTPUT_CHARS,
            max(4_096, self.config.max_output_tokens * 16),
        )

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        cancellation.bind_current_task()
        response: httpx.Response | None = None
        try:
            cancellation.raise_if_cancelled()
            secret = self._read_secret()
            headers = self._auth_headers(secret)
            headers.update(self._cache_headers())
            payload = self._build_payload(request)
            timeout = httpx.Timeout(
                self.config.request_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
            )
            client_kwargs: dict[str, object] = {
                "timeout": timeout,
                "follow_redirects": False,
                "trust_env": False,
            }
            if self._transport is not None:
                client_kwargs["transport"] = self._transport

            async with asyncio.timeout(self.config.request_timeout_seconds):
                async with self._client_factory(**client_kwargs) as client:
                    network_request = client.build_request(
                        "POST",
                        f"{self.config.base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response = await client.send(
                        network_request,
                        # Always stream at the HTTP layer so even a non-streaming
                        # provider response is subject to the local byte cap.
                        stream=True,
                    )
                    await self._raise_for_status(response)
                    if self.config.stream_enabled:
                        async for chunk in _bounded_stream_chunks(
                            self._parse_stream(response),
                            first_timeout_seconds=float(
                                getattr(
                                    self.config,
                                    "first_chunk_timeout_seconds",
                                    self.config.request_timeout_seconds,
                                )
                            ),
                            idle_timeout_seconds=float(
                                getattr(
                                    self.config,
                                    "idle_timeout_seconds",
                                    self.config.request_timeout_seconds,
                                )
                            ),
                        ):
                            cancellation.raise_if_cancelled()
                            yield chunk
                    else:
                        for chunk in await self._parse_completion(response):
                            cancellation.raise_if_cancelled()
                            yield chunk
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        except TimeoutError as exc:
            raise ChatProviderError(ProviderErrorCode.TIMEOUT) from exc
        except httpx.TimeoutException as exc:
            raise ChatProviderError(ProviderErrorCode.TIMEOUT) from exc
        except httpx.RemoteProtocolError as exc:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
        except (httpx.NetworkError, httpx.ProxyError) as exc:
            raise ChatProviderError(ProviderErrorCode.NETWORK) from exc
        except ChatProviderError:
            raise
        except Exception as exc:
            # The raw exception may contain a URL, header, body, prompt, or secret.
            raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    await response.aclose()
            cancellation.unbind_current_task()

    def _read_secret(self) -> str:
        if getattr(self.config, "auth", None) is ProviderAuth.NONE:
            return ""
        try:
            secret = self._credential_store.read_secret()
        except Exception as exc:
            raise ChatProviderError(ProviderErrorCode.CREDENTIAL) from exc
        if not isinstance(secret, str) or not secret.strip():
            raise ChatProviderError(ProviderErrorCode.CREDENTIAL)
        secret = secret.strip()
        if secret.casefold().startswith("tp-"):
            raise ChatProviderError(ProviderErrorCode.CREDENTIAL)
        return secret

    def _auth_headers(self, secret: str) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream, application/json",
            # Enforce limits on the bytes actually received; never let an
            # implicit content decoder inflate an attacker-controlled body first.
            "Accept-Encoding": "identity",
        }
        auth_mode = getattr(self.config, "auth", None)
        if auth_mode is None:
            auth_mode = (
                ProviderAuth.BEARER
                if self.config.auth_mode is AuthMode.BEARER
                else ProviderAuth.API_KEY
            )
        if auth_mode is ProviderAuth.BEARER:
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_mode is ProviderAuth.API_KEY:
            headers["api-key"] = secret
        elif auth_mode is ProviderAuth.NONE:
            pass
        else:
            raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
        return headers

    def _cache_headers(self) -> dict[str, str]:
        if not bool(getattr(self.config, "cache_enabled", False)):
            return {}
        if getattr(self.config, "cache_policy", None) is CachePolicy.DASHSCOPE_SESSION:
            return {"x-dashscope-session-cache": "enable"}
        return {}

    def _build_payload(self, request: ChatRequest) -> dict[str, object]:
        temperature = (
            self.config.temperature
            if request.options.temperature is None
            else request.options.temperature
        )
        max_output_tokens = (
            self.config.max_output_tokens
            if request.options.max_output_tokens is None
            else min(request.options.max_output_tokens, self.config.max_output_tokens)
        )
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [
                {"role": message.role.value, "content": _serialize_prompt_content(message.content)}
                for message in request.messages
            ],
            "temperature": temperature,
            "top_p": self.config.top_p,
            "stream": self.config.stream_enabled,
        }
        token_field = getattr(self.config, "token_limit_field", None)
        if isinstance(token_field, TokenLimitField):
            token_field = token_field.value
        if token_field is None:
            token_field = "max_completion_tokens"
        if token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
        payload[token_field] = max_output_tokens
        policy = getattr(self.config, "reasoning_policy", None)
        if policy is None and self.config.preset in {
            ProviderPreset.DEEPSEEK_PAYG,
            ProviderPreset.MIMO_PAYG,
        }:
            policy = ReasoningPolicy.THINKING_TYPE_DISABLED
        payload.update(_reasoning_payload(policy))
        return payload

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        content_encoding = response.headers.get("content-encoding", "").strip().casefold()
        if content_encoding not in {"", "identity"}:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        if 200 <= response.status_code < 300:
            return
        body = await _read_response_limited(response)
        error_details = _safe_error_details(body)
        code = _http_error_code(response.status_code, error_details)
        raise ChatProviderError(code)

    async def _parse_stream(self, response: httpx.Response) -> AsyncIterator[str]:
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "text/event-stream":
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        saw_done = False
        visible_characters = 0
        async for data in _iter_sse_data(response.aiter_bytes()):
            if not data.strip():
                continue
            if data.strip() == "[DONE]":
                saw_done = True
                break
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
            for content in _content_from_payload(payload, streaming=True):
                visible_characters += len(content)
                if visible_characters > self._visible_output_limit:
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                yield content
        if not saw_done:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)

    async def _parse_completion(self, response: httpx.Response) -> tuple[str, ...]:
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        body = await _read_response_limited(response)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
        content = tuple(_content_from_payload(payload, streaming=False))
        if sum(len(chunk) for chunk in content) > self._visible_output_limit:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        return content


class AnthropicMessagesChatProvider(OpenAICompatibleChatProvider):
    """Strict Anthropic Messages adapter implemented directly on ``httpx``."""

    def __init__(
        self,
        config: ProviderRequestSnapshot,
        credential_store: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        if config.protocol is not ProviderProtocol.ANTHROPIC_MESSAGES:
            raise ValueError("Anthropic provider requires anthropic_messages protocol")
        self.config = config
        self._credential_store = credential_store
        self._transport = transport
        self._client_factory = client_factory
        self._visible_output_limit = min(
            _MAX_VISIBLE_OUTPUT_CHARS,
            max(4_096, config.max_output_tokens * 16),
        )

    def _auth_headers(self, secret: str) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream, application/json",
            "Accept-Encoding": "identity",
            "anthropic-version": "2023-06-01",
        }
        if self.config.auth is ProviderAuth.ANTHROPIC_X_API_KEY:
            headers["x-api-key"] = secret
        elif self.config.auth is ProviderAuth.BEARER:
            headers["Authorization"] = f"Bearer {secret}"
        elif self.config.auth is ProviderAuth.NONE:
            pass
        else:
            raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
        return headers

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        cancellation.bind_current_task()
        response: httpx.Response | None = None
        try:
            cancellation.raise_if_cancelled()
            payload = self._build_anthropic_payload(request)
            timeout = httpx.Timeout(
                self.config.request_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
            )
            kwargs: dict[str, object] = {
                "timeout": timeout,
                "follow_redirects": False,
                "trust_env": False,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            async with asyncio.timeout(self.config.request_timeout_seconds):
                async with self._client_factory(**kwargs) as client:
                    network_request = client.build_request(
                        "POST",
                        f"{self.config.base_url.rstrip('/')}/messages",
                        headers=self._auth_headers(self._read_secret()),
                        json=payload,
                    )
                    response = await client.send(network_request, stream=True)
                    await self._raise_for_status(response)
                    if self.config.stream_enabled:
                        async for chunk in _bounded_stream_chunks(
                            self._parse_anthropic_stream(response),
                            first_timeout_seconds=self.config.first_chunk_timeout_seconds,
                            idle_timeout_seconds=self.config.idle_timeout_seconds,
                        ):
                            cancellation.raise_if_cancelled()
                            yield chunk
                    else:
                        for chunk in await self._parse_anthropic_completion(response):
                            cancellation.raise_if_cancelled()
                            yield chunk
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        except TimeoutError as exc:
            raise ChatProviderError(ProviderErrorCode.TIMEOUT) from exc
        except httpx.TimeoutException as exc:
            raise ChatProviderError(ProviderErrorCode.TIMEOUT) from exc
        except httpx.RemoteProtocolError as exc:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
        except (httpx.NetworkError, httpx.ProxyError) as exc:
            raise ChatProviderError(ProviderErrorCode.NETWORK) from exc
        except ChatProviderError:
            raise
        except Exception as exc:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    await response.aclose()
            cancellation.unbind_current_task()

    def _build_anthropic_payload(self, request: ChatRequest) -> dict[str, object]:
        systems: list[str] = []
        messages: list[dict[str, object]] = []
        for message in request.messages:
            if message.role is PromptRole.SYSTEM:
                systems.extend(_text_only_parts(message.content))
                continue
            messages.append(
                {
                    "role": message.role.value,
                    "content": _serialize_anthropic_content(message.content),
                }
            )
        if not messages:
            raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
        max_output = (
            self.config.max_output_tokens
            if request.options.max_output_tokens is None
            else min(request.options.max_output_tokens, self.config.max_output_tokens)
        )
        system_text = "\n\n".join(systems)
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_output,
            "temperature": (
                self.config.temperature
                if request.options.temperature is None
                else request.options.temperature
            ),
            "top_p": self.config.top_p,
            "stream": self.config.stream_enabled,
        }
        if system_text:
            payload["system"] = (
                [
                    {
                        "type": "text",
                        "text": system_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if self.config.cache_enabled
                and self.config.cache_policy is CachePolicy.ANTHROPIC_EPHEMERAL
                else system_text
            )
        return payload

    async def _parse_anthropic_stream(self, response: httpx.Response) -> AsyncIterator[str]:
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "text/event-stream":
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        started = False
        stopped = False
        visible = 0
        text_blocks: set[int] = set()
        ignored_thinking_blocks: set[int] = set()
        closed_blocks: set[int] = set()
        async for data in _iter_sse_data(response.aiter_bytes()):
            if not data.strip():
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
            if not isinstance(payload, dict):
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            event_type = payload.get("type")
            if event_type == "message_stop":
                if not started or text_blocks or ignored_thinking_blocks:
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                stopped = True
                break
            if event_type == "error":
                raise ChatProviderError(_payload_error_code(payload.get("error")))
            if event_type == "message_start":
                if started or not isinstance(payload.get("message"), dict):
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                started = True
                continue
            if not started and event_type != "ping":
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if event_type == "message_delta":
                delta = payload.get("delta")
                if not isinstance(delta, dict):
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                _raise_for_anthropic_stop_reason(delta.get("stop_reason"))
                continue
            if event_type == "ping":
                continue
            if event_type == "content_block_start":
                block = payload.get("content_block")
                index = payload.get("index")
                if not isinstance(block, dict) or not _is_event_index(index):
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                if (
                    index in text_blocks
                    or index in ignored_thinking_blocks
                    or index in closed_blocks
                ):
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                block_type = block.get("type")
                if block_type in {"thinking", "redacted_thinking"}:
                    ignored_thinking_blocks.add(index)
                    continue
                if block_type != "text":
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                text_blocks.add(index)
                continue
            if event_type == "content_block_stop":
                index = payload.get("index")
                if not _is_event_index(index):
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                if index in text_blocks:
                    text_blocks.remove(index)
                elif index in ignored_thinking_blocks:
                    ignored_thinking_blocks.remove(index)
                else:
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                closed_blocks.add(index)
                continue
            if event_type != "content_block_delta":
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            index = payload.get("index")
            delta = payload.get("delta")
            if not _is_event_index(index) or not isinstance(delta, dict):
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if index in ignored_thinking_blocks:
                if delta.get("type") not in {"thinking_delta", "signature_delta"}:
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                continue
            if index not in text_blocks:
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if delta.get("type") != "text_delta":
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            text = delta.get("text")
            if not isinstance(text, str):
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            visible += len(text)
            if visible > self._visible_output_limit:
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if text:
                yield text
        if not stopped:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)

    async def _parse_anthropic_completion(self, response: httpx.Response) -> tuple[str, ...]:
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        body = await _read_response_limited(response)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        _raise_for_anthropic_stop_reason(payload.get("stop_reason"))
        chunks: list[str] = []
        for block in payload["content"]:
            if not isinstance(block, dict):
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if block.get("type") in {"thinking", "redacted_thinking"}:
                continue
            if block.get("type") != "text":
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            text = block.get("text")
            if not isinstance(text, str):
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            if text:
                chunks.append(text)
        if sum(len(chunk) for chunk in chunks) > self._visible_output_limit:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        return tuple(chunks)


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """Privacy-safe evidence that a candidate provider completed a small request."""

    preset: ProviderPreset | str
    model: str
    elapsed_ms: int


class _CandidateCredentialStore:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def read_secret(self) -> str | None:
        return self._secret

    def has_secret(self) -> bool:
        return bool(self._secret)

    def write_secret(self, secret: str) -> None:
        self._secret = secret

    def delete_secret(self) -> None:
        self._secret = ""


class ProviderConnectionTester:
    """Run a minimal, cancellable request without persisting candidate secrets."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        self._transport = transport
        self._client_factory = client_factory

    async def test(
        self,
        config: ProviderConfig,
        secret: str,
        cancellation: CancellationToken,
    ) -> ConnectionTestResult:
        test_config = replace(config, max_output_tokens=min(config.max_output_tokens, 32))
        provider = OpenAICompatibleChatProvider(
            test_config,
            _CandidateCredentialStore(secret),
            transport=self._transport,
            client_factory=self._client_factory,
        )
        request = ChatRequest(
            request_id="connection-test",
            turn_id="connection-test",
            attempt=1,
            messages=(
                PromptMessage(
                    PromptRole.SYSTEM,
                    "这是不含用户资料的连接测试。请只给出简短文字回答。",
                ),
                PromptMessage(PromptRole.USER, "请回复：连接正常。"),
            ),
        )
        started = time.perf_counter()
        received_text = False
        async for chunk in provider.stream(request, cancellation):
            received_text = received_text or bool(chunk)
        if not received_text:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        return ConnectionTestResult(
            preset=config.preset,
            model=config.model,
            elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )

    async def test_profile(
        self,
        snapshot: ProviderRequestSnapshot,
        secret: str,
        cancellation: CancellationToken,
        *,
        visual_data_url: str | None = None,
    ) -> ConnectionTestResult:
        """Test one exact role/model snapshot with only synthetic app data."""

        provider = build_profile_provider(
            replace(snapshot, max_output_tokens=min(snapshot.max_output_tokens, 32)),
            _CandidateCredentialStore(secret),
            transport=self._transport,
            client_factory=self._client_factory,
        )
        user_content: PromptContent = "请回复：连接正常。"
        if snapshot.role.value == "vision":
            if not visual_data_url:
                raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
            user_content = (
                TextPart("这是应用生成的测试图。请只回复：视觉连接正常。"),
                ImagePart("connection-test-image", visual_data_url, "low"),
            )
        request = ChatRequest(
            request_id="profile-connection-test",
            turn_id="profile-connection-test",
            attempt=1,
            messages=(
                PromptMessage(
                    PromptRole.SYSTEM,
                    "这是不含用户聊天、附件或记忆的 Amadeus 最小连接测试。",
                ),
                PromptMessage(PromptRole.USER, user_content),
            ),
            provider_role=snapshot.role.value,
        )
        started = time.perf_counter()
        received = False
        async for chunk in provider.stream(request, cancellation):
            received = received or bool(chunk)
        if not received:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        return ConnectionTestResult(
            preset=snapshot.catalog_id,
            model=snapshot.model,
            elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )


def build_profile_provider(
    snapshot: ProviderRequestSnapshot,
    credential_store: CredentialStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client_factory: ClientFactory = httpx.AsyncClient,
) -> ChatProvider:
    """Construct exactly the protocol captured in an immutable request snapshot."""

    if snapshot.protocol is ProviderProtocol.OPENAI_CHAT_COMPLETIONS:
        provider: ChatProvider = OpenAICompatibleChatProvider(
            snapshot,
            credential_store,
            transport=transport,
            client_factory=client_factory,
        )
    elif snapshot.protocol is ProviderProtocol.ANTHROPIC_MESSAGES:
        provider = AnthropicMessagesChatProvider(
            snapshot,
            credential_store,
            transport=transport,
            client_factory=client_factory,
        )
    else:
        raise ValueError("Unsupported provider protocol")
    if snapshot.filter_think_tags:
        provider = ThinkingSafeProvider(provider)
    return provider


class ThinkingSafeProvider:
    """Remove a leading provider-leaked ``<think>`` block across stream chunks."""

    def __init__(self, provider: ChatProvider) -> None:
        self._provider = provider

    async def stream(
        self,
        request: ChatRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[str]:
        filtering: bool | None = None
        buffer = ""
        async for chunk in self._provider.stream(request, cancellation):
            if filtering is False:
                yield chunk
                continue
            buffer += chunk
            stripped = buffer.lstrip("\ufeff\r\n \t")
            if filtering is None:
                if len(stripped) < len("<think>") and "<think>".startswith(stripped.casefold()):
                    continue
                if stripped.casefold().startswith("<think>"):
                    filtering = True
                else:
                    filtering = False
                    if buffer:
                        yield buffer
                    buffer = ""
                    continue
            closing = buffer.casefold().find("</think>")
            if closing < 0:
                if len(buffer) > _MAX_SSE_EVENT_CHARS:
                    raise ChatProviderError(ProviderErrorCode.PROTOCOL)
                continue
            buffer = buffer[closing + len("</think>") :].lstrip("\r\n")
            filtering = False
            if buffer:
                yield buffer
            buffer = ""
        if filtering is True or (filtering is None and buffer):
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        if buffer:
            yield buffer


def _reasoning_payload(policy: ReasoningPolicy | None) -> dict[str, object]:
    """Return only reviewed provider-specific non-reasoning modifiers.

    This is a closed allowlist. No Profile field can inject an arbitrary
    request header, ``extra_body`` value, tool or search parameter.
    """

    if policy is ReasoningPolicy.ENABLE_THINKING_FALSE:
        return {"enable_thinking": False}
    if policy is ReasoningPolicy.THINKING_TYPE_DISABLED:
        return {"thinking": {"type": "disabled"}}
    if policy is ReasoningPolicy.GEMINI_DISABLED:
        # Gemini's OpenAI-compatibility wrappers have used SDK-only
        # ``extra_body`` shims across versions. Raw HTTP requests never emit
        # that field; absence is the only stable, reviewed no-opt-in contract.
        return {}
    if policy is ReasoningPolicy.OPENROUTER_NONE:
        return {"reasoning": {"effort": "none"}}
    if policy is ReasoningPolicy.MINIMAX_SPLIT:
        return {"reasoning_split": True}
    return {}


def _text_only_parts(content: PromptContent) -> tuple[str, ...]:
    if isinstance(content, str):
        return (content,) if content else ()
    parts: list[str] = []
    for part in content:
        if isinstance(part, TextPart) and part.text:
            parts.append(part.text)
        elif isinstance(part, ImagePart):
            raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    return tuple(parts)


def _serialize_anthropic_content(content: PromptContent) -> object:
    if isinstance(content, str):
        return content
    blocks: list[dict[str, object]] = []
    for part in content:
        if isinstance(part, TextPart):
            if not part.text:
                raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
            blocks.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, ImagePart):
            media_type, data = _parse_image_data_url(part.data_url)
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
            continue
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    if not blocks:
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    return blocks


def _parse_image_data_url(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 36 * 1024 * 1024:
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    prefix, separator, data = value.partition(",")
    if (
        not separator
        or not data
        or not prefix.startswith("data:")
        or not prefix.endswith(";base64")
    ):
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    media_type = prefix[5:-7]
    if media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER) from None
    if not decoded or len(decoded) > 25 * 1024 * 1024:
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    return media_type, data


async def _iter_sse_data(byte_chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Decode SSE data events across arbitrary UTF-8 and line chunk boundaries."""

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    buffer = ""
    data_lines: list[str] = []
    total_bytes = 0
    event_characters = 0

    def consume_line(line: str) -> str | None:
        nonlocal event_characters
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            if not data_lines:
                return None
            event = "\n".join(data_lines)
            data_lines.clear()
            event_characters = 0
            return event
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if field != "data":
            return None
        if separator and value.startswith(" "):
            value = value[1:]
        event_characters += len(value)
        if event_characters > _MAX_SSE_EVENT_CHARS:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        data_lines.append(value)
        return None

    try:
        async for chunk in byte_chunks:
            if not isinstance(chunk, bytes):
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESPONSE_BYTES:
                raise ChatProviderError(ProviderErrorCode.PROTOCOL)
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                event = consume_line(line)
                if event is not None:
                    yield event
        buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ChatProviderError(ProviderErrorCode.PROTOCOL) from exc

    if buffer:
        event = consume_line(buffer)
        if event is not None:
            yield event
    if data_lines:
        yield "\n".join(data_lines)


async def _bounded_stream_chunks(
    chunks: AsyncIterator[str],
    *,
    first_timeout_seconds: float,
    idle_timeout_seconds: float,
) -> AsyncIterator[str]:
    """Apply profile-captured first-chunk and stream-idle deadlines."""

    iterator = chunks.__aiter__()
    first = True
    while True:
        timeout_seconds = first_timeout_seconds if first else idle_timeout_seconds
        try:
            value = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_seconds)
        except StopAsyncIteration:
            return
        first = False
        yield value


async def _read_response_limited(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in response.aiter_bytes():
        if not isinstance(chunk, bytes):
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        total_bytes += len(chunk)
        if total_bytes > _MAX_RESPONSE_BYTES:
            raise ChatProviderError(ProviderErrorCode.PROTOCOL)
        chunks.append(chunk)
    return b"".join(chunks)


def _content_from_payload(payload: object, *, streaming: bool) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    if payload.get("error") is not None:
        raise ChatProviderError(_payload_error_code(payload.get("error")))
    choices = payload.get("choices")
    if choices == [] and streaming and payload.get("usage") is not None:
        return ()
    if not isinstance(choices, list) or len(choices) != 1:
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)

    finish_reason = choice.get("finish_reason")
    container_name = "delta" if streaming else "message"
    container = choice.get(container_name)
    if not isinstance(container, dict):
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    if container.get("tool_calls") or container.get("function_call"):
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    if any(
        key in container
        for key in (
            "reasoning",
            "reasoning_content",
            "thinking",
            "thought",
            "analysis",
        )
    ):
        # Amadeus never surfaces independent provider thinking fields. A
        # registered leading <think> leak in visible content is handled by the
        # separate bounded stream filter.
        container = {
            key: value
            for key, value in container.items()
            if key not in {"reasoning", "reasoning_content", "thinking", "thought", "analysis"}
        }
    if container.get("refusal"):
        raise ChatProviderError(ProviderErrorCode.CONTENT_FILTER)
    _raise_for_finish_reason(finish_reason)

    content = container.get("content")
    if content is None or content == "":
        return ()
    if not isinstance(content, str):
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    return (content,)


def _raise_for_finish_reason(finish_reason: object) -> None:
    if finish_reason is None or finish_reason == "stop":
        return
    if finish_reason == "length":
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    if finish_reason == "content_filter":
        raise ChatProviderError(ProviderErrorCode.CONTENT_FILTER)
    if finish_reason in {"tool_calls", "function_call"}:
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    raise ChatProviderError(ProviderErrorCode.PROTOCOL)


def _raise_for_anthropic_stop_reason(stop_reason: object) -> None:
    if stop_reason in {None, "end_turn", "stop_sequence"}:
        return
    if stop_reason == "max_tokens":
        raise ChatProviderError(ProviderErrorCode.MODEL_OR_PARAMETER)
    if stop_reason in {"tool_use", "pause_turn"}:
        raise ChatProviderError(ProviderErrorCode.PROTOCOL)
    if stop_reason == "refusal":
        raise ChatProviderError(ProviderErrorCode.CONTENT_FILTER)
    raise ChatProviderError(ProviderErrorCode.PROTOCOL)


def _is_event_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _safe_error_details(body: bytes) -> object:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return None
    return payload.get("error") if isinstance(payload, dict) else None


def _payload_error_code(error: object) -> ProviderErrorCode:
    text = ""
    if isinstance(error, dict):
        text = " ".join(
            str(error.get(field, "")) for field in ("code", "type", "message")
        ).casefold()
    elif isinstance(error, str):
        text = error.casefold()
    if any(word in text for word in ("balance", "quota", "insufficient", "余额", "额度")):
        return ProviderErrorCode.INSUFFICIENT_BALANCE
    if any(word in text for word in ("auth", "api key", "api_key", "unauthorized", "forbidden")):
        return ProviderErrorCode.AUTHENTICATION
    if "rate" in text and "limit" in text:
        return ProviderErrorCode.RATE_LIMIT
    if any(word in text for word in ("content_filter", "content filter", "safety")):
        return ProviderErrorCode.CONTENT_FILTER
    return ProviderErrorCode.PROTOCOL


def _http_error_code(status_code: int, error: object) -> ProviderErrorCode:
    payload_code = _payload_error_code(error)
    if payload_code is not ProviderErrorCode.PROTOCOL:
        return payload_code
    if status_code in {401, 403}:
        return ProviderErrorCode.AUTHENTICATION
    if status_code == 402:
        return ProviderErrorCode.INSUFFICIENT_BALANCE
    if status_code == 429:
        return ProviderErrorCode.RATE_LIMIT
    if status_code == 408:
        return ProviderErrorCode.TIMEOUT
    if status_code in {400, 404, 405, 409, 422}:
        return ProviderErrorCode.MODEL_OR_PARAMETER
    if 500 <= status_code < 600:
        return ProviderErrorCode.SERVER
    return ProviderErrorCode.PROTOCOL
