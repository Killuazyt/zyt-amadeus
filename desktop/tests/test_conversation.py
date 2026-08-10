from __future__ import annotations

import threading
from collections.abc import AsyncIterator

import pytest

from amadeus_desktop.chat_models import (
    ChatRequest,
    ConversationState,
    MessageStatus,
    PromptRole,
    TurnTerminalReason,
)
from amadeus_desktop.chat_provider import (
    CancellationToken,
    ChatProviderError,
    ProviderErrorCode,
    ScriptedChatProvider,
    ScriptedScenario,
)
from amadeus_desktop.conversation import MAX_VISIBLE_RESPONSE_CHARS, ConversationCoordinator


@pytest.fixture
def coordinator_factory(qapp):
    coordinators: list[ConversationCoordinator] = []

    def create(
        provider,
        *,
        first_chunk_timeout_ms: int = 300,
        stream_idle_timeout_ms: int = 300,
    ) -> ConversationCoordinator:
        coordinator = ConversationCoordinator(
            provider,
            first_chunk_timeout_ms=first_chunk_timeout_ms,
            stream_idle_timeout_ms=stream_idle_timeout_ms,
        )
        coordinators.append(coordinator)
        return coordinator

    yield create

    for coordinator in coordinators:
        assert coordinator.shutdown(wait_ms=1_000)


def wait_until_idle(qtbot, coordinator: ConversationCoordinator, *, timeout: int = 2_000) -> None:
    qtbot.waitUntil(
        lambda: coordinator.state is ConversationState.IDLE and not coordinator.has_running_worker,
        timeout=timeout,
    )
    assert coordinator.active_request_id is None
    assert not coordinator.has_active_timers


def test_normal_stream_runs_off_ui_and_visits_all_states(
    qtbot,
    coordinator_factory,
) -> None:
    class RecordingProvider:
        def __init__(self) -> None:
            self.worker_thread_id: int | None = None

        async def stream(
            self,
            request: ChatRequest,
            cancellation: CancellationToken,
        ) -> AsyncIterator[str]:
            del request
            cancellation.raise_if_cancelled()
            self.worker_thread_id = threading.get_ident()
            yield "第一段"
            yield "第二段"

    provider = RecordingProvider()
    coordinator = coordinator_factory(provider)
    states: list[ConversationState] = []
    updates = []
    coordinator.state_changed.connect(states.append)
    coordinator.turn_updated.connect(updates.append)

    turn = coordinator.send_message("你好")

    assert turn is not None
    assert coordinator.state is ConversationState.WAITING_FIRST_CHUNK
    wait_until_idle(qtbot, coordinator)

    assert states == [
        ConversationState.SENDING,
        ConversationState.WAITING_FIRST_CHUNK,
        ConversationState.STREAMING,
        ConversationState.COMPLETED,
        ConversationState.IDLE,
    ]
    assert provider.worker_thread_id is not None
    assert provider.worker_thread_id != threading.get_ident()
    completed = coordinator.turns[0]
    assert completed.turn_id == turn.turn_id
    assert completed.assistant_message.message_id == turn.assistant_message.message_id
    assert completed.assistant_message.content == "第一段第二段"
    assert completed.assistant_message.status is MessageStatus.COMPLETED
    assert completed.terminal_reason is TurnTerminalReason.COMPLETED
    assert len(updates) == 3


def test_stop_before_first_chunk_preserves_empty_assistant_placeholder(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=500)
    terminal = []
    coordinator.request_finished.connect(lambda *args: terminal.append(args))

    turn = coordinator.send_message("停止测试")
    assert turn is not None
    assert coordinator.stop()
    assert coordinator.state is ConversationState.STOPPED
    assert not coordinator.stop()
    wait_until_idle(qtbot, coordinator)

    stopped = coordinator.turns[0]
    assert stopped.user_message.content == "停止测试"
    assert stopped.assistant_message.content == ""
    assert stopped.assistant_message.status is MessageStatus.STOPPED
    assert stopped.terminal_reason is TurnTerminalReason.USER_STOPPED
    assert stopped.status_text == "用户已停止"
    assert len(terminal) == 1


