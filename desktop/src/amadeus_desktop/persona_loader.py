"""Strict, content-safe loading for local persona knowledge JSONL files."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from amadeus_desktop.storage_models import PersonaKnowledgeDraft

MAX_PERSONA_FILE_BYTES = 1024 * 1024
MAX_PERSONA_ITEMS = 512
MAX_PERSONA_CONTENT_CHARS = 2_000
MAX_PERSONA_SOURCE_REF_CHARS = 1_024
MAX_PERSONA_TAGS = 16
MAX_PERSONA_TAG_CHARS = 64

_FIELDS = frozenset({"content", "tags", "source_ref", "source_hash"})
_SAFE_PERSONA_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PersonaKnowledgeErrorCode(StrEnum):
    """Stable local error categories that never contain persona text."""

    IO_ERROR = "io_error"
    TOO_LARGE = "too_large"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_VALUE = "invalid_value"
    TOO_MANY_ITEMS = "too_many_items"
    DUPLICATE_ITEM = "duplicate_item"


_ERROR_MESSAGES = {
    PersonaKnowledgeErrorCode.IO_ERROR: "无法读取本地角色知识文件。",
    PersonaKnowledgeErrorCode.TOO_LARGE: "本地角色知识文件超过大小上限。",
    PersonaKnowledgeErrorCode.INVALID_UTF8: "本地角色知识文件不是有效 UTF-8。",
    PersonaKnowledgeErrorCode.INVALID_JSON: "本地角色知识文件包含无效 JSON。",
    PersonaKnowledgeErrorCode.INVALID_SCHEMA: "本地角色知识条目字段无效。",
    PersonaKnowledgeErrorCode.INVALID_VALUE: "本地角色知识条目值无效。",
    PersonaKnowledgeErrorCode.TOO_MANY_ITEMS: "本地角色知识条目数量超过上限。",
    PersonaKnowledgeErrorCode.DUPLICATE_ITEM: "本地角色知识包含重复条目。",
}


class PersonaKnowledgeLoadError(ValueError):
    """Fail-closed loader error with no source path or fragment body."""

    def __init__(
        self,
        code: PersonaKnowledgeErrorCode,
        *,
        line_number: int | None = None,
    ) -> None:
        self.code = code
        self.line_number = line_number
        message = _ERROR_MESSAGES[code]
        if line_number is not None:
            message = f"{message}（第 {line_number} 行）"
        super().__init__(message)


def load_persona_knowledge_jsonl(
    path: Path,
    *,
    persona_id: str,
) -> tuple[PersonaKnowledgeDraft, ...]:
    """Load one local JSONL file without exposing its path or bodies in errors."""

    _validate_persona_id(persona_id)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.IO_ERROR) from exc
    if len(payload) > MAX_PERSONA_FILE_BYTES:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.TOO_LARGE)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.INVALID_UTF8) from exc
    return parse_persona_knowledge_jsonl(text, persona_id=persona_id)


def parse_persona_knowledge_jsonl(
    text: str,
    *,
    persona_id: str,
) -> tuple[PersonaKnowledgeDraft, ...]:
    """Parse the exact local persona schema with deterministic fragment IDs."""

    _validate_persona_id(persona_id)
    if not isinstance(text, str):
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.INVALID_UTF8)
    if len(text.encode("utf-8")) > MAX_PERSONA_FILE_BYTES:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.TOO_LARGE)

    lines = text.splitlines()
    if len(lines) > MAX_PERSONA_ITEMS:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.TOO_MANY_ITEMS)
    if not lines:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.INVALID_SCHEMA)

    entries: list[PersonaKnowledgeDraft] = []
    content_hashes: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise PersonaKnowledgeLoadError(
                PersonaKnowledgeErrorCode.INVALID_SCHEMA,
                line_number=line_number,
            )
        document = _parse_line(line, line_number)
        content = _required_string(
            document["content"],
            maximum=MAX_PERSONA_CONTENT_CHARS,
            line_number=line_number,
        )
        source_ref = _required_string(
            document["source_ref"],
            maximum=MAX_PERSONA_SOURCE_REF_CHARS,
            line_number=line_number,
        )
        source_hash = _required_sha256(document["source_hash"], line_number)
        tags = _required_tags(document["tags"], line_number)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_hash in content_hashes:
            raise PersonaKnowledgeLoadError(
                PersonaKnowledgeErrorCode.DUPLICATE_ITEM,
                line_number=line_number,
            )
        content_hashes.add(content_hash)
        identity = hashlib.sha256(
            f"{persona_id}\0{source_hash}\0{content_hash}".encode()
        ).hexdigest()
        entries.append(
            PersonaKnowledgeDraft(
                content=content,
                tags=tags,
                source_ref=source_ref,
                source_hash=source_hash,
                knowledge_id=identity,
            )
        )
    return tuple(entries)


def _parse_line(line: str, line_number: int) -> Mapping[str, Any]:
    try:
        document = json.loads(
            line,
            object_pairs_hook=_object_without_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
    except (_DuplicateField, _InvalidJsonConstant, json.JSONDecodeError, RecursionError) as exc:
        raise PersonaKnowledgeLoadError(
            PersonaKnowledgeErrorCode.INVALID_JSON,
            line_number=line_number,
        ) from exc
    if not isinstance(document, dict) or frozenset(document) != _FIELDS:
        raise PersonaKnowledgeLoadError(
            PersonaKnowledgeErrorCode.INVALID_SCHEMA,
            line_number=line_number,
        )
    return document


def _required_string(value: Any, *, maximum: int, line_number: int) -> str:
    if not isinstance(value, str):
        raise PersonaKnowledgeLoadError(
            PersonaKnowledgeErrorCode.INVALID_VALUE,
            line_number=line_number,
        )
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise PersonaKnowledgeLoadError(
            PersonaKnowledgeErrorCode.INVALID_VALUE,
            line_number=line_number,
        )
    return normalized


def _required_sha256(value: Any, line_number: int) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PersonaKnowledgeLoadError(
            PersonaKnowledgeErrorCode.INVALID_VALUE,
            line_number=line_number,
        )
    return value


def _required_tags(value: Any, line_number: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PERSONA_TAGS:
        raise PersonaKnowledgeLoadError(
            PersonaKnowledgeErrorCode.INVALID_VALUE,
            line_number=line_number,
        )
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in value:
        tag = _required_string(
            raw_tag,
            maximum=MAX_PERSONA_TAG_CHARS,
            line_number=line_number,
        )
        if tag in seen:
            raise PersonaKnowledgeLoadError(
                PersonaKnowledgeErrorCode.INVALID_VALUE,
                line_number=line_number,
            )
        seen.add(tag)
        tags.append(tag)
    return tuple(tags)


def _validate_persona_id(persona_id: str) -> None:
    if not isinstance(persona_id, str) or _SAFE_PERSONA_ID.fullmatch(persona_id) is None:
        raise PersonaKnowledgeLoadError(PersonaKnowledgeErrorCode.INVALID_VALUE)


class _DuplicateField(ValueError):
    pass


class _InvalidJsonConstant(ValueError):
    pass


def _object_without_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateField
        document[key] = value
    return document


def _reject_json_constant(value: str) -> None:
    raise _InvalidJsonConstant(value)
