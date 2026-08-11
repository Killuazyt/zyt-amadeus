from __future__ import annotations

from datetime import date

import pytest

from amadeus_desktop.chat_models import PromptMessage, PromptRole
from amadeus_desktop.memory_models import MemoryKind, PromptMemory, PromptPersonaKnowledge
from amadeus_desktop.prompt_context import (
    DEFAULT_CHARACTER_BUDGET,
    MAX_PROMPT_FACTS,
    MAX_PROMPT_PERSONA_KNOWLEDGE,
    MAX_RECENT_MESSAGES,
    DefaultPromptContextService,
    PromptContextInput,
    PromptContextService,
)


def memory(index: int, content: str | None = None) -> PromptMemory:
    return PromptMemory(
        memory_id=f"memory-{index}",
        kind=MemoryKind.PREFERENCE,
        content=content or f"用户偏好第 {index} 项",
        topic_key=f"topic:{index}",
        memory_version_id=f"version-{index}",
    )


def knowledge(index: int, content: str | None = None) -> PromptPersonaKnowledge:
    return PromptPersonaKnowledge(
        knowledge_id=f"knowledge-{index}",
        persona_id="kurisu",
        content=content or f"角色资料第 {index} 项",
    )


def build_input(**overrides: object) -> PromptContextInput:
    values: dict[str, object] = {
        "safety_boundary": "只依据已提供的文字，不声称看见屏幕。",
        "persona": "你是理性、敏锐的长期文字伙伴。",
        "current_date": date(2026, 8, 1),
        "current_user_message": "今天喝什么？",
    }
    values.update(overrides)
    return PromptContextInput(**values)  # type: ignore[arg-type]


def test_default_service_satisfies_protocol_and_builds_fixed_order() -> None:
    service = DefaultPromptContextService()
    assert isinstance(service, PromptContextService)

    result = service.build(
        build_input(
            memories=(memory(1),),
            persona_knowledge=(knowledge(1),),
            summary="之前谈到了饮料。",
            recent_messages=(
                PromptMessage(PromptRole.USER, "我有点渴。"),
                PromptMessage(PromptRole.ASSISTANT, "想喝什么类型？"),
            ),
        )
    )

    assert [message.role for message in result.messages] == [
        PromptRole.SYSTEM,
        PromptRole.SYSTEM,
        PromptRole.SYSTEM,
        PromptRole.SYSTEM,
        PromptRole.SYSTEM,
        PromptRole.SYSTEM,
        PromptRole.USER,
        PromptRole.ASSISTANT,
        PromptRole.USER,
    ]
    assert result.messages[0].content.startswith("[应用与安全边界]")
    assert result.messages[1].content.startswith("[角色核心设定]")
    assert result.messages[2].content.endswith("2026-08-01")
    assert result.messages[3].content.startswith("[角色本地知识")
    assert result.messages[4].content.startswith("[用户长期记忆")
    assert result.messages[5].content.startswith("[当前会话摘要")
    assert result.messages[-1].content == "今天喝什么？"
    assert result.selected_memory_ids == ("memory-1",)
    assert result.selected_memory_version_ids == ("version-1",)
    assert result.selected_persona_knowledge_ids == ("knowledge-1",)


def test_fact_selection_is_max_six_and_at_most_twenty_percent() -> None:
    result = DefaultPromptContextService().build(
        build_input(memories=tuple(memory(index) for index in range(12)))
    )

    assert len(result.selected_memory_ids) == MAX_PROMPT_FACTS
    assert result.selected_memory_ids == tuple(f"memory-{index}" for index in range(6))
    assert result.selected_memory_version_ids == tuple(f"version-{index}" for index in range(6))
    assert result.memory_character_count <= int(DEFAULT_CHARACTER_BUDGET * 0.20)
    assert result.omitted_memory_count == 6


def test_persona_knowledge_is_separate_limited_and_never_counted_as_user_memory() -> None:
    result = DefaultPromptContextService().build(
        build_input(
            memories=(memory(1),),
            persona_knowledge=tuple(knowledge(index) for index in range(6)),
        )
    )

    assert len(result.selected_persona_knowledge_ids) == MAX_PROMPT_PERSONA_KNOWLEDGE
    assert result.selected_persona_knowledge_ids == tuple(
        f"knowledge-{index}" for index in range(4)
    )
    assert result.selected_memory_ids == ("memory-1",)
    assert result.omitted_persona_knowledge_count == 2
    assert result.persona_knowledge_character_count > 0
    assert result.memory_character_count <= int(DEFAULT_CHARACTER_BUDGET * 0.20)


