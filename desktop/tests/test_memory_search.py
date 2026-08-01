from __future__ import annotations

import pytest

from amadeus_desktop.memory_search import (
    MAX_FTS_QUERY_TOKENS,
    EmptySearchQuery,
    build_fts_match_query,
    build_search_text,
    exact_memory_hash,
    normalize_memory_content,
    normalize_topic_key,
)


def test_search_text_uses_nfkc_lower_latin_and_cjk_unigrams_bigrams() -> None:
    assert build_search_text("ＣＯＦＦＥＥ 咖啡好!") == "coffee 咖 咖啡 啡 啡好 好"
    assert build_search_text("CAFÉ ２杯") == "café 2 杯"


def test_cjk_bigrams_do_not_cross_punctuation_boundaries() -> None:
    assert build_search_text("咖，啡") == "咖 啡"
    assert "咖啡" not in build_search_text("咖，啡").split()


def test_document_and_query_share_the_exact_coffee_bigram() -> None:
    document_tokens = set(build_search_text("用户不喜欢太甜的咖啡").split())
    query = build_fts_match_query("咖啡")

    assert "咖啡" in document_tokens
    assert '"咖啡"' in query.expression
    assert query.parameters == (query.expression,)


def test_match_query_neutralizes_fts_and_sql_syntax() -> None:
    malicious = '咖啡" OR memory_id:*); DROP TABLE memory_fts; --'
    query = build_fts_match_query(malicious)

    assert malicious not in query.expression
    assert '"or"' in query.expression
    assert '"drop"' in query.expression
    assert "*" not in query.expression
    assert ";" not in query.expression
    assert ")" not in query.expression
    assert query.parameters == (query.expression,)


def test_match_query_caps_complexity_and_rejects_empty_text() -> None:
    words = " ".join(f"word{index}" for index in range(100))
    query = build_fts_match_query(words)
    assert len(query.expression.split(" OR ")) == MAX_FTS_QUERY_TOKENS

    with pytest.raises(EmptySearchQuery):
        build_fts_match_query('"*():; --')


def test_topic_content_and_exact_hash_normalization_are_deterministic() -> None:
    assert normalize_topic_key(" 饮料／咖啡 · TASTE ") == "饮料:咖啡:taste"
    assert normalize_memory_content("  ＣＯＦＦＥＥ\n  好喝 ") == "coffee 好喝"
    assert exact_memory_hash("  ＣＯＦＦＥＥ\n好喝 ") == exact_memory_hash("coffee  好喝")
    assert len(exact_memory_hash("coffee")) == 64


def test_exact_hash_rejects_blank_content() -> None:
    with pytest.raises(ValueError):
        exact_memory_hash("\n\t")