def test_stop_after_partial_stream_preserves_received_text(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.STALL,
        chunks=("已收到的部分",),
        first_delay_ms=1,
    )
    coordinator = coordinator_factory(provider, stream_idle_timeout_ms=500)
    coordinator.send_message("请开始")
    qtbot.waitUntil(
        lambda: coordinator.state is ConversationState.STREAMING,
        timeout=1_000,
    )

    assert coordinator.stop()
    wait_until_idle(qtbot, coordinator)

    stopped = coordinator.turns[0]
    assert stopped.assistant_message.content == "已收到的部分"
    assert stopped.assistant_message.status is MessageStatus.STOPPED


def test_slow_first_chunk_can_complete_before_timeout(qtbot, coordinator_factory) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.SLOW_FIRST,
        chunks=("慢，但成功",),
        slow_first_delay_ms=40,
    )
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=200)

    coordinator.send_message("慢回复")
    wait_until_idle(qtbot, coordinator)

    assert coordinator.turns[0].assistant_message.content == "慢，但成功"
    assert coordinator.turns[0].terminal_reason is TurnTerminalReason.COMPLETED


def test_per_attempt_first_chunk_timeout_override_does_not_change_default(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.SLOW_FIRST,
        chunks=("放宽后成功",),
        slow_first_delay_ms=60,
    )
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=25)

    coordinator.send_message("凝神请求", first_chunk_timeout_ms=500)
    wait_until_idle(qtbot, coordinator)
    assert coordinator.turns[0].terminal_reason is TurnTerminalReason.COMPLETED

    coordinator.send_message("普通请求")
    wait_until_idle(qtbot, coordinator)
    assert coordinator.turns[1].terminal_reason is TurnTerminalReason.FIRST_CHUNK_TIMEOUT


def test_invalid_per_attempt_first_chunk_timeout_is_rejected_without_a_turn(
    coordinator_factory,
) -> None:
    coordinator = coordinator_factory(ScriptedChatProvider())

    with pytest.raises(ValueError, match="positive integer"):
        coordinator.send_message("不会发送", first_chunk_timeout_ms=0)

    assert coordinator.turns == ()


def test_first_chunk_timeout_cancels_never_returning_provider(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=25)
    failures = []
    coordinator.error_occurred.connect(lambda *args: failures.append(args))

    coordinator.send_message("永不返回")
    wait_until_idle(qtbot, coordinator)

    failed = coordinator.turns[0]
    assert failed.assistant_message.content == ""
    assert failed.assistant_message.status is MessageStatus.FAILED
    assert failed.terminal_reason is TurnTerminalReason.FIRST_CHUNK_TIMEOUT
    assert failed.provider_error_code == ProviderErrorCode.TIMEOUT.value
    assert failed.error == "等待回复首段超时，请重试。"
    assert failures[0][1] == failed.turn_id


def test_partial_provider_error_preserves_text_and_allows_failure_ui(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.PARTIAL_ERROR,
        chunks=("中断前片段",),
        first_delay_ms=1,
        chunk_delay_ms=1,
    )
    coordinator = coordinator_factory(provider)

    coordinator.send_message("制造中断")
    wait_until_idle(qtbot, coordinator)

    failed = coordinator.turns[0]
    assert failed.assistant_message.content == "中断前片段"
    assert failed.assistant_message.status is MessageStatus.FAILED
    assert failed.terminal_reason is TurnTerminalReason.PROVIDER_ERROR
    assert failed.provider_error_code == ProviderErrorCode.PROTOCOL.value
    assert failed.error == "模型服务返回了无法识别的数据。"


