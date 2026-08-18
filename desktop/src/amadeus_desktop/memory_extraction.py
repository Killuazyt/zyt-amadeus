"""Strict parsing and local admission rules for extracted user memories."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from amadeus_desktop.memory_models import (
    ExtractionSource,
    MemoryCandidate,
    MemoryKind,
    MemoryOperation,
    MemorySubjectScope,
    SourceRole,
)
from amadeus_desktop.memory_search import normalize_topic_key
from amadeus_desktop.storage_models import CompanionCueReason

MAX_CANDIDATES = 5
MAX_EXTRACTION_PAYLOAD_CHARS = 65_536
MAX_MEMORY_CONTENT_CHARS = 2_000
MAX_TOPIC_KEY_CHARS = 200
MAX_SOURCE_IDS = 20
MAX_COMPANION_CUES = 2
MAX_COMPANION_CUE_TOPIC_CHARS = 120
MAX_COMPANION_CUE_TEXT_CHARS = 240

_LEGACY_ROOT_FIELDS = frozenset({"candidates"})
_ROOT_FIELDS = frozenset({"candidates", "companion_cues"})
_LEGACY_CANDIDATE_FIELDS = frozenset(
    {
        "type",
        "operation",
        "content",
        "topic_key",
        "importance",
        "confidence",
        "source_message_ids",
    }
)
_CANDIDATE_FIELDS = _LEGACY_CANDIDATE_FIELDS | {
    "subject_scope",
    "event_started_at",
    "event_ended_at",
    "time_confidence",
    "correction_explicit",
}
_COMPANION_CUE_FIELDS = frozenset(
    {"topic", "follow_up_text", "reason", "confidence", "source_message_ids"}
)

_EXPLICIT_RETURN = re.compile(
    r"(?:稍后|晚点|回头|下次|之后|以后).{0,20}(?:继续|再聊|回来|接着)|"
    r"(?:continue|come back|talk about).{0,24}(?:later|next time)",
    re.IGNORECASE,
)
_PENDING_RESULT = re.compile(
    r"(?:等|等待).{0,24}(?:结果|回复|消息|答复)|(?:结果|回复|消息|答复).{0,16}(?:出来|到了|收到)|"
    r"(?:wait(?:ing)? for|when I get).{0,30}(?:result|reply|response|news)",
    re.IGNORECASE,
)
_PROMISED_UPDATE = re.compile(
    r"(?:我会|我再|到时候|之后|回头).{0,24}(?:告诉你|跟你说|更新|反馈|汇报)|"
    r"(?:I(?:'ll| will)).{0,30}(?:tell you|update you|let you know|report back)",
    re.IGNORECASE,
)

_DO_NOT_REMEMBER_PATTERNS = (
    re.compile(r"(?:不要|别|不用|不必|不准)(?:帮我)?(?:记住|记录|记下来|保存|存储)"),
    re.compile(r"(?:不要|别)(?:把)?.{0,12}(?:放进|加入|写入)(?:长期)?记忆"),
    re.compile(
        r"(?:请(?:你)?|麻烦你?|把.{0,12})?忘掉(?:这|那|刚才|上面)"
        r"(?:件事|句话|段话|条消息|些内容)?"
    ),
    re.compile(r"\b(?:do\s+not|don['’]t)\s+(?:remember|save|store|record)\b"),
    re.compile(r"\bforget\s+(?:this|that|it)\b"),
    re.compile(r"\boff[ -]the[ -]record\b"),
)

_PASSWORD = re.compile(
    r"(?:密码|口令|pass(?:word|code)?|\bpin\b)\s*(?:是|为|[:：=])\s*\S{4,}",
    re.IGNORECASE,
)
_API_KEY = re.compile(
    r"(?:api\s*[-_ ]?key|secret\s*key|access\s*token|密钥|访问令牌|bearer)"
    r"\s*(?:是|为|[:：=])\s*\S{6,}",
    re.IGNORECASE,
)
_API_KEY_PREFIX = re.compile(r"\b(?:sk|tp)-[a-z0-9_-]{8,}\b", re.IGNORECASE)
_KNOWN_SECRET_FORMAT = re.compile(
    r"(?:\bgh[pousr]_[a-z0-9]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_HIGH_ENTROPY_TOKEN = re.compile(r"(?<!\w)[A-Za-z0-9_+/=-]{32,}(?!\w)")
_VERIFICATION_CODE = re.compile(
    r"(?:验证码|短信码|动态码|一次性密码|verification\s*code|\botp\b)"
    r"\s*(?:是|为|[:：=])?\s*\d{4,8}\b",
    re.IGNORECASE,
)
_PAYMENT_DETAIL = re.compile(
    r"(?:银行卡号|信用卡号|借记卡号|支付密码|交易密码|安全码|cvv|收款码|付款码)"
    r"\s*(?:是|为|[:：=])?\s*\S{3,}",
    re.IGNORECASE,
)
_CARD_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_MEDICAL_DIAGNOSIS = re.compile(
    r"(?:确诊|诊断(?:为|出|是)|患有|得了|罹患|"
    r"抑郁症|焦虑症|双相情感障碍|精神分裂症|癌症|糖尿病|高血压)",
    re.IGNORECASE,
)
_LEGAL_CONCLUSION = re.compile(
    r"(?:违法|犯法|犯罪|有罪|无罪|非法|侵权|诈骗犯|"
    r"应当起诉|可以起诉|负(?:有)?法律责任|法律上(?:属于|构成|认定))",
    re.IGNORECASE,
)
_THIRD_PARTY = re.compile(
    r"(?:他|她|他们|她们|老板|同事|朋友|家人|父母|伴侣|老师|同学|某人)"
    r".{0,24}(?:可能|大概|也许|肯定|一定|故意|心里|其实|应该是|看起来|"
    r"似乎|八成|撒谎|看不起|不在乎|嫉妒)",
    re.IGNORECASE,
)
_INFERENCE_THIRD_PARTY = re.compile(
    r"(?:认为|猜测|怀疑|感觉).{0,20}"
    r"(?:他|她|他们|她们|老板|同事|朋友|家人|父母|伴侣|老师|同学|某人)",
    re.IGNORECASE,
)


class ExtractionPayloadErrorCode(StrEnum):
    """Stable parse errors safe for logs and retry scheduling."""

    TOO_LARGE = "too_large"
    INVALID_JSON = "invalid_json"
    DUPLICATE_FIELD = "duplicate_field"
    ROOT_SCHEMA = "root_schema"
    TOO_MANY_CANDIDATES = "too_many_candidates"
    CANDIDATE_SCHEMA = "candidate_schema"
    INVALID_VALUE = "invalid_value"


_PAYLOAD_ERROR_MESSAGES = {
    ExtractionPayloadErrorCode.TOO_LARGE: "记忆提炼结果超过本地校验上限。",
    ExtractionPayloadErrorCode.INVALID_JSON: "记忆提炼结果不是严格 JSON。",
    ExtractionPayloadErrorCode.DUPLICATE_FIELD: "记忆提炼结果包含重复字段。",
    ExtractionPayloadErrorCode.ROOT_SCHEMA: "记忆提炼结果的根结构无效。",
    ExtractionPayloadErrorCode.TOO_MANY_CANDIDATES: "记忆提炼候选超过五条。",
    ExtractionPayloadErrorCode.CANDIDATE_SCHEMA: "记忆提炼候选字段无效。",
    ExtractionPayloadErrorCode.INVALID_VALUE: "记忆提炼候选值无效。",
}


class ExtractionPayloadError(ValueError):
    """A fixed-message error that never embeds the remote payload."""

    def __init__(self, code: ExtractionPayloadErrorCode) -> None:
        self.code = code
        super().__init__(_PAYLOAD_ERROR_MESSAGES[code])


class CandidateRejectionReason(StrEnum):
    """Stable local reasons for not admitting a parsed candidate."""

    DO_NOT_REMEMBER = "do_not_remember"
    UNKNOWN_SOURCE = "unknown_source"
    NON_USER_SOURCE = "non_user_source"
    LOW_CONFIDENCE = "low_confidence"
    PASSWORD_OR_SECRET = "password_or_secret"
    VERIFICATION_CODE = "verification_code"
    PAYMENT_INFORMATION = "payment_information"
    MEDICAL_DIAGNOSIS = "medical_diagnosis"
    LEGAL_CONCLUSION = "legal_conclusion"
    THIRD_PARTY_INFERENCE = "third_party_inference"


class CompanionCueRejectionReason(StrEnum):
    DO_NOT_REMEMBER = "do_not_remember"
    UNKNOWN_SOURCE = "unknown_source"
    NON_USER_SOURCE = "non_user_source"
    LOW_CONFIDENCE = "low_confidence"
    SENSITIVE_CONTENT = "sensitive_content"
    UNSUPPORTED_INTENT = "unsupported_intent"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """A rejected candidate paired only with a stable local reason."""

    candidate: MemoryCandidate
    reason: CandidateRejectionReason


@dataclass(frozen=True, slots=True)
class ExtractionValidationResult:
    """Admission result suitable for audit counters without raw-text logging."""

    accepted: tuple[MemoryCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    do_not_remember: bool = False


@dataclass(frozen=True, slots=True)
class CompanionCueCandidate:
    topic: str
    follow_up_text: str
    reason: CompanionCueReason
    confidence: float
    source_message_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RejectedCompanionCueCandidate:
    candidate: CompanionCueCandidate
    reason: CompanionCueRejectionReason


@dataclass(frozen=True, slots=True)
class ExtractionBundleValidationResult:
    memories: ExtractionValidationResult
    companion_cues: tuple[CompanionCueCandidate, ...]
    rejected_companion_cues: tuple[RejectedCompanionCueCandidate, ...]


class SensitiveCategory(StrEnum):
    """Sensitive or unsupported assertions forbidden from durable memory."""

    PASSWORD_OR_SECRET = "password_or_secret"
    VERIFICATION_CODE = "verification_code"
    PAYMENT_INFORMATION = "payment_information"
    MEDICAL_DIAGNOSIS = "medical_diagnosis"
    LEGAL_CONCLUSION = "legal_conclusion"
    THIRD_PARTY_INFERENCE = "third_party_inference"


_SENSITIVE_TO_REJECTION = {
    SensitiveCategory.PASSWORD_OR_SECRET: CandidateRejectionReason.PASSWORD_OR_SECRET,
    SensitiveCategory.VERIFICATION_CODE: CandidateRejectionReason.VERIFICATION_CODE,
    SensitiveCategory.PAYMENT_INFORMATION: CandidateRejectionReason.PAYMENT_INFORMATION,
    SensitiveCategory.MEDICAL_DIAGNOSIS: CandidateRejectionReason.MEDICAL_DIAGNOSIS,
    SensitiveCategory.LEGAL_CONCLUSION: CandidateRejectionReason.LEGAL_CONCLUSION,
    SensitiveCategory.THIRD_PARTY_INFERENCE: CandidateRejectionReason.THIRD_PARTY_INFERENCE,
}


def parse_extraction_candidates(raw_json: str) -> tuple[MemoryCandidate, ...]:
    """Parse the exact ``{"candidates": [...]}`` extraction contract.

    Markdown fences, trailing prose, duplicate keys, non-finite numbers, extra
    fields, coercions, and more than five candidates all fail closed.
    """

    if not isinstance(raw_json, str):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_JSON)
    if len(raw_json) > MAX_EXTRACTION_PAYLOAD_CHARS:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.TOO_LARGE)

    try:
        payload = json.loads(
            raw_json,
            object_pairs_hook=_object_without_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateField as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.DUPLICATE_FIELD) from exc
    except (json.JSONDecodeError, _InvalidJsonConstant, RecursionError) as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_JSON) from exc

    candidates, _cues = _parse_payload_object(payload)
    return candidates


def parse_extraction_payload(
    raw_json: str,
) -> tuple[tuple[MemoryCandidate, ...], tuple[CompanionCueCandidate, ...]]:
    """Parse the v7 extraction contract while accepting legacy candidate-only output."""

    if not isinstance(raw_json, str):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_JSON)
    if len(raw_json) > MAX_EXTRACTION_PAYLOAD_CHARS:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.TOO_LARGE)
    try:
        payload = json.loads(
            raw_json,
            object_pairs_hook=_object_without_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateField as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.DUPLICATE_FIELD) from exc
    except (json.JSONDecodeError, _InvalidJsonConstant, RecursionError) as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_JSON) from exc
    return _parse_payload_object(payload)


def validate_extraction_candidates(
    candidates: Sequence[MemoryCandidate],
    sources: Mapping[str, ExtractionSource],
) -> ExtractionValidationResult:
    """Apply provenance, confidence, opt-out, and sensitive-data admission rules."""

    if len(candidates) > MAX_CANDIDATES:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.TOO_MANY_CANDIDATES)

    source_values = tuple(sources.values())
    if any(
        source.role is SourceRole.USER and contains_do_not_remember(source.content)
        for source in source_values
    ):
        return ExtractionValidationResult(
            accepted=(),
            rejected=tuple(
                RejectedCandidate(candidate, CandidateRejectionReason.DO_NOT_REMEMBER)
                for candidate in candidates
            ),
            do_not_remember=True,
        )

    accepted: list[MemoryCandidate] = []
    rejected: list[RejectedCandidate] = []
    for candidate in candidates:
        reason = _candidate_rejection_reason(candidate, sources)
        if reason is None:
            accepted.append(candidate)
        else:
            rejected.append(RejectedCandidate(candidate, reason))
    return ExtractionValidationResult(tuple(accepted), tuple(rejected))


def parse_and_validate_extraction(
    raw_json: str,
    sources: Mapping[str, ExtractionSource],
) -> ExtractionValidationResult:
    """Strictly parse and then locally validate one extraction response."""

    return validate_extraction_candidates(parse_extraction_candidates(raw_json), sources)


def parse_and_validate_extraction_bundle(
    raw_json: str,
    sources: Mapping[str, ExtractionSource],
) -> ExtractionBundleValidationResult:
    """Validate memories and follow-up suggestions from the same model response."""

    memories, cues = parse_extraction_payload(raw_json)
    memory_result = validate_extraction_candidates(memories, sources)
    accepted: list[CompanionCueCandidate] = []
    rejected: list[RejectedCompanionCueCandidate] = []
    seen: set[tuple[object, ...]] = set()
    opt_out = any(
        source.role is SourceRole.USER and contains_do_not_remember(source.content)
        for source in sources.values()
    )
    for cue in cues:
        reason = _companion_cue_rejection_reason(cue, sources, opt_out=opt_out)
        dedupe = (
            unicodedata.normalize("NFKC", cue.topic).casefold(),
            unicodedata.normalize("NFKC", cue.follow_up_text).casefold(),
            cue.reason,
            cue.source_message_ids,
        )
        if reason is None and dedupe in seen:
            reason = CompanionCueRejectionReason.DUPLICATE
        if reason is None:
            seen.add(dedupe)
            accepted.append(cue)
        else:
            rejected.append(RejectedCompanionCueCandidate(cue, reason))
    return ExtractionBundleValidationResult(memory_result, tuple(accepted), tuple(rejected))


def contains_do_not_remember(text: str) -> bool:
    """Return whether a user message explicitly opts this turn out of memory."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    return any(pattern.search(normalized) is not None for pattern in _DO_NOT_REMEMBER_PATTERNS)


