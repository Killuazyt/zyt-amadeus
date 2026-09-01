"""Deterministic, provider-free parsing for explicit one-shot time commitments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from amadeus_desktop.storage_models import TemporalCommitmentKind

MAX_CONTENT_CHARS = 500
MAX_FUTURE_DAYS = 5 * 366

_ZH_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_WEEKDAYS = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_ZH_FOLLOWUP = re.compile(
    r"(?:(?:到时|到时候)\s*(?:再)?\s*(?:问问我|问我)|记得\s*(?:问问我|问我))",
    re.IGNORECASE,
)
_ZH_REMINDER = re.compile(r"提醒我|记得提醒我?|(?:到时|到时候)\s*(?:叫我|提醒我)")
_EN_FOLLOWUP = re.compile(r"\b(?:check\s+in\s+with\s+me|ask\s+me)\b", re.IGNORECASE)
_EN_REMINDER = re.compile(r"\bremind\s+me\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TemporalParseResult:
    matched: bool
    kind: TemporalCommitmentKind | None = None
    content: str = ""
    due_at_utc: datetime | None = None
    original_local_time: str | None = None
    timezone_name: str | None = None
    utc_offset_minutes: int | None = None
    issue: str | None = None

    @property
    def resolved(self) -> bool:
        return self.matched and self.due_at_utc is not None and self.issue is None


def parse_temporal_intent(text: str, *, now: datetime | None = None) -> TemporalParseResult:
    """Parse only explicit reminder/follow-up commands and never infer ordinary events."""

    raw = " ".join(str(text).strip().split())
    if not raw:
        return TemporalParseResult(False)
    current = now if now is not None else datetime.now().astimezone()
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    intent_match, kind = _intent(raw)
    if intent_match is None or kind is None:
        return TemporalParseResult(False)

    due, consumed, issue = _parse_due(raw, current)
    content = _content_without_tokens(raw, (intent_match.span(), *consumed))
    if not content:
        content = "到时提醒我" if kind is TemporalCommitmentKind.REMINDER else "到时问问我"
    content = content[:MAX_CONTENT_CHARS]
    if due is None:
        return TemporalParseResult(True, kind, content, issue=issue or "missing_time")
    if due <= current:
        return TemporalParseResult(True, kind, content, issue="past_time")
    if due - current > timedelta(days=MAX_FUTURE_DAYS):
        return TemporalParseResult(True, kind, content, issue="too_far")
    offset = due.utcoffset()
    return TemporalParseResult(
        True,
        kind,
        content,
        due.astimezone(UTC),
        due.isoformat(timespec="minutes"),
        due.tzname() or "local",
        None if offset is None else round(offset.total_seconds() / 60),
        None,
    )


def is_reminder_list_command(text: str) -> bool:
    normalized = " ".join(str(text).strip().casefold().split())
    return normalized in {"查看提醒", "我的提醒", "提醒列表", "show reminders", "my reminders"}


def is_reminder_cancel_command(text: str) -> bool:
    normalized = " ".join(str(text).strip().casefold().split())
    return normalized in {"取消提醒", "管理提醒", "cancel reminder", "manage reminders"}


def _intent(text: str) -> tuple[re.Match[str] | None, TemporalCommitmentKind | None]:
    candidates: list[tuple[int, re.Match[str], TemporalCommitmentKind]] = []
    for pattern, kind in (
        (_ZH_FOLLOWUP, TemporalCommitmentKind.SCHEDULED_FOLLOWUP),
        (_EN_FOLLOWUP, TemporalCommitmentKind.SCHEDULED_FOLLOWUP),
        (_ZH_REMINDER, TemporalCommitmentKind.REMINDER),
        (_EN_REMINDER, TemporalCommitmentKind.REMINDER),
    ):
        match = pattern.search(text)
        if match is not None:
            candidates.append((match.start(), match, kind))
    if not candidates:
        return None, None
    _position, match, kind = min(candidates, key=lambda item: item[0])
    return match, kind


def _parse_due(
    text: str,
    now: datetime,
) -> tuple[datetime | None, tuple[tuple[int, int], ...], str | None]:
    relative = re.search(
        r"(?:(?:在|in)\s*)?"
        r"(?P<n>\d{1,4}|[零〇一二两三四五六七八九十百]+)\s*"
        r"(?P<u>分钟|分|小时|天|minutes?|mins?|hours?|hrs?|days?)\s*(?:后|later|from now)?",
        text,
        re.IGNORECASE,
    )
    if relative is not None:
        amount = _number(relative.group("n"))
        if amount <= 0:
            return None, (relative.span(),), "invalid_time"
        unit = relative.group("u").casefold()
        if unit in {"分钟", "分", "minute", "minutes", "min", "mins"}:
            delta = timedelta(minutes=amount)
        elif unit in {"小时", "hour", "hours", "hr", "hrs"}:
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        due = (now.astimezone(UTC) + delta).astimezone(now.tzinfo)
        return due, (relative.span(),), None

    date_value, date_span, date_explicit, date_issue = _date_component(text, now)
    time_value, time_span, time_issue = _time_component(text)
    spans = tuple(span for span in (date_span, time_span) if span is not None)
    if date_issue or time_issue:
        return None, spans, date_issue or time_issue
    if time_value is None:
        return None, spans, "missing_time"
    if date_value is None:
        date_value = now.date()
    try:
        candidate, localization_issue = _localized_datetime(
            date_value,
            time_value,
            now,
        )
    except ValueError:
        return None, spans, "invalid_time"
    if localization_issue is not None:
        return None, spans, localization_issue
    if candidate is None:
        return None, spans, "invalid_time"
    if candidate <= now and date_explicit and _is_yearless_month_day(text, date_span):
        for year in range(candidate.year + 1, candidate.year + 6):
            try:
                candidate = candidate.replace(year=year)
            except ValueError:
                continue
            break
        else:
            return None, spans, "invalid_date"
    elif not date_explicit and candidate <= now:
        candidate += timedelta(days=1)
    return candidate, spans, None


def _date_component(
    text: str,
    now: datetime,
) -> tuple[date | None, tuple[int, int] | None, bool, str | None]:
    lowered = text.casefold()
    relative_days = (
        ("day after tomorrow", 2),
        ("tomorrow", 1),
        ("today", 0),
        ("后天", 2),
        ("明天", 1),
        ("今天", 0),
        ("今晚", 0),
    )
    for token, days in relative_days:
        index = lowered.find(token)
        if index >= 0:
            return now.date() + timedelta(days=days), (index, index + len(token)), True, None

    next_week = re.search(r"下周\s*([一二三四五六日天])", text)
    if next_week is not None:
        start = now.date() + timedelta(days=(7 - now.weekday()))
        return start + timedelta(days=_WEEKDAYS[next_week.group(1)]), next_week.span(), True, None
    english_next = re.search(
        r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lowered,
    )
    if english_next is not None:
        start = now.date() + timedelta(days=(7 - now.weekday()))
        return start + timedelta(days=_WEEKDAYS[english_next.group(1)]), english_next.span(), True, None
    weekday = re.search(r"(?:周|星期)\s*([一二三四五六日天])", text)
    if weekday is not None:
        target = _WEEKDAYS[weekday.group(1)]
        days = (target - now.weekday()) % 7
        return now.date() + timedelta(days=days), weekday.span(), days > 0, None

    full = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if full is not None:
        try:
            return datetime(int(full.group(1)), int(full.group(2)), int(full.group(3))).date(), full.span(), True, None
        except ValueError:
            return None, full.span(), True, "invalid_date"
    month_day = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if month_day is None:
        month_day = re.search(r"(?<!\d)(\d{1,2})[-/](\d{1,2})(?![-/\d])", text)
    if month_day is not None:
        month, day = int(month_day.group(1)), int(month_day.group(2))
        for year in (now.year, now.year + 1):
            try:
                value = datetime(year, month, day).date()
            except ValueError:
                return None, month_day.span(), True, "invalid_date"
            if value >= now.date():
                return value, month_day.span(), True, None
    return None, None, False, None


def _time_component(
    text: str,
) -> tuple[time | None, tuple[int, int] | None, str | None]:
    candidates: list[tuple[time, tuple[int, int]]] = []
    for match in re.finditer(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text):
        candidates.append((time(int(match.group(1)), int(match.group(2))), match.span()))
    zh_pattern = re.compile(
        r"(?:(上午|下午|晚上|今晚|中午|凌晨)\s*)?"
        r"(\d{1,2}|[零〇一二两三四五六七八九十]+)\s*[点时]"
        r"\s*(半|\d{1,2}\s*分?)?"
    )
    for match in zh_pattern.finditer(text):
        hour = _number(match.group(2))
        minute_token = (match.group(3) or "").replace("分", "").strip()
        minute = 30 if minute_token == "半" else int(minute_token or 0)
        period = match.group(1) or ""
        if period in {"下午", "晚上", "今晚"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None, match.span(), "invalid_time"
        candidates.append((time(hour, minute), match.span()))
    for match in re.finditer(
        r"\b(?:at\s+)?(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)\b",
        text,
        re.IGNORECASE,
    ):
        hour = int(match.group(1)) % 12
        if match.group(3).casefold() == "pm":
            hour += 12
        candidates.append((time(hour, int(match.group(2) or 0)), match.span()))
    unique = {(candidate.isoformat(), span) for candidate, span in candidates}
    if not unique:
        return None, None, None
    distinct_times = {candidate.isoformat() for candidate, _span in candidates}
    if len(distinct_times) > 1:
        return None, min((span for _value, span in candidates), key=lambda span: span[0]), "ambiguous_time"
    candidate, span = min(candidates, key=lambda item: item[1][0])
    return candidate, span, None


def _content_without_tokens(text: str, spans: tuple[tuple[int, int], ...]) -> str:
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "
    value = " ".join("".join(chars).split()).strip("，。,.!?！？:：;；- ")
    return value


def _is_yearless_month_day(
    text: str,
    span: tuple[int, int] | None,
) -> bool:
    if span is None:
        return False
    token = text[span[0] : span[1]].strip()
    return "月" in token or re.fullmatch(r"\d{1,2}[-/]\d{1,2}", token) is not None


def _localized_datetime(
    date_value: date,
    time_value: time,
    now: datetime,
) -> tuple[datetime | None, str | None]:
    """Reject nonexistent or duplicated DST wall times instead of guessing a fold."""

    zone = now.tzinfo
    if zone is None:
        return None, "invalid_time"
    wall_time = datetime.combine(date_value, time_value)
    candidates = (
        wall_time.replace(tzinfo=zone, fold=0),
        wall_time.replace(tzinfo=zone, fold=1),
    )
    valid = tuple(
        candidate
        for candidate in candidates
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall_time
    )
    if not valid:
        return None, "invalid_time"
    if len({candidate.utcoffset() for candidate in valid}) > 1:
        return None, "ambiguous_time"
    return valid[0], None


def _number(token: str) -> int:
    if token.isdigit():
        return int(token)
    if "百" in token:
        head, _sep, tail = token.partition("百")
        return (_ZH_DIGITS.get(head, 1) * 100) + _number(tail or "零")
    if "十" in token:
        head, _sep, tail = token.partition("十")
        return (_ZH_DIGITS.get(head, 1) * 10) + _ZH_DIGITS.get(tail, 0)
    value = 0
    for character in token:
        value = (value * 10) + _ZH_DIGITS.get(character, 0)
    return value
