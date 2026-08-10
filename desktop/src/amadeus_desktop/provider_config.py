"""Validated, non-sensitive configuration for OpenAI-compatible providers."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Self
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

PROVIDER_CREDENTIAL_REF = "windows-credential-manager:amadeus-chat-provider"
MULTIMODAL_CREDENTIAL_REF = "windows-credential-manager:amadeus-multimodal-provider"
MIMO_SPEECH_CREDENTIAL_REF = "windows-credential-manager:amadeus-mimo-speech"
_ALLOWED_CREDENTIAL_REFS = frozenset(
    {PROVIDER_CREDENTIAL_REF, MULTIMODAL_CREDENTIAL_REF, MIMO_SPEECH_CREDENTIAL_REF}
)

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_EXPECTED_FIELDS = {
    "preset",
    "display_name",
    "base_url",
    "model",
    "auth_mode",
    "credential_ref",
    "connect_timeout_seconds",
    "request_timeout_seconds",
    "max_output_tokens",
    "temperature",
    "top_p",
    "stream_enabled",
    "token_limit_field",
}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ProviderConfigError(ValueError):
    """Raised when a provider configuration is unsafe or malformed."""


class ProviderPreset(StrEnum):
    """Supported provider contracts."""

    DEEPSEEK_PAYG = "deepseek_payg"
    MIMO_PAYG = "mimo_payg"
    CUSTOM_OPENAI = "custom_openai"


class AuthMode(StrEnum):
    """Supported API authentication schemes."""

    BEARER = "bearer"
    API_KEY = "api_key"


class TokenLimitField(StrEnum):
    """OpenAI-compatible request field used for the output limit."""

    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """A single active provider configuration without its secret."""

    preset: ProviderPreset
    display_name: str
    base_url: str
    model: str
    auth_mode: AuthMode
    credential_ref: str
    connect_timeout_seconds: float
    request_timeout_seconds: float
    max_output_tokens: int
    temperature: float
    top_p: float
    stream_enabled: bool
    token_limit_field: TokenLimitField

    def __post_init__(self) -> None:
        self.validated()

    @classmethod
    def default(cls) -> Self:
        """Return the locked P4 default without configuring a credential."""

        return cls.for_preset(ProviderPreset.DEEPSEEK_PAYG)

    @classmethod
    def for_preset(cls, preset: ProviderPreset | str, **overrides: Any) -> Self:
        """Build a validated provider preset with optional editable fields."""

        try:
            selected = ProviderPreset(preset)
        except (TypeError, ValueError):
            raise ProviderConfigError("Unsupported provider preset.") from None

        common: dict[str, Any] = {
            "preset": selected,
            "credential_ref": PROVIDER_CREDENTIAL_REF,
            "connect_timeout_seconds": 15.0,
            "request_timeout_seconds": 90.0,
            "max_output_tokens": 1024,
            "temperature": 0.7,
            "top_p": 0.9,
            "stream_enabled": True,
        }
        if selected is ProviderPreset.DEEPSEEK_PAYG:
            common.update(
                display_name="DeepSeek",
                base_url=_DEEPSEEK_BASE_URL,
                model="deepseek-v4-flash",
                auth_mode=AuthMode.BEARER,
                token_limit_field=TokenLimitField.MAX_TOKENS,
            )
        elif selected is ProviderPreset.MIMO_PAYG:
            common.update(
                display_name="MiMo",
                base_url=_MIMO_BASE_URL,
                model="mimo-v2.5-pro",
                auth_mode=AuthMode.API_KEY,
                token_limit_field=TokenLimitField.MAX_COMPLETION_TOKENS,
            )
        else:
            common.update(
                display_name="OpenAI 兼容服务",
                base_url="https://api.example.invalid/v1",
                model="custom-model",
                auth_mode=AuthMode.BEARER,
                token_limit_field=TokenLimitField.MAX_TOKENS,
            )
        unknown = set(overrides) - _EXPECTED_FIELDS
        if unknown:
            raise ProviderConfigError("Provider configuration contains unsupported fields.")
        common.update(overrides)
        return cls(**common).validated()

    @classmethod
    def from_mapping(cls, source: Any) -> Self:
        """Parse the strict JSON representation used by settings schema v3."""

        if not isinstance(source, dict):
            from collections.abc import Mapping

            if not isinstance(source, Mapping):
                raise ProviderConfigError("Provider configuration must be an object.")
        if set(source) != _EXPECTED_FIELDS:
            raise ProviderConfigError(
                "Provider configuration fields are incomplete or unsupported."
            )
        try:
            preset = ProviderPreset(source["preset"])
            auth_mode = AuthMode(source["auth_mode"])
            token_limit_field = TokenLimitField(source["token_limit_field"])
        except (TypeError, ValueError):
            raise ProviderConfigError(
                "Provider configuration contains invalid enum values."
            ) from None
        return cls(
            preset=preset,
            display_name=source["display_name"],
            base_url=source["base_url"],
            model=source["model"],
            auth_mode=auth_mode,
            credential_ref=source["credential_ref"],
            connect_timeout_seconds=source["connect_timeout_seconds"],
            request_timeout_seconds=source["request_timeout_seconds"],
            max_output_tokens=source["max_output_tokens"],
            temperature=source["temperature"],
            top_p=source["top_p"],
            stream_enabled=source["stream_enabled"],
            token_limit_field=token_limit_field,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-safe representation which never contains the secret."""

        result = asdict(self)
        result["preset"] = self.preset.value
        result["auth_mode"] = self.auth_mode.value
        result["token_limit_field"] = self.token_limit_field.value
        return result

    @property
    def chat_completions_url(self) -> str:
        """Return the request endpoint derived from the validated base URL."""

        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def credential_scope(self) -> tuple[str, str, str]:
        """Return the security boundary within which a saved secret may be reused."""

        return (self.preset.value, self.base_url, self.auth_mode.value)

    def validated(self) -> Self:
        """Validate all fields and preset-specific security invariants."""

        normalized_url = _validate_base_url(self.base_url)
        if normalized_url != self.base_url:
            raise ProviderConfigError("Base URL must already be normalized.")
        _validate_text(self.display_name, "Display name", maximum=64)
        _validate_text(self.model, "Model name", maximum=128)
        if self.credential_ref not in _ALLOWED_CREDENTIAL_REFS:
            raise ProviderConfigError("Credential reference is not owned by the application.")
        _validate_number(
            self.connect_timeout_seconds,
            "Connect timeout",
            minimum=1.0,
            maximum=60.0,
        )
        _validate_number(
            self.request_timeout_seconds,
            "Request timeout",
            minimum=5.0,
            maximum=300.0,
        )
        if self.request_timeout_seconds < self.connect_timeout_seconds:
            raise ProviderConfigError("Request timeout cannot be shorter than connect timeout.")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 1 <= self.max_output_tokens <= 32768
        ):
            raise ProviderConfigError("Maximum output tokens must be between 1 and 32768.")
        _validate_number(self.temperature, "Temperature", minimum=0.0, maximum=2.0)
        _validate_number(self.top_p, "Top P", minimum=0.0, maximum=1.0, minimum_open=True)
        if not isinstance(self.stream_enabled, bool):
            raise ProviderConfigError("Stream enabled must be a boolean.")

        if self.preset is ProviderPreset.DEEPSEEK_PAYG:
            _require_contract(
                self,
                display_name="DeepSeek",
                base_url=_DEEPSEEK_BASE_URL,
                auth_mode=AuthMode.BEARER,
                token_limit_field=TokenLimitField.MAX_TOKENS,
            )
        elif self.preset is ProviderPreset.MIMO_PAYG:
            _require_contract(
                self,
                display_name="MiMo",
                base_url=_MIMO_BASE_URL,
                auth_mode=AuthMode.API_KEY,
                token_limit_field=TokenLimitField.MAX_COMPLETION_TOKENS,
            )
        return self