def detect_sensitive_content(text: str) -> SensitiveCategory | None:
    """Classify content that must never become a durable user memory."""

    normalized = unicodedata.normalize("NFKC", text)
    if (
        _PASSWORD.search(normalized)
        or _API_KEY.search(normalized)
        or _API_KEY_PREFIX.search(normalized)
        or _KNOWN_SECRET_FORMAT.search(normalized)
        or _contains_high_entropy_token(normalized)
    ):
        return SensitiveCategory.PASSWORD_OR_SECRET
    if _VERIFICATION_CODE.search(normalized):
        return SensitiveCategory.VERIFICATION_CODE
    if _PAYMENT_DETAIL.search(normalized) or _CARD_NUMBER.search(normalized):
        return SensitiveCategory.PAYMENT_INFORMATION
    if _MEDICAL_DIAGNOSIS.search(normalized):
        return SensitiveCategory.MEDICAL_DIAGNOSIS
    if _LEGAL_CONCLUSION.search(normalized):
        return SensitiveCategory.LEGAL_CONCLUSION
    if _THIRD_PARTY.search(normalized) or _INFERENCE_THIRD_PARTY.search(normalized):
        return SensitiveCategory.THIRD_PARTY_INFERENCE
    return None


def _contains_high_entropy_token(text: str) -> bool:
    for match in _HIGH_ENTROPY_TOKEN.finditer(text):
        token = match.group(0)
        classes = sum(
            (
                any(character.islower() for character in token),
                any(character.isupper() for character in token),
                any(character.isdigit() for character in token),
                any(character in "_+/=-" for character in token),
            )
        )
        if classes < 3:
            continue
        counts = Counter(token)
        entropy = -sum(
            (count / len(token)) * math.log2(count / len(token)) for count in counts.values()
        )
        if entropy >= 3.5:
            return True
    return False


