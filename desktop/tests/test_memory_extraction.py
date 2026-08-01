from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from amadeus_desktop.memory_extraction import (
    CandidateRejectionReason,
    ExtractionPayloadError,
    ExtractionPayloadErrorCode,
    SensitiveCategory,
    contains_do_not_remember,
    detect_sensitive_content,
    parse_and_validate_extraction,
    parse_extraction_candidates,
    validate_extraction_candidates,
)
from amadeus_desktop.memory_models import (
    ExtractionSource,
    MemoryCandidate,
    MemoryKind,
    MemoryOperation,
    SourceRole,
)


def candidate_dict(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "preference",
        "operation": "add",
        "content": "用户不喜欢太甜的咖啡",
        "topic_key": "饮料／咖啡",
        "importance": 0.8,
        "confidence": 0.9,
        "source_message_ids": ["user-1"],
    }
    value.update(overrides)
    return value


def payload(*candidates: dict[str, object]) -> str:
    return json.dumps({"candidates": list(candidates)}, ensure_ascii=False)


def source(
    content: str = "我不喜欢太甜的咖啡",
    *,
    role: SourceRole = SourceRole.USER,
    message_id: str = "user-1",
) -> ExtractionSource:
    return ExtractionSource(message_id, role, content)


def test_strict_parser_returns_frozen_canonical_candidate() -> None:
    parsed = parse_extraction_candidates(payload(candidate_dict()))

    assert parsed == (
        MemoryCandidate(
            kind=MemoryKind.PREFERENCE,
            operation=MemoryOperation.ADD,
            content="用户不喜欢太甜的咖啡",
            topic_key="饮料:咖啡",
            importance=0.8,
            confidence=0.9,
            source_message_ids=("user-1",),
        ),
    )
    with pytest.raises(FrozenInstanceError):
        parsed[0].content = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("```json\n{}\n```", ExtractionPayloadErrorCode.INVALID_JSON),
        ('{"candidates": []} trailing', ExtractionPayloadErrorCode.INVALID_JSON),
        ('{"candidates": [], "extra": 1}', ExtractionPayloadErrorCode.ROOT_SCHEMA),
        ('{"candidates": [], "candidates": []}', ExtractionPayloadErrorCode.DUPLICATE_FIELD),
        ('{"candidates": NaN}', ExtractionPayloadErrorCode.INVALID_JSON),
        (
            json.dumps({"candidates": [candidate_dict()] * 6}, ensure_ascii=False),
            ExtractionPayloadErrorCode.TOO_MANY_CANDIDATES,
        ),
        (
            payload(candidate_dict(confidence=True)),
            ExtractionPayloadErrorCode.INVALID_VALUE,
        ),
        (
            payload(candidate_dict(unexpected="no")),
            ExtractionPayloadErrorCode.CANDIDATE_SCHEMA,
        ),
        (
            payload(candidate_dict(source_message_ids=[])),
            ExtractionPayloadErrorCode.INVALID_VALUE,
        ),
    ],
)
def test_strict_parser_fails_closed(raw: str, code: ExtractionPayloadErrorCode) -> None:
    with pytest.raises(ExtractionPayloadError) as caught:
        parse_extraction_candidates(raw)

    assert caught.value.code is code
    assert raw not in str(caught.value)


def test_parser_accepts_exactly_five_and_rejects_invalid_enum_and_range() -> None:
    assert len(parse_extraction_candidates(payload(*[candidate_dict() for _ in range(5)]))) == 5

    for invalid in (
        candidate_dict(type="guess"),
        candidate_dict(operation="replace"),
        candidate_dict(importance=-0.1),
        candidate_dict(confidence=1.01),
    ):
        with pytest.raises(ExtractionPayloadError) as caught:
            parse_extraction_candidates(payload(invalid))
        assert caught.value.code is ExtractionPayloadErrorCode.INVALID_VALUE