def _validate_base_url(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderConfigError("Base URL must be a non-empty normalized string.")
    if "\\" in value or any(character.isspace() for character in value):
        raise ProviderConfigError("Base URL contains forbidden characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ProviderConfigError("Base URL is invalid.") from None
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise ProviderConfigError("Base URL must use HTTPS and include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigError("Base URL must not contain user information.")
    if parsed.query or parsed.fragment:
        raise ProviderConfigError("Base URL must not contain a query or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderConfigError("Base URL contains an invalid port.")

    hostname = parsed.hostname.rstrip(".").lower()
    if ":" not in hostname:
        labels = hostname.split(".")
        if any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise ProviderConfigError("Base URL host is invalid.")
    if hostname.startswith("token-plan-") and hostname.endswith(".xiaomimimo.com"):
        raise ProviderConfigError("MiMo Token Plan endpoints are not supported.")
    path = parsed.path.rstrip("/")
    if unquote(path).lower().endswith("/chat/completions"):
        raise ProviderConfigError("Enter a base URL, not the complete chat endpoint.")
    normalized_netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        normalized_netloc = f"[{hostname}]"
    if port is not None:
        normalized_netloc = f"{normalized_netloc}:{port}"
    normalized = urlunsplit(SplitResult("https", normalized_netloc, path, "", ""))
    if normalized != value:
        raise ProviderConfigError("Base URL must already be normalized.")
    return normalized


def _validate_text(value: Any, label: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProviderConfigError(f"{label} is invalid.")


def _validate_number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
    minimum_open: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderConfigError(f"{label} must be numeric.")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ProviderConfigError(f"{label} must be finite.")
    if numeric > maximum or (numeric <= minimum if minimum_open else numeric < minimum):
        raise ProviderConfigError(f"{label} is outside the supported range.")


def _require_contract(
    config: ProviderConfig,
    *,
    display_name: str,
    base_url: str,
    auth_mode: AuthMode,
    token_limit_field: TokenLimitField,
) -> None:
    if (
        config.display_name != display_name
        or config.base_url != base_url
        or config.auth_mode is not auth_mode
        or config.token_limit_field is not token_limit_field
    ):
        raise ProviderConfigError("Built-in provider contract fields cannot be changed.")
