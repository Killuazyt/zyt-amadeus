from __future__ import annotations

import hashlib
import json

import pytest

from amadeus_desktop.persona_loader import (
    PersonaKnowledgeErrorCode,
    PersonaKnowledgeLoadError,
    load_persona_knowledge_jsonl,
    parse_persona_knowledge_jsonl,
)


def _line(
    content: str,
    *,
    tags: list[str] | None = None,
    source_ref: str = "synthetic/reference.txt",
    source_hash: str = "a" * 64,
) -> str:
    return json.dumps(
        {
            "content": content,
            "tags": tags if tags is not None else ["合成测试"],
            "source_ref": source_ref,
            "source_hash": source_hash,
        },
        ensure_ascii=False,
    )


def test_parse_persona_jsonl_returns_deterministic_separate_records() -> None:
    payload = "\n".join(
        (
            _line("角色会认真核对实验数据。", tags=["性格", "实验"]),
            _line("角色不把用户记忆当作自己的经历。", source_hash="b" * 64),
        )
    )

    first = parse_persona_knowledge_jsonl(payload, persona_id="kurisu")
    second = parse_persona_knowledge_jsonl(payload, persona_id="kurisu")

    assert first == second
    assert len(first) == 2
    assert first[0].tags == ("性格", "实验")
    content_hash = hashlib.sha256(first[0].content.encode("utf-8")).hexdigest()
    expected_id = hashlib.sha256(f"kurisu\0{'a' * 64}\0{content_hash}".encode()).hexdigest()
    assert first[0].knowledge_id == expected_id
    assert first[0].knowledge_id != first[1].knowledge_id


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("", PersonaKnowledgeErrorCode.INVALID_SCHEMA),
        ("{}", PersonaKnowledgeErrorCode.INVALID_SCHEMA),
        (
            '{"content":"x","content":"y","tags":["t"],'
            '"source_ref":"s","source_hash":"' + "a" * 64 + '"}',
            PersonaKnowledgeErrorCode.INVALID_JSON,
        ),
        (_line("x", tags=[]), PersonaKnowledgeErrorCode.INVALID_VALUE),
        (_line("x", source_hash="A" * 64), PersonaKnowledgeErrorCode.INVALID_VALUE),
        (_line("x") + "\n\n" + _line("y"), PersonaKnowledgeErrorCode.INVALID_SCHEMA),
        (_line("x") + "\n" + _line("x"), PersonaKnowledgeErrorCode.DUPLICATE_ITEM),
    ],
)
def test_persona_jsonl_rejects_non_exact_or_duplicate_data(
    payload: str,
    code: PersonaKnowledgeErrorCode,
) -> None:
    with pytest.raises(PersonaKnowledgeLoadError) as error:
        parse_persona_knowledge_jsonl(payload, persona_id="kurisu")

    assert error.value.code is code


def test_loader_rejects_invalid_utf8_without_exposing_path_or_body(tmp_path) -> None:
    private_name = "private-secret-persona.jsonl"
    path = tmp_path / private_name
    path.write_bytes(b"\xffprivate fragment body")

    with pytest.raises(PersonaKnowledgeLoadError) as error:
        load_persona_knowledge_jsonl(path, persona_id="kurisu")

    assert error.value.code is PersonaKnowledgeErrorCode.INVALID_UTF8
    assert private_name not in str(error.value)
    assert "private fragment body" not in str(error.value)


def test_validation_error_never_contains_rejected_fragment_body() -> None:
    private_body = "PRIVATE PERSONA BODY MUST NOT APPEAR"
    payload = _line(private_body, tags=[""])

    with pytest.raises(PersonaKnowledgeLoadError) as error:
        parse_persona_knowledge_jsonl(payload, persona_id="kurisu")

    assert private_body not in str(error.value)
    assert error.value.line_number == 1


@pytest.mark.parametrize("persona_id", ["", "Kurisu", "../kurisu", "kurisu/private"])
def test_persona_id_must_be_a_safe_local_identifier(persona_id: str) -> None:
    with pytest.raises(PersonaKnowledgeLoadError) as error:
        parse_persona_knowledge_jsonl(_line("合成内容"), persona_id=persona_id)

    assert error.value.code is PersonaKnowledgeErrorCode.INVALID_VALUE