def test_provenance_whitelist_and_confidence_threshold() -> None:
    candidates = parse_extraction_candidates(
        payload(
            candidate_dict(confidence=0.65),
            candidate_dict(content="未知来源", source_message_ids=["missing"]),
            candidate_dict(content="引用助手", source_message_ids=["assistant-1"]),
            candidate_dict(content="置信度不足", confidence=0.649),
        )
    )
    sources = {
        "user-1": source(),
        "assistant-1": source(
            "模型猜测用户喜欢咖啡",
            role=SourceRole.ASSISTANT,
            message_id="assistant-1",
        ),
    }

    result = validate_extraction_candidates(candidates, sources)

    assert result.accepted == (candidates[0],)
    assert [item.reason for item in result.rejected] == [
        CandidateRejectionReason.UNKNOWN_SOURCE,
        CandidateRejectionReason.NON_USER_SOURCE,
        CandidateRejectionReason.LOW_CONFIDENCE,
    ]


@pytest.mark.parametrize(
    "text",
    [
        "这件事不要记住",
        "别把这段话写入长期记忆",
        "不用帮我保存这个",
        "Please don't remember this.",
        "This is off-the-record.",
    ],
)
def test_explicit_do_not_remember_suppresses_the_whole_batch(text: str) -> None:
    candidates = parse_extraction_candidates(payload(candidate_dict()))

    result = validate_extraction_candidates(candidates, {"user-1": source(text)})

    assert contains_do_not_remember(text)
    assert result.do_not_remember
    assert not result.accepted
    assert result.rejected[0].reason is CandidateRejectionReason.DO_NOT_REMEMBER


def test_do_not_forget_is_not_mistaken_for_an_opt_out() -> None:
    assert not contains_do_not_remember("不要忘记我喜欢咖啡")
    assert not contains_do_not_remember("我忘了带伞")


def test_huge_json_number_is_a_fixed_invalid_value_error() -> None:
    raw = payload(candidate_dict(confidence=10**1_000))
    with pytest.raises(ExtractionPayloadError) as caught:
        parse_extraction_candidates(raw)

    assert caught.value.code is ExtractionPayloadErrorCode.INVALID_VALUE


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("我的密码是 invalid-password-123", SensitiveCategory.PASSWORD_OR_SECRET),
        ("api_key: invalid-fake-secret", SensitiveCategory.PASSWORD_OR_SECRET),
        ("验证码是 123456", SensitiveCategory.VERIFICATION_CODE),
        ("信用卡号 4111 1111 1111 1111", SensitiveCategory.PAYMENT_INFORMATION),
        ("我被确诊为焦虑症", SensitiveCategory.MEDICAL_DIAGNOSIS),
        ("我认定对方已经违法", SensitiveCategory.LEGAL_CONCLUSION),
        ("我老板肯定是故意针对我", SensitiveCategory.THIRD_PARTY_INFERENCE),
    ],
)
def test_sensitive_and_subjective_content_is_rejected(
    text: str,
    category: SensitiveCategory,
) -> None:
    assert detect_sensitive_content(text) is category
    raw = payload(candidate_dict(content=text))
    result = parse_and_validate_extraction(raw, {"user-1": source(text)})

    assert not result.accepted
    assert result.rejected[0].reason.value == category.value


def test_sensitive_source_cannot_be_hidden_by_a_vague_candidate() -> None:
    result = parse_and_validate_extraction(
        payload(candidate_dict(content="用户刚收到一条私人消息")),
        {"user-1": source("短信验证码为 654321")},
    )

    assert not result.accepted
    assert result.rejected[0].reason is CandidateRejectionReason.VERIFICATION_CODE


@pytest.mark.parametrize(
    "text",
    [
        "gh" + "p_" + "A" * 24,
        "AK" + "IA" + "0" * 16,
        "ey" + "J" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        "Ab3_Cd4+Ef5=Gh6-Ij7_Kl8+Mn9=Op0-",
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_unlabelled_common_or_high_entropy_secret_formats_are_rejected(text: str) -> None:
    assert detect_sensitive_content(text) is SensitiveCategory.PASSWORD_OR_SECRET


@pytest.mark.parametrize(
    "text",
    [
        "用户偏好使用支付宝付款",
        "用户下周要去医院复诊",
        "用户正在学习法律基础",
        "用户的朋友喜欢喝咖啡",
    ],
)
def test_safe_payment_method_health_event_and_direct_facts_are_not_overfiltered(
    text: str,
) -> None:
    assert detect_sensitive_content(text) is None
    result = parse_and_validate_extraction(
        payload(candidate_dict(content=text)),
        {"user-1": source(text)},
    )
    assert len(result.accepted) == 1
    assert not result.rejected
