"""Local, deterministic policy for the user-visible Focus/Pondering mode."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

DEFAULT_FOCUSED_FIRST_CHUNK_TIMEOUT_MS = 30_000
FOCUS_STATUS_TEXT = "凝神中 · 正在认真梳理这个问题…"
FOCUS_STATUS_TOOLTIP = (
    "凝神模式只表示 Amadeus 正在整理并等待回复；"
    "不会展示、保存或记录模型的隐藏思维链。"
)


class FocusModeReason(StrEnum):
    """Privacy-safe reason categories that never contain the user's text."""

    COMPLEX = "complex"
    EMOTIONAL = "emotional"
    COMPLEX_AND_EMOTIONAL = "complex_and_emotional"


@dataclass(frozen=True, slots=True)
class FocusModeDecision:
    """A local presentation decision with no prompt or model-derived content."""

    reason: FocusModeReason | None = None

    @property
    def active(self) -> bool:
        return self.reason is not None


_EMOTIONAL_MARKERS = (
    "难过",
    "焦虑",
    "害怕",
    "恐惧",
    "不安",
    "崩溃",
    "撑不住",
    "受不了",
    "想哭",
    "痛苦",
    "孤独",
    "委屈",
    "绝望",
    "迷茫",
    "失眠",
    "压力很大",
    "喘不过气",
    "心里很乱",
    "很累",
    "好累",
    "生气",
    "愤怒",
    "内疚",
    "自责",
    "羞愧",
    "被抛弃",
    "没人理解",
    "抑郁",
    "不想活",
    "活不下去",
    "自杀",
    "伤害自己",
)
_ENGLISH_EMOTIONAL_PATTERN = re.compile(
    r"\b(?:anxious|afraid|angry|depressed|hopeless|lonely|overwhelmed|scared|"
    r"suicidal|terrified|upset)\b"
)
_STRONG_COMPLEX_MARKERS = (
    "详细分析",
    "深入分析",
    "系统分析",
    "全面分析",
    "逐步推导",
    "一步一步",
    "权衡利弊",
    "制定方案",
    "帮我规划",
    "完整规划",
    "根因分析",
    "综合判断",
    "多角度分析",
    "step by step",
    "trade-off",
    "root cause",
)
_COMPLEX_MARKERS = (
    "分析",
    "比较",
    "权衡",
    "利弊",
    "规划",
    "方案",
    "推导",
    "论证",
    "评估",
    "解释原因",
    "多步骤",
    "分别说明",
    "逐项",
    "复盘",
    "取舍",
    "影响因素",
    "根因",
    "analyze",
    "compare",
    "evaluate",
)
_STRUCTURE_MARKERS = (
    "同时",
    "并且",
    "以及",
    "还要",
    "另外",
    "分别",
    "一方面",
    "另一方面",
    "首先",
    "其次",
    "最后",
)
_LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*")


def classify_focus_mode(text: str) -> FocusModeDecision:
    """Classify only presentation needs; never infer or expose reasoning content."""

    if not isinstance(text, str):
        raise TypeError("focus mode input must be text")
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    if not normalized:
        return FocusModeDecision()

    emotional = any(marker in normalized for marker in _EMOTIONAL_MARKERS) or bool(
        _ENGLISH_EMOTIONAL_PATTERN.search(normalized)
    )
    compact_length = sum(not character.isspace() for character in normalized)
    keyword_hits = sum(marker in normalized for marker in _COMPLEX_MARKERS)
    structure_hits = sum(marker in normalized for marker in _STRUCTURE_MARKERS)
    question_count = normalized.count("?") + normalized.count("？")
    lines = [line for line in normalized.splitlines() if line.strip()]
    listed_lines = sum(bool(_LIST_PREFIX.match(line)) for line in lines)

    complex_input = (
        any(marker in normalized for marker in _STRONG_COMPLEX_MARKERS)
        or compact_length >= 120
        or keyword_hits >= 2
        or (compact_length >= 48 and keyword_hits >= 1)
        or (compact_length >= 32 and question_count >= 2)
        or structure_hits >= 3
        or listed_lines >= 2
        or len(lines) >= 4
    )

    if emotional and complex_input:
        return FocusModeDecision(FocusModeReason.COMPLEX_AND_EMOTIONAL)
    if emotional:
        return FocusModeDecision(FocusModeReason.EMOTIONAL)
    if complex_input:
        return FocusModeDecision(FocusModeReason.COMPLEX)
    return FocusModeDecision()
