from __future__ import annotations

import pytest

from amadeus_desktop.focus_mode import (
    FocusModeReason,
    classify_focus_mode,
)


@pytest.mark.parametrize(
    "text",
    [
        "你好",
        "今天星期几？",
        "请分析这个短句",
        "我今天很开心",
    ],
)
def test_short_ordinary_inputs_do_not_enter_focus_mode(text: str) -> None:
    assert not classify_focus_mode(text).active


@pytest.mark.parametrize(
    "text",
    [
        "请比较方案 A 和方案 B 的利弊，并评估长期风险。",
        "请帮我做一个完整规划：\n1. 分析现状\n2. 制定方案\n3. 说明取舍",
        "请一步一步解释这些因素怎样相互影响，并给出综合判断。",
    ],
)
def test_structured_or_multi_factor_inputs_enter_complex_focus_mode(text: str) -> None:
    decision = classify_focus_mode(text)

    assert decision.active
    assert decision.reason is FocusModeReason.COMPLEX


def test_emotional_input_enters_focus_mode_without_storing_the_text() -> None:
    text = "我最近很焦虑，也有点撑不住，不知道该怎么办。"

    decision = classify_focus_mode(text)

    assert decision.reason is FocusModeReason.EMOTIONAL
    assert text not in repr(decision)


def test_complex_emotional_input_uses_combined_privacy_safe_reason() -> None:
    decision = classify_focus_mode(
        "我最近很焦虑，请详细分析可能的原因，并帮我制定一个分步骤的调整方案。"
    )

    assert decision.reason is FocusModeReason.COMPLEX_AND_EMOTIONAL


def test_non_text_input_is_rejected() -> None:
    with pytest.raises(TypeError, match="must be text"):
        classify_focus_mode(None)  # type: ignore[arg-type]