def _parse_candidate(value: Any) -> MemoryCandidate:
    if not isinstance(value, dict) or frozenset(value) not in {
        _LEGACY_CANDIDATE_FIELDS,
        _CANDIDATE_FIELDS,
    }:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.CANDIDATE_SCHEMA)

    try:
        kind = MemoryKind(_required_text(value["type"], 64))
        operation = MemoryOperation(_required_text(value["operation"], 64))
    except ValueError as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE) from exc

    content = _required_text(value["content"], MAX_MEMORY_CONTENT_CHARS)
    raw_topic_key = _required_text(value["topic_key"], MAX_TOPIC_KEY_CHARS)
    topic_key = normalize_topic_key(raw_topic_key)
    if not topic_key:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)

    importance = _unit_number(value["importance"])
    confidence = _unit_number(value["confidence"])
    source_message_ids = _source_ids(value["source_message_ids"])
    subject_scope = MemorySubjectScope.USER
    event_started_at = None
    event_ended_at = None
    time_confidence = None
    correction_explicit = False
    if frozenset(value) == _CANDIDATE_FIELDS:
        try:
            subject_scope = MemorySubjectScope(_required_text(value["subject_scope"], 32))
        except ValueError as exc:
            raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE) from exc
        if subject_scope is MemorySubjectScope.COMPANION:
            raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
        event_started_at = _optional_timestamp(value["event_started_at"])
        event_ended_at = _optional_timestamp(value["event_ended_at"])
        raw_time_confidence = value["time_confidence"]
        time_confidence = None if raw_time_confidence is None else _unit_number(raw_time_confidence)
        if not isinstance(value["correction_explicit"], bool):
            raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
        correction_explicit = bool(value["correction_explicit"])
        if kind is not MemoryKind.EVENT and any(
            item is not None for item in (event_started_at, event_ended_at, time_confidence)
        ):
            raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    return MemoryCandidate(
        kind=kind,
        operation=operation,
        content=content,
        topic_key=topic_key,
        importance=importance,
        confidence=confidence,
        source_message_ids=source_message_ids,
        subject_scope=subject_scope,
        event_started_at=event_started_at,
        event_ended_at=event_ended_at,
        time_confidence=time_confidence,
        correction_explicit=correction_explicit,
    )


