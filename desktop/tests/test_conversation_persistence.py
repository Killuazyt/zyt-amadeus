from __future__ import annotations

from collections.abc import Callable

from amadeus_desktop.chat_models import (
    ConversationState,
    ConversationTurn,
    MessageStatus,
    PreparedPrompt,
    PromptMessage,
    PromptRole,
    TurnTerminalReason,
)
from amadeus_desktop.chat_provider import ScriptedChatProvider, ScriptedScenario
from amadeus_desktop.conversation import ConversationCoordinator


class DeferredPersistence:
    def __init__(self) -> None:
        self.new_turns: list[ConversationTurn] = []
        self.retried_turns: list[ConversationTurn] = []
        self.checkpoints: list[ConversationTurn] = []
        self.finalized: list[ConversationTurn] = []
        self.recalled: list[tuple[ConversationTurn, PreparedPrompt]] = []
        self.success: Callable[[tuple[PromptMessage, ...]], None] | None = None
        self.failure: Callable[[str], None] | None = None

    def prepare_new_turn(self, turn, on_success, on_failure) -> bool:
        self.new_turns.append(turn)
        self.success = on_success
        self.failure = on_failure
        return True

    def prepare_retry(self, turn, on_success, on_failure) -> bool:
        self.retried_turns.append(turn)
        self.success = on_success
        self.failure = on_failure
        return True

    def checkpoint_assistant(self, turn: ConversationTurn) -> None:
        self.checkpoints.append(turn)

    def finalize_turn(self, turn: ConversationTurn, on_success, _on_failure) -> bool:
        self.finalized.append(turn)
        on_success()
        return True

    def record_successful_recall(
        self,
        turn: ConversationTurn,
        prepared: PreparedPrompt,
    ) -> None:
        self.recalled.append((turn, prepared))


def _wait_idle(qtbot, coordinator: ConversationCoordinator) -> None:
    qtbot.waitUntil(
        lambda: coordinator.state is ConversationState.IDLE and not coordinator.has_running_worker,
        timeout=2_000,
    )