def test_provider_error_code_is_preserved_for_privacy_safe_evidence(
    qtbot,
    coordinator_factory,
) -> None:
    class AuthenticationFailureProvider:
        async def stream(
            self,
            request: ChatRequest,
            cancellation: CancellationToken,
        ) -> AsyncIterator[str]:
            del request
            cancellation.raise_if_cancelled()
            raise ChatProviderError(ProviderErrorCode.AUTHENTICATION)
            yield  # pragma: no cover - keep this an async generator

    coordinator = coordinator_factory(AuthenticationFailureProvider())
    coordinator.send_message("鉴权分类")
    wait_until_idle(qtbot, coordinator)

    failed = coordinator.turns[0]
    assert failed.terminal_reason is TurnTerminalReason.PROVIDER_ERROR
    assert failed.provider_error_code == ProviderErrorCode.AUTHENTICATION.value
    assert failed.error == "模型服务鉴权失败，请检查密钥。"


def test_stream_idle_timeout_preserves_partial_text(qtbot, coordinator_factory) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.STALL,
        chunks=("停滞前片段",),
        first_delay_ms=1,
    )
    coordinator = coordinator_factory(provider, stream_idle_timeout_ms=25)

    coordinator.send_message("制造停滞")
    wait_until_idle(qtbot, coordinator)

    failed = coordinator.turns[0]
    assert failed.assistant_message.content == "停滞前片段"
    assert failed.assistant_message.status is MessageStatus.FAILED
    assert failed.terminal_reason is TurnTerminalReason.STREAM_IDLE_TIMEOUT
    assert failed.error == "回复流式输出超时，请重试。"


def test_failed_retry_reuses_turn_and_message_ids_without_duplicate_user(
    qtbot,
    coordinator_factory,
) -> None:
    class FailThenSucceedProvider:
        def __init__(self) -> None:
            self.requests: list[ChatRequest] = []
            self._lock = threading.Lock()

        async def stream(
            self,
            request: ChatRequest,
            cancellation: CancellationToken,
        ) -> AsyncIterator[str]:
            cancellation.raise_if_cancelled()
            with self._lock:
                self.requests.append(request)
                attempt = len(self.requests)
            if attempt == 1:
                yield "失败前片段"
                raise ChatProviderError("第一次失败")
            yield "重试成功"

    provider = FailThenSucceedProvider()
    coordinator = coordinator_factory(provider)
    added = []
    coordinator.turn_added.connect(added.append)

    original = coordinator.send_message("只发送一次")
    assert original is not None
    wait_until_idle(qtbot, coordinator)
    failed = coordinator.turns[0]
    assert failed.assistant_message.status is MessageStatus.FAILED

    assert coordinator.retry(failed.turn_id)
    assert len(coordinator.turns) == 1
    assert coordinator.turns[0].assistant_message.content == ""
    wait_until_idle(qtbot, coordinator)

    completed = coordinator.turns[0]
    assert len(added) == 1
    assert len(provider.requests) == 2
    assert completed.turn_id == original.turn_id
    assert completed.user_message.message_id == original.user_message.message_id
    assert completed.assistant_message.message_id == original.assistant_message.message_id
    assert completed.attempt == 2
    assert completed.assistant_message.content == "重试成功"
    assert completed.assistant_message.status is MessageStatus.COMPLETED
    second_prompt = provider.requests[1].messages
    assert [(message.role, message.content) for message in second_prompt[1:]] == [
        (PromptRole.USER, "只发送一次")
    ]
    assert second_prompt[0].role is PromptRole.SYSTEM
    assert "看见屏幕" in second_prompt[0].content


def test_late_event_for_terminal_request_is_ignored(qtbot, coordinator_factory) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=500)
    coordinator.send_message("迟到事件")
    request_id = coordinator.active_request_id
    assert request_id is not None

    assert coordinator.stop()
    coordinator._on_chunk(request_id, "不应出现")
    wait_until_idle(qtbot, coordinator)

    assert coordinator.turns[0].assistant_message.content == ""
    assert coordinator.turns[0].assistant_message.status is MessageStatus.STOPPED