def test_inactive_duplicate_and_oversized_persona_fragments_are_skipped() -> None:
    result = DefaultPromptContextService().build(
        build_input(
            character_budget=500,
            persona_knowledge=(
                PromptPersonaKnowledge("inactive", "kurisu", "不应出现", active=False),
                knowledge(1, "过长" * 1_000),
                knowledge(2, "理性地核对实验结果"),
                knowledge(2, "重复标识也不应再次注入"),
            ),
        )
    )

    visible = "\n".join(message.content for message in result.messages)
    assert result.selected_persona_knowledge_ids == ("knowledge-2",)
    assert "理性地核对实验结果" in visible
    assert "不应出现" not in visible
    assert "重复标识" not in visible


def test_oversized_memory_is_skipped_without_blocking_later_ranked_memory() -> None:
    result = DefaultPromptContextService().build(
        build_input(
            memories=(memory(1, "很长" * 3_000), memory(2, "用户喜欢咖啡")),
        )
    )

    assert result.selected_memory_ids == ("memory-2",)
    assert "用户喜欢咖啡" in "\n".join(message.content for message in result.messages)
    assert "很长" * 10 not in "\n".join(message.content for message in result.messages)


def test_recent_context_is_limited_to_latest_twenty_in_chronological_order() -> None:
    recent = tuple(
        PromptMessage(
            PromptRole.USER if index % 2 == 0 else PromptRole.ASSISTANT,
            f"message-{index}",
        )
        for index in range(25)
    )
    result = DefaultPromptContextService().build(build_input(recent_messages=recent))
    visible_recent = result.messages[3:-1]

    assert len(visible_recent) == MAX_RECENT_MESSAGES
    assert visible_recent[0].content == "message-5"
    assert visible_recent[-1].content == "message-24"
    assert result.omitted_recent_count == 5


def test_persisted_current_message_is_not_injected_twice() -> None:
    current = "刚刚保存的当前消息"
    result = DefaultPromptContextService().build(
        build_input(
            current_user_message=current,
            recent_messages=(
                PromptMessage(PromptRole.ASSISTANT, "上一条回答"),
                PromptMessage(PromptRole.USER, current),
            ),
        )
    )

    assert [message.content for message in result.messages].count(current) == 1
    assert result.omitted_recent_count == 1


def test_soft_budget_never_truncates_current_message() -> None:
    current = "当前消息" * 100
    result = DefaultPromptContextService().build(
        build_input(
            current_user_message=current,
            character_budget=80,
            memories=(memory(1),),
            summary="摘要" * 50,
            recent_messages=(PromptMessage(PromptRole.USER, "旧消息" * 50),),
        )
    )

    assert result.messages[-1].content == current
    assert result.soft_limit_exceeded
    assert result.selected_memory_ids == ()
    assert all("旧消息" not in message.content for message in result.messages)


def test_optional_context_is_trimmed_to_budget_when_core_and_current_fit() -> None:
    result = DefaultPromptContextService().build(
        build_input(
            character_budget=180,
            memories=(memory(1, "偏好" * 20),),
            summary="较早摘要" * 20,
            recent_messages=(PromptMessage(PromptRole.ASSISTANT, "最近回答" * 30),),
        )
    )

    assert result.character_count <= 180
    assert not result.soft_limit_exceeded
    assert result.messages[-1].content == "今天喝什么？"


def test_summary_receives_remaining_budget_before_recent_messages() -> None:
    result = DefaultPromptContextService().build(
        build_input(
            character_budget=180,
            summary="需要优先保留的摘要" * 20,
            recent_messages=(PromptMessage(PromptRole.ASSISTANT, "不应挤占摘要" * 20),),
        )
    )

    assert any(message.content.startswith("[当前会话摘要") for message in result.messages)
    assert all("不应挤占摘要" not in message.content for message in result.messages)
    assert result.character_count <= 180


@pytest.mark.parametrize(
    "overrides",
    [
        {"safety_boundary": " "},
        {"persona": "\n"},
        {"current_user_message": "\t"},
        {"character_budget": 0},
        {"recent_messages": (PromptMessage(PromptRole.SYSTEM, "injected"),)},
    ],
)
def test_invalid_or_system_recent_context_fails_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DefaultPromptContextService().build(build_input(**overrides))