def test_provider_does_not_start_until_durable_prepare_succeeds(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(chunks=("已保存后回复",), first_delay_ms=0)
    coordinator = ConversationCoordinator(provider, persistence=persistence)
    try:
        turn = coordinator.send_message("必须先落库")

        assert turn is not None
        assert coordinator.state is ConversationState.SENDING
        assert provider.call_count == 0
        assert persistence.new_turns == [turn]

        assert persistence.success is not None
        persistence.success(
            (
                PromptMessage(PromptRole.SYSTEM, "安全边界"),
                PromptMessage(PromptRole.USER, "必须先落库"),
            )
        )
        _wait_idle(qtbot, coordinator)

        assert provider.call_count == 1
        assert provider.requests[0].messages[-1].content == "必须先落库"
        assert persistence.finalized[-1].assistant_message.status is MessageStatus.COMPLETED
        assert persistence.checkpoints[-1].assistant_message.content == "已保存后回复"
    finally:
        assert coordinator.shutdown(1_000)


def test_prepare_failure_blocks_network_and_leaves_retryable_turn(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(chunks=("不应调用",), first_delay_ms=0)
    coordinator = ConversationCoordinator(provider, persistence=persistence)
    try:
        turn = coordinator.send_message("数据库失败")
        assert turn is not None and persistence.failure is not None

        persistence.failure("DatabaseReadOnlyError")
        _wait_idle(qtbot, coordinator)

        failed = coordinator.turns[0]
        assert provider.call_count == 0
        assert failed.assistant_message.status is MessageStatus.FAILED
        assert failed.terminal_reason is TurnTerminalReason.LOCAL_PERSISTENCE_ERROR
        assert "本地数据" in (failed.error or "")
    finally:
        assert coordinator.shutdown(1_000)


def test_stop_during_prepare_ignores_late_success_and_persists_terminal(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(chunks=("不应调用",), first_delay_ms=0)
    coordinator = ConversationCoordinator(provider, persistence=persistence)
    try:
        coordinator.send_message("提交中停止")
        success = persistence.success
        assert success is not None

        assert coordinator.stop()
        _wait_idle(qtbot, coordinator)
        success((PromptMessage(PromptRole.USER, "提交中停止"),))
        qtbot.wait(20)

        assert provider.call_count == 0
        assert persistence.finalized[-1].terminal_reason is TurnTerminalReason.USER_STOPPED
    finally:
        assert coordinator.shutdown(1_000)


def test_stream_checkpoints_are_coalesced_at_500ms_and_terminal_flushes(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(
        chunks=("一", "二", "三"),
        first_delay_ms=0,
        chunk_delay_ms=300,
    )
    coordinator = ConversationCoordinator(
        provider,
        persistence=persistence,
        first_chunk_timeout_ms=1_000,
        stream_idle_timeout_ms=1_000,
    )
    try:
        turn = coordinator.send_message("检查点节奏")
        assert turn is not None and persistence.success is not None
        persistence.success((PromptMessage(PromptRole.USER, "检查点节奏"),))
        _wait_idle(qtbot, coordinator)

        assert len(persistence.checkpoints) == 2
        assert persistence.checkpoints[0].assistant_message.content == "一二"
        assert persistence.checkpoints[-1].assistant_message.content == "一二三"
        assert persistence.finalized[-1].assistant_message.content == "一二三"
    finally:
        assert coordinator.shutdown(1_000)


def test_recall_is_recorded_only_after_first_chunk_and_successful_terminal(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(chunks=("已使用记忆",), first_delay_ms=0)
    coordinator = ConversationCoordinator(provider, persistence=persistence)
    try:
        coordinator.send_message("记得吗")
        assert persistence.success is not None
        prepared = PreparedPrompt(
            messages=(PromptMessage(PromptRole.USER, "记得吗"),),
            user_memory_version_ids=("version-1",),
            persona_knowledge_ids=("persona-1",),
        )
        persistence.success(prepared)
        _wait_idle(qtbot, coordinator)

        assert len(persistence.recalled) == 1
        recalled_turn, recalled_prompt = persistence.recalled[0]
        assert recalled_turn.terminal_reason is TurnTerminalReason.COMPLETED
        assert recalled_prompt == prepared
    finally:
        assert coordinator.shutdown(1_000)


def test_recall_is_not_recorded_when_user_stops_before_first_chunk(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(chunks=("不应到达",), first_delay_ms=1_000)
    coordinator = ConversationCoordinator(
        provider,
        persistence=persistence,
        first_chunk_timeout_ms=2_000,
    )
    try:
        coordinator.send_message("先停下")
        assert persistence.success is not None
        persistence.success(
            PreparedPrompt(
                messages=(PromptMessage(PromptRole.USER, "先停下"),),
                user_memory_version_ids=("version-1",),
            )
        )
        qtbot.waitUntil(
            lambda: coordinator.state is ConversationState.WAITING_FIRST_CHUNK,
            timeout=1_000,
        )
        assert coordinator.stop()
        _wait_idle(qtbot, coordinator)

        assert persistence.recalled == []
    finally:
        assert coordinator.shutdown(1_000)


def test_recall_is_recorded_when_user_stops_after_first_chunk(qtbot) -> None:
    persistence = DeferredPersistence()
    provider = ScriptedChatProvider(
        ScriptedScenario.STALL,
        chunks=("已收到",),
        first_delay_ms=0,
        chunk_delay_ms=0,
    )
    coordinator = ConversationCoordinator(provider, persistence=persistence)
    try:
        coordinator.send_message("收到后停止")
        assert persistence.success is not None
        persistence.success(
            PreparedPrompt(
                messages=(PromptMessage(PromptRole.USER, "收到后停止"),),
                user_memory_version_ids=("version-1",),
            )
        )
        qtbot.waitUntil(
            lambda: coordinator.state is ConversationState.STREAMING,
            timeout=1_000,
        )
        assert coordinator.stop()
        _wait_idle(qtbot, coordinator)

        assert len(persistence.recalled) == 1
        assert persistence.recalled[0][0].terminal_reason is TurnTerminalReason.USER_STOPPED
    finally:
        assert coordinator.shutdown(1_000)
