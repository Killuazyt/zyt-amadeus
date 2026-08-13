"""Bundled P7G provider catalog and capability registry.

The catalog is static, ships with the application, never calls ``/models`` and
never refreshes over the network.  A corrupt packaged catalog fails closed to a
small built-in recovery catalog so the settings center remains usable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

CATALOG_SCHEMA_VERSION = 1
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ProviderCatalogError(ValueError):
    """Raised when bundled or injected catalog data is malformed."""


class ProviderProtocol(StrEnum):
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class ProviderRole(StrEnum):
    CONVERSATION = "conversation"
    SUMMARY = "summary"
    MEMORY = "memory"
    VISION = "vision"


class ProviderAuth(StrEnum):
    BEARER = "bearer"
    API_KEY = "api_key"
    ANTHROPIC_X_API_KEY = "anthropic_x_api_key"
    NONE = "none"


class ReasoningPolicy(StrEnum):
    NONE = "none"
    ENABLE_THINKING_FALSE = "enable_thinking_false"
    THINKING_TYPE_DISABLED = "thinking_type_disabled"
    GEMINI_DISABLED = "gemini_disabled"
    OPENROUTER_NONE = "openrouter_none"
    MINIMAX_SPLIT = "minimax_split"
    ANTHROPIC_DISABLED = "anthropic_disabled"


class CachePolicy(StrEnum):
    NONE = "none"
    DASHSCOPE_SESSION = "dashscope_session"
    ANTHROPIC_EPHEMERAL = "anthropic_ephemeral"
    UPSTREAM_AUTO = "upstream_auto"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    streaming: bool
    image_input: bool
    native_files: bool = False
    reasoning: bool = False
    reasoning_can_disable: bool = False
    suggested_context_tokens: int = 32_768
    suggested_output_tokens: int = 4_096
    first_chunk_timeout_seconds: int = 30
    idle_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    catalog_id: str
    display_name: str
    protocol: ProviderProtocol
    endpoints: tuple[str, ...]
    auth: ProviderAuth
    models: Mapping[ProviderRole, str]
    capabilities: ProviderCapabilities
    reasoning_policy: ReasoningPolicy
    cache_policy: CachePolicy
    custom_endpoint: bool = False
    local_template: bool = False
    custom_models: bool = True

    @property
    def default_endpoint(self) -> str:
        return self.endpoints[0] if self.endpoints else "https://api.example.invalid/v1"

    def model_for(self, role: ProviderRole) -> str:
        return self.models.get(role, "")


@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    entries: Mapping[str, ProviderCatalogEntry]
    degraded: bool = False
    error_category: str | None = None

    def entry(self, catalog_id: str) -> ProviderCatalogEntry:
        try:
            return self.entries[catalog_id]
        except KeyError:
            raise ProviderCatalogError("Unknown provider catalog entry.") from None

    def all(self) -> tuple[ProviderCatalogEntry, ...]:
        return tuple(self.entries.values())


_ENTRY_FIELDS = frozenset(
    {
        "id",
        "name",
        "protocol",
        "endpoints",
        "auth",
        "models",
        "stream",
        "vision",
        "native_files",
        "reasoning",
        "reasoning_can_disable",
        "context_tokens",
        "output_tokens",
        "first_chunk_timeout_seconds",
        "idle_timeout_seconds",
        "custom_models",
        "reasoning_policy",
        "cache_policy",
        "custom",
        "local",
    }
)
_MODEL_FIELDS = frozenset(role.value for role in ProviderRole)


def load_provider_catalog(path: Path | None = None) -> ProviderCatalog:
    """Load and strictly validate the bundled catalog with fail-closed recovery."""

    try:
        if path is None:
            resource = (
                Path(__file__).resolve().parent
                / "resources"
                / "provider_catalog"
                / "providers.json"
            )
            with resource.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        else:
            with Path(path).open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        return parse_provider_catalog(document)
    except (OSError, UnicodeError, json.JSONDecodeError, ProviderCatalogError, TypeError):
        return ProviderCatalog(
            MappingProxyType({entry.catalog_id: entry for entry in _recovery_entries()}),
            degraded=True,
            error_category="catalog_invalid",
        )


def parse_provider_catalog(document: object) -> ProviderCatalog:
    if not isinstance(document, dict) or set(document) != {"schema_version", "providers"}:
        raise ProviderCatalogError("Provider catalog root is invalid.")
    if document.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ProviderCatalogError("Provider catalog version is unsupported.")
    raw_entries = document.get("providers")
    if not isinstance(raw_entries, list) or not raw_entries or len(raw_entries) > 64:
        raise ProviderCatalogError("Provider catalog entries are invalid.")
    entries: dict[str, ProviderCatalogEntry] = {}
    for raw in raw_entries:
        entry = _parse_entry(raw)
        if entry.catalog_id in entries:
            raise ProviderCatalogError("Provider catalog contains duplicate ids.")
        entries[entry.catalog_id] = entry
    return ProviderCatalog(MappingProxyType(entries))


def _parse_entry(raw: object) -> ProviderCatalogEntry:
    if not isinstance(raw, dict) or not set(raw).issubset(_ENTRY_FIELDS):
        raise ProviderCatalogError("Provider catalog entry fields are invalid.")
    required = _ENTRY_FIELDS - {"custom", "local"}
    if not required.issubset(raw):
        raise ProviderCatalogError("Provider catalog entry fields are incomplete.")
    catalog_id = _safe_identifier(raw["id"])
    display_name = _safe_text(raw["name"], 80)
    try:
        protocol = ProviderProtocol(raw["protocol"])
        auth = ProviderAuth(raw["auth"])
        reasoning_policy = ReasoningPolicy(raw["reasoning_policy"])
        cache_policy = CachePolicy(raw["cache_policy"])
    except (TypeError, ValueError):
        raise ProviderCatalogError("Provider catalog enum is invalid.") from None
    endpoints_raw = raw["endpoints"]
    if not isinstance(endpoints_raw, list) or len(endpoints_raw) > 4:
        raise ProviderCatalogError("Provider catalog endpoints are invalid.")
    endpoints = tuple(_safe_endpoint(value) for value in endpoints_raw)
    if len(endpoints) != len(set(endpoints)):
        raise ProviderCatalogError("Provider catalog endpoints must be unique.")
    custom = raw.get("custom") is True
    local = raw.get("local") is True
    if custom and local:
        raise ProviderCatalogError("Provider catalog type flags are mutually exclusive.")
    if not custom and not endpoints:
        raise ProviderCatalogError("A built-in provider requires an endpoint.")
    if custom and endpoints:
        raise ProviderCatalogError("Custom provider templates must not lock an endpoint.")
    if local and auth is not ProviderAuth.NONE:
        raise ProviderCatalogError("Local templates must use loopback no-auth mode.")
    if auth is ProviderAuth.NONE and not local and not custom:
        raise ProviderCatalogError("Unauthenticated providers must be local templates.")
    if auth is ProviderAuth.NONE and any(
        (urlsplit(endpoint).hostname or "").rstrip(".").casefold() not in _LOOPBACK_HOSTS
        for endpoint in endpoints
    ):
        raise ProviderCatalogError("Unauthenticated catalog endpoints must be loopback-only.")
    if protocol is ProviderProtocol.ANTHROPIC_MESSAGES and auth not in {
        ProviderAuth.ANTHROPIC_X_API_KEY,
        ProviderAuth.BEARER,
    }:
        raise ProviderCatalogError("Anthropic protocol authentication is invalid.")
    models_raw = raw["models"]
    if not isinstance(models_raw, dict) or set(models_raw) != _MODEL_FIELDS:
        raise ProviderCatalogError("Provider catalog model slots are invalid.")
    models = MappingProxyType(
        {ProviderRole(role): _safe_model(models_raw[role]) for role in _MODEL_FIELDS}
    )
    boolean_capabilities = {
        field: raw[field]
        for field in (
            "stream",
            "vision",
            "native_files",
            "reasoning",
            "reasoning_can_disable",
            "custom_models",
        )
    }
    if any(not isinstance(value, bool) for value in boolean_capabilities.values()):
        raise ProviderCatalogError("Provider capabilities are invalid.")
    if boolean_capabilities["reasoning_can_disable"] and not boolean_capabilities["reasoning"]:
        raise ProviderCatalogError("Reasoning disable capability requires reasoning support.")
    if boolean_capabilities["native_files"]:
        raise ProviderCatalogError("P7G does not expose provider-native file uploads.")
    if not boolean_capabilities["custom_models"]:
        raise ProviderCatalogError("P7G catalog model suggestions must remain editable.")
    if (reasoning_policy is ReasoningPolicy.NONE) == boolean_capabilities["reasoning"]:
        raise ProviderCatalogError("Reasoning capability and request policy are inconsistent.")
    expected_reasoning_can_disable = reasoning_policy in {
        ReasoningPolicy.ENABLE_THINKING_FALSE,
        ReasoningPolicy.THINKING_TYPE_DISABLED,
        ReasoningPolicy.OPENROUTER_NONE,
        ReasoningPolicy.ANTHROPIC_DISABLED,
    }
    if boolean_capabilities["reasoning_can_disable"] != expected_reasoning_can_disable:
        raise ProviderCatalogError("Reasoning disable capability and policy are inconsistent.")
    context_tokens = _safe_integer(raw["context_tokens"], 1, 16_777_216)
    output_tokens = _safe_integer(raw["output_tokens"], 1, 1_048_576)
    first_chunk_timeout_seconds = _safe_integer(raw["first_chunk_timeout_seconds"], 1, 300)
    idle_timeout_seconds = _safe_integer(raw["idle_timeout_seconds"], 1, 300)
    if output_tokens > context_tokens:
        raise ProviderCatalogError("Suggested output tokens exceed the context window.")
    if models[ProviderRole.VISION] and not boolean_capabilities["vision"]:
        raise ProviderCatalogError("Vision model suggestion requires image capability.")
    capabilities = ProviderCapabilities(
        streaming=boolean_capabilities["stream"],
        image_input=boolean_capabilities["vision"],
        native_files=boolean_capabilities["native_files"],
        reasoning=boolean_capabilities["reasoning"],
        reasoning_can_disable=boolean_capabilities["reasoning_can_disable"],
        suggested_context_tokens=context_tokens,
        suggested_output_tokens=output_tokens,
        first_chunk_timeout_seconds=first_chunk_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    return ProviderCatalogEntry(
        catalog_id=catalog_id,
        display_name=display_name,
        protocol=protocol,
        endpoints=endpoints,
        auth=auth,
        models=models,
        capabilities=capabilities,
        reasoning_policy=reasoning_policy,
        cache_policy=cache_policy,
        custom_endpoint=custom,
        local_template=local,
        custom_models=boolean_capabilities["custom_models"],
    )


def _recovery_entries() -> tuple[ProviderCatalogEntry, ...]:
    return (
        ProviderCatalogEntry(
            "custom_openai",
            "自定义 OpenAI Chat Completions（目录恢复）",
            ProviderProtocol.OPENAI_CHAT_COMPLETIONS,
            (),
            ProviderAuth.BEARER,
            MappingProxyType(
                {
                    role: "" if role is ProviderRole.VISION else "custom-model"
                    for role in ProviderRole
                }
            ),
            ProviderCapabilities(True, True),
            ReasoningPolicy.NONE,
            CachePolicy.NONE,
            custom_endpoint=True,
        ),
        ProviderCatalogEntry(
            "custom_anthropic",
            "自定义 Anthropic Messages（目录恢复）",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            (),
            ProviderAuth.ANTHROPIC_X_API_KEY,
            MappingProxyType(
                {
                    role: "" if role is ProviderRole.VISION else "custom-model"
                    for role in ProviderRole
                }
            ),
            ProviderCapabilities(True, True),
            ReasoningPolicy.NONE,
            CachePolicy.NONE,
            custom_endpoint=True,
        ),
    )


def _safe_identifier(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 48
        or not value.replace("_", "").isalnum()
        or value != value.casefold()
    ):
        raise ProviderCatalogError("Provider catalog id is invalid.")
    return value


def _safe_text(value: Any, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProviderCatalogError("Provider catalog text is invalid.")
    return value


def _safe_endpoint(value: Any) -> str:
    """Validate a shipped endpoint before it can become a trusted catalog contract."""

    endpoint = _safe_text(value, 512)
    if "\\" in endpoint or any(character.isspace() for character in endpoint):
        raise ProviderCatalogError("Provider catalog endpoint is invalid.")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ProviderCatalogError("Provider catalog endpoint is invalid.") from None
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or not parsed.hostname:
        raise ProviderCatalogError("Provider catalog endpoint requires an HTTP(S) host.")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderCatalogError("Provider catalog endpoint has forbidden URL components.")
    if "%" in parsed.netloc:
        raise ProviderCatalogError("Provider catalog endpoint host must not be encoded.")
    hostname = parsed.hostname.rstrip(".").casefold()
    decoded_hostname = unquote(hostname).casefold()
    if parsed.scheme == "http" and hostname not in _LOOPBACK_HOSTS:
        raise ProviderCatalogError("Catalog HTTP endpoints must be exact loopback hosts.")
    if decoded_hostname.startswith("token-plan-") or ".token-plan-" in decoded_hostname:
        raise ProviderCatalogError("MiMo Token Plan endpoints are not supported.")
    path = parsed.path.rstrip("/")
    decoded_path = unquote(path).casefold()
    if decoded_path.endswith("/chat/completions") or decoded_path.endswith("/messages"):
        raise ProviderCatalogError("Catalog endpoints must be base URLs.")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        if not 1 <= port <= 65535:
            raise ProviderCatalogError("Provider catalog endpoint port is invalid.")
        normalized_host = f"{normalized_host}:{port}"
    normalized = urlunsplit(SplitResult(parsed.scheme, normalized_host, path, "", ""))
    if endpoint != normalized:
        raise ProviderCatalogError("Provider catalog endpoint is not normalized.")
    return normalized


def _safe_model(value: Any) -> str:
    if value == "":
        return ""
    return _safe_text(value, 192)


def _safe_integer(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProviderCatalogError("Provider catalog integer capability is invalid.")
    return value
