"""Strict parsing and local admission rules for extracted user memories."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from amadeus_desktop.memory_models import (
    ExtractionSource,
    MemoryCandidate,
    MemoryKind,
    MemoryOperation,
    SourceRole,
)
from amadeus_desktop.memory_search import normalize_topic_key

MAX_CANDIDATES = 5
MAX_EXTRACTION_PAYLOAD_CHARS = 65_536
MAX_MEMORY_CONTENT_CHARS = 2_000
MAX_TOPIC_KEY_CHARS = 200
MAX_SOURCE_IDS = 20

_ROOT_FIELDS = frozenset({"candidates"})
_CANDIDATE_FIELDS = frozenset(
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

    if not isinstance(payload, dict) or frozenset(payload) != _ROOT_FIELDS:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.ROOT_SCHEMA)
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list):
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.ROOT_SCHEMA)
    if len(raw_candidates) > MAX_CANDIDATES:
        raise ExtractionPayloadError(ExtractionPayloadErrorCode.TOO_MANY_CANDIDATES)
    return tuple(_parse_candidate(candidate) for candidate in raw_candidates)


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
    if not isinstance(value, dict) or frozenset(value) != _CANDIDATE_FIELDS:
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
    return MemoryCandidate(
        kind=kind,
        operation=operation,
        content=content,
        topic_key=topic_key,
        importance=importance,
        confidence=confidence,
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