def _parse_payload_object(
    payload: Any,
) -> tuple[tuple[MemoryCandidate, ...], tuple[CompanionCueCandidate, ...]]:
    if not isinstance(payload, dict) or frozenset(payload) not in {
        _LEGACY_ROOT_FIELDS,
        _ROOT_FIELDS,
    }:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.ROOT_SCHEMA)
    raw_candidates = payload["candidates"]
    raw_cues = payload.get("companion_cues", [])
    if not isinstance(raw_candidates, list) or not isinstance(raw_cues, list):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.ROOT_SCHEMA)
    if len(raw_candidates) > MAX_CANDIDATES or len(raw_cues) > MAX_COMPANION_CUES:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.TOO_MANY_CANDIDATES)
    return (
        tuple(_parse_candidate(candidate) for candidate in raw_candidates),
        tuple(_parse_companion_cue(candidate) for candidate in raw_cues),
    )


def _parse_companion_cue(value: Any) -> CompanionCueCandidate:
    if not isinstance(value, dict) or frozenset(value) != _COMPANION_CUE_FIELDS:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.CANDIDATE_SCHEMA)
    try:
        reason = CompanionCueReason(_required_text(value["reason"], 64))
    except ValueError as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE) from exc
    if reason is CompanionCueReason.MEMORY_AUTHORIZED:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    source_message_ids = _source_ids(value["source_message_ids"])
    if len(source_message_ids) > 3:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    return CompanionCueCandidate(
        topic=_required_text(value["topic"], MAX_COMPANION_CUE_TOPIC_CHARS),
        follow_up_text=_required_text(value["follow_up_text"], MAX_COMPANION_CUE_TEXT_CHARS),
        reason=reason,
        confidence=_unit_number(value["confidence"]),
        source_message_ids=source_message_ids,
    )


