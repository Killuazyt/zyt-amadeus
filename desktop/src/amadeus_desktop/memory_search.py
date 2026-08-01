"""Deterministic normalization and injection-safe FTS5 query construction."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

MAX_FTS_QUERY_TOKENS = 64
_WHITESPACE = re.compile(r"\s+")


class EmptySearchQuery(ValueError):
    """Raised when a query contains no searchable Latin, numeric, or CJK text."""


@dataclass(frozen=True, slots=True)
class FtsMatchQuery:
    """A MATCH expression and its positional DB-API parameters.

    Callers must keep SQL static, for example ``... WHERE memory_fts MATCH ?``,
    and pass :attr:`parameters` separately.  No user text belongs in SQL.
    """

    search_text: str
    expression: str
    parameters: tuple[str]


def normalize_memory_content(text: str) -> str:
    """Normalize content for exact duplicate hashing without changing storage text."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    return _WHITESPACE.sub(" ", normalized).strip()


def normalize_topic_key(text: str) -> str:
    """Create a case- and punctuation-insensitive deterministic topic key."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    pieces: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum() or _is_cjk(character):
            current.append(character)
        elif current:
            pieces.append("".join(current))
            current.clear()
    if current:
        pieces.append("".join(current))
    return ":".join(pieces)


def exact_memory_hash(content: str) -> str:
    """Return the SHA-256 identity of normalized memory content."""

    normalized = normalize_memory_content(content)
    if not normalized:
        raise ValueError("memory content must contain searchable text")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_search_text(text: str) -> str:
    """Build deterministic FTS text from Latin words and CJK uni/bi-grams.

    NFKC and lowercase normalization is shared by indexed documents and queries.
    Consecutive CJK runs yield each character plus every overlapping two-character
    token, so a query such as ``咖啡`` can match a dedicated bigram token.
    """

    return " ".join(_tokenize(text))


def build_fts_match_query(text: str) -> FtsMatchQuery:
    """Compile user text into a quoted, positional-parameter MATCH query."""

    search_text = build_search_text(text)
    tokens = _unique(search_text.split())[:MAX_FTS_QUERY_TOKENS]
    if not tokens:
        raise EmptySearchQuery("search query has no searchable tokens")

    # Tokens are produced by the whitelist tokenizer. Quoting also neutralizes
    # FTS operators such as OR, NOT, NEAR, column names, prefixes, and parens.
    expression = " OR ".join(f'"{token}"' for token in tokens)
    return FtsMatchQuery(
        search_text=search_text,
        expression=expression,
        parameters=(expression,),
    )


def _tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    latin_or_number: list[str] = []
    cjk: list[str] = []

    def flush_latin_or_number() -> None:
        if latin_or_number:
            tokens.append("".join(latin_or_number))
            latin_or_number.clear()

    def flush_cjk() -> None:
        if not cjk:
            return
        for index, character in enumerate(cjk):
            tokens.append(character)
            if index + 1 < len(cjk):
                tokens.append(character + cjk[index + 1])
        cjk.clear()

    for character in normalized:
        if _is_cjk(character):
            flush_latin_or_number()
            cjk.append(character)
            continue
        if _is_latin(character) or character.isdecimal():
            flush_cjk()
            latin_or_number.append(character)
            continue
        if unicodedata.category(character).startswith("M") and latin_or_number:
            latin_or_number.append(character)
            continue
        flush_latin_or_number()
        flush_cjk()

    flush_latin_or_number()
    flush_cjk()
    return tokens


def _is_latin(character: str) -> bool:
    if not character.isalpha():
        return False
    return "LATIN" in unicodedata.name(character, "")


def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x2FA1F
    )


def _unique(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(tokens))