def test_shutdown_cancels_worker_and_leaves_no_thread_or_timer(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=2_000)
    coordinator.send_message("退出测试")
    qtbot.waitUntil(lambda: provider.call_count == 1, timeout=1_000)

    assert coordinator.shutdown(wait_ms=1_000)

    assert coordinator.state is ConversationState.IDLE
    assert coordinator.active_request_id is None
    assert not coordinator.has_running_worker
    assert not coordinator.has_active_timers
    stopped = coordinator.turns[0]
    assert stopped.assistant_message.status is MessageStatus.STOPPED
    assert stopped.terminal_reason is TurnTerminalReason.SHUTDOWN
    assert coordinator.send_message("退出后不可再发") is None


def test_cancellation_token_wakes_waiter_thread_safely() -> None:
    token = CancellationToken()
    woke = threading.Event()

    def wait_for_cancel() -> None:
        if token.wait(1):
            woke.set()

    thread = threading.Thread(target=wait_for_cancel)
    thread.start()
    token.cancel()
    thread.join(timeout=1)

    assert token.is_cancelled
    assert woke.is_set()
    assert not thread.is_alive()


def test_fifty_rounds_have_unique_ids_and_no_state_leak(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(
        ScriptedScenario.NORMAL,
        chunks=("模拟完成",),
        first_delay_ms=0,
        chunk_delay_ms=0,
    )
    coordinator = coordinator_factory(provider)

    for index in range(50):
        assert coordinator.send_message(f"第 {index + 1} 轮") is not None
        wait_until_idle(qtbot, coordinator)

    turns = coordinator.turns
    assert len(turns) == 50
    assert provider.call_count == 50
    assert len({turn.turn_id for turn in turns}) == 50
    assert len({turn.user_message.message_id for turn in turns}) == 50
    assert len({turn.assistant_message.message_id for turn in turns}) == 50
    assert all(turn.assistant_message.content == "模拟完成" for turn in turns)
    assert all(turn.assistant_message.status is MessageStatus.COMPLETED for turn in turns)
    assert coordinator.state is ConversationState.IDLE
    assert coordinator.active_request_id is None
    assert not coordinator.has_running_worker
    assert not coordinator.has_active_timers


def test_provider_can_only_switch_when_fully_idle(qtbot, coordinator_factory) -> None:
    first = ScriptedChatProvider(ScriptedScenario.NEVER)
    second = ScriptedChatProvider(ScriptedScenario.NEVER)
    coordinator = coordinator_factory(first, first_chunk_timeout_ms=500)

    assert coordinator.set_provider(second)
    coordinator.send_message("第一次")
    qtbot.waitUntil(lambda: second.call_count == 1)
    assert not coordinator.set_provider(first)
    assert coordinator.stop()
    wait_until_idle(qtbot, coordinator)
    assert coordinator.set_provider(first)


def test_prompt_contains_system_boundary_and_only_twenty_recent_messages(
    qtbot,
    coordinator_factory,
) -> None:
    provider = ScriptedChatProvider(chunks=("回答",), first_delay_ms=0, chunk_delay_ms=0)
    coordinator = coordinator_factory(provider)

    for index in range(11):
        coordinator.send_message(f"消息 {index}")
        wait_until_idle(qtbot, coordinator)

    prompt = provider.requests[-1].messages
    assert prompt[0].role is PromptRole.SYSTEM
    assert "麦克风" in prompt[0].content
    assert len(prompt) == 21
    assert prompt[-1].role is PromptRole.USER
    assert prompt[-1].content == "消息 10"


def test_ui_boundary_rejects_oversized_visible_chunk(qtbot, coordinator_factory) -> None:
    provider = ScriptedChatProvider(ScriptedScenario.NEVER)
    coordinator = coordinator_factory(provider, first_chunk_timeout_ms=1_000)
    coordinator.send_message("超限保护")
    request_id = coordinator.active_request_id
    assert request_id is not None

    coordinator._on_chunk(request_id, "x" * (MAX_VISIBLE_RESPONSE_CHARS + 1))
    wait_until_idle(qtbot, coordinator)

    failed = coordinator.turns[0]
    assert failed.assistant_message.content == ""
    assert failed.terminal_reason is TurnTerminalReason.PROVIDER_ERROR
    assert failed.provider_error_code == ProviderErrorCode.PROTOCOL.value