def _required_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    stripped = value.strip()
    if not stripped or len(stripped) > maximum or "\x00" in stripped:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    return stripped


def _unit_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    try:
        number = float(value)
    except OverflowError as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE) from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    return number


def _optional_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    text = _required_text(value, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE) from exc
    if parsed.tzinfo is None:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _source_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_SOURCE_IDS:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    source_ids = tuple(_required_text(item, 128) for item in value)
    if len(set(source_ids)) != len(source_ids):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.INVALID_VALUE)
    return source_ids


def _candidate_rejection_reason(
    candidate: MemoryCandidate,
    sources: Mapping[str, ExtractionSource],
) -> CandidateRejectionReason | None:
    referenced_sources: list[ExtractionSource] = []
    for source_id in candidate.source_message_ids:
        source = sources.get(source_id)
        if source is None:
            return CandidateRejectionReason.UNKNOWN_SOURCE
        if source.role is not SourceRole.USER:
            return CandidateRejectionReason.NON_USER_SOURCE
        referenced_sources.append(source)

    if candidate.confidence < 0.65:
        return CandidateRejectionReason.LOW_CONFIDENCE

    category = detect_sensitive_content(candidate.content)
    if category is None:
        for source in referenced_sources:
            category = detect_sensitive_content(source.content)
            if category is not None:
                break
    return _SENSITIVE_TO_REJECTION.get(category) if category is not None else None


