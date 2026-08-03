"""Pure policy and provider request boundaries for restrained proactive greetings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from amadeus_desktop.chat_models import (
    ChatRequest,
    GenerationOptions,
    GenerationPurpose,
    PromptMessage,
    PromptRole,
)
from amadeus_desktop.persona import (
    build_capability_safety_boundary,
    build_persona_core_prompt,
)

STARTUP_DELAY_MS = 5_000
IDLE_THRESHOLD_SECONDS = 120 * 60
POLL_INTERVAL_MS = 60_000
GREETING_BUBBLE_TIMEOUT_MS = 12_000
MAX_GENERATED_GREETING_CHARS = 120


class ProactiveMode(StrEnum):
    OFF = "off"
    STARTUP_ONLY = "startup_only"
    RESTRAINED = "restrained"


class ProactiveTrigger(StrEnum):
    STARTUP = "startup"
    IDLE = "idle"


class ProactiveBlockReason(StrEnum):
    ALLOWED = "allowed"
    MODE_OFF = "mode_off"
    TRIGGER_DISABLED = "trigger_disabled"
    QUIET_HOURS = "quiet_hours"
    DAILY_LIMIT = "daily_limit"
    PAUSED_TODAY = "paused_today"
    PET_HIDDEN = "pet_hidden"
    CONVERSATION_ACTIVE = "conversation_active"
    SETTINGS_OPEN = "settings_open"
    SESSION_LOCKED = "session_locked"
    FULLSCREEN = "fullscreen"
    DATA_UNAVAILABLE = "data_unavailable"


@dataclass(frozen=True, slots=True)
class ProactivePolicyInput:
    now: datetime
    trigger: ProactiveTrigger
    mode: ProactiveMode
    quiet_start_minute: int
    quiet_end_minute: int
    daily_limit: int
    displayed_today: int
    paused_local_date: str | None
    pet_visible: bool
    conversation_active: bool
    settings_open: bool
    session_locked: bool
    fullscreen: bool
    data_writable: bool


@dataclass(slots=True)
class ProactiveOpportunityTracker:
    """Consume startup and idle opportunities exactly once per real cycle."""

    startup_consumed: bool = False
    idle_cycle_consumed: bool = False

    def consume_startup(self) -> bool:
        if self.startup_consumed:
            return False
        self.startup_consumed = True
        return True

    def observe_idle(self, idle_seconds: float) -> bool:
        normalized = max(0.0, float(idle_seconds))
        if normalized < IDLE_THRESHOLD_SECONDS:
            self.idle_cycle_consumed = False
            return False
        if self.idle_cycle_consumed:
            return False
        self.idle_cycle_consumed = True
        return True


def evaluate_proactive_policy(value: ProactivePolicyInput) -> ProactiveBlockReason:
    """Return a content-free reason suitable for tests and safe diagnostics."""

    if value.mode is ProactiveMode.OFF:
        return ProactiveBlockReason.MODE_OFF
    if value.trigger is ProactiveTrigger.IDLE and value.mode is not ProactiveMode.RESTRAINED:
        return ProactiveBlockReason.TRIGGER_DISABLED
    if _in_quiet_hours(
        _local_minute(value.now),
        value.quiet_start_minute,
        value.quiet_end_minute,
    ):
        return ProactiveBlockReason.QUIET_HOURS
    if value.displayed_today >= value.daily_limit:
        return ProactiveBlockReason.DAILY_LIMIT
    if value.paused_local_date == value.now.date().isoformat():
        return ProactiveBlockReason.PAUSED_TODAY
    if not value.pet_visible:
        return ProactiveBlockReason.PET_HIDDEN
    if value.conversation_active:
        return ProactiveBlockReason.CONVERSATION_ACTIVE
    if value.settings_open:
        return ProactiveBlockReason.SETTINGS_OPEN
    if value.session_locked:
        return ProactiveBlockReason.SESSION_LOCKED
    if value.fullscreen:
        return ProactiveBlockReason.FULLSCREEN
    if not value.data_writable:
        return ProactiveBlockReason.DATA_UNAVAILABLE
    return ProactiveBlockReason.ALLOWED


def build_proactive_request(now: datetime, trigger: ProactiveTrigger) -> ChatRequest:
    """Create a privacy-minimal greeting request without chat history or user memory."""

    trigger_text = "应用刚刚启动" if trigger is ProactiveTrigger.STARTUP else "用户已较长时间未输入"
    prompt = (
        f"当前本地时间是 {now.isoformat(timespec='minutes')}，{trigger_text}。"
        "请写一句不超过四十个汉字、克制、自然、不要求用户立刻回复的桌面陪伴问候。"
        "不要提及未获得的用户状态，不要复述规则，不要使用引号。"
    )
    request_id = uuid4().hex
    return ChatRequest(
        request_id=request_id,
        turn_id=f"proactive:{request_id}",
        attempt=1,
        messages=(
            PromptMessage(PromptRole.SYSTEM, build_capability_safety_boundary()),
            PromptMessage(PromptRole.SYSTEM, build_persona_core_prompt()),
            PromptMessage(PromptRole.USER, prompt),
        ),
        options=GenerationOptions(
            purpose=GenerationPurpose.PROACTIVE_GREETING,
            temperature=0.7,
            max_output_tokens=80,
        ),
    )


def validate_generated_greeting(value: object) -> str:
    """Accept one short single-line model result or reject it for local fallback."""

    if not isinstance(value, str):
        raise ValueError("generated greeting must be text")
    normalized = " ".join(value.strip().splitlines()).strip().strip("\"'“”‘’")
    if (
        not normalized
        or len(normalized) > MAX_GENERATED_GREETING_CHARS
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("generated greeting is invalid")
    return normalized


def _local_minute(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _in_quiet_hours(minute: int, start: int, end: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end