def _companion_cue_rejection_reason(
    candidate: CompanionCueCandidate,
    sources: Mapping[str, ExtractionSource],
    *,
    opt_out: bool,
) -> CompanionCueRejectionReason | None:
    if opt_out:
        return CompanionCueRejectionReason.DO_NOT_REMEMBER
    referenced: list[ExtractionSource] = []
    for source_id in candidate.source_message_ids:
        source = sources.get(source_id)
        if source is None:
            return CompanionCueRejectionReason.UNKNOWN_SOURCE
        if source.role is not SourceRole.USER:
            return CompanionCueRejectionReason.NON_USER_SOURCE
        referenced.append(source)
    if candidate.confidence < 0.80:
        return CompanionCueRejectionReason.LOW_CONFIDENCE
    if detect_sensitive_content(candidate.topic) or detect_sensitive_content(
        candidate.follow_up_text
    ):
        return CompanionCueRejectionReason.SENSITIVE_CONTENT
    if any(detect_sensitive_content(source.content) for source in referenced):
        return CompanionCueRejectionReason.SENSITIVE_CONTENT
    combined = "\n".join(source.content for source in referenced)
    pattern = {
        CompanionCueReason.EXPLICIT_RETURN: _EXPLICIT_RETURN,
        CompanionCueReason.PENDING_RESULT: _PENDING_RESULT,
        CompanionCueReason.USER_PROMISED_UPDATE: _PROMISED_UPDATE,
    }[candidate.reason]
    if pattern.search(combined) is None:
        return CompanionCueRejectionReason.UNSUPPORTED_INTENT
    return None


class _DuplicateField(ValueError):
    pass


class _InvalidJsonConstant(ValueError):
    pass


def _object_without_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise _InvalidJsonConstant
