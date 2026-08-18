from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QRect, Signal

from amadeus_desktop.presence import PresenceProbe, PresenceSnapshot
from amadeus_desktop.proactive import (
    IDLE_THRESHOLD_SECONDS,
    ProactiveBlockReason,
    ProactiveMode,
    ProactiveOpportunityTracker,
    ProactivePolicyInput,
    ProactiveTrigger,
    build_proactive_request,
    evaluate_proactive_policy,
    validate_generated_greeting,
)
from amadeus_desktop.proactive_controller import (
    ProactiveInteractionController,
    ProactiveVisualPlan,
)
from amadeus_desktop.storage_models import ProactivePresentation


def _policy(
    now: datetime,
    *,
    trigger: ProactiveTrigger = ProactiveTrigger.STARTUP,
    displayed: int = 0,
    paused: str | None = None,
) -> ProactiveBlockReason:
    return evaluate_proactive_policy(
        ProactivePolicyInput(
            now=now,
            trigger=trigger,
            mode=ProactiveMode.RESTRAINED,
            quiet_start_minute=23 * 60,
            quiet_end_minute=8 * 60,
            daily_limit=2,
            displayed_today=displayed,
            paused_local_date=paused,
            pet_visible=True,
            conversation_active=False,
            settings_open=False,
            session_locked=False,
            fullscreen=False,
            data_writable=True,
        )
    )


def test_seven_day_clock_simulation_respects_midnight_quiet_and_daily_limit() -> None:
    first_day = datetime(2026, 8, 3)
    displayed_by_date: dict[str, int] = {}

    for offset in range(7):
        day = first_day + timedelta(days=offset)
        key = day.date().isoformat()
        displayed_by_date[key] = 0
        assert _policy(day.replace(hour=7, minute=59)) is ProactiveBlockReason.QUIET_HOURS
        assert _policy(day.replace(hour=8), displayed=0) is ProactiveBlockReason.ALLOWED
        displayed_by_date[key] += 1
        assert (
            _policy(
                day.replace(hour=12),
                trigger=ProactiveTrigger.IDLE,
                displayed=displayed_by_date[key],
            )
            is ProactiveBlockReason.ALLOWED
        )
        displayed_by_date[key] += 1
        assert (
            _policy(day.replace(hour=22, minute=59), displayed=displayed_by_date[key])
            is ProactiveBlockReason.DAILY_LIMIT
        )
        assert _policy(day.replace(hour=23)) is ProactiveBlockReason.QUIET_HOURS
        next_day = day + timedelta(days=1)
        assert _policy(next_day.replace(hour=0, minute=1)) is ProactiveBlockReason.QUIET_HOURS

    assert set(displayed_by_date.values()) == {2}


def test_pause_is_natural_day_scoped_and_restart_count_still_blocks() -> None:
    now = datetime(2026, 8, 3, 10)
    assert _policy(now, paused="2026-08-03") is ProactiveBlockReason.PAUSED_TODAY
    assert _policy(now + timedelta(days=1), paused="2026-08-03") is ProactiveBlockReason.ALLOWED

    restarted_tracker = ProactiveOpportunityTracker()
    assert restarted_tracker.consume_startup()
    assert _policy(now, displayed=2) is ProactiveBlockReason.DAILY_LIMIT


def test_each_real_idle_cycle_is_consumed_once_until_input_resumes() -> None:
    tracker = ProactiveOpportunityTracker()
    assert not tracker.observe_idle(IDLE_THRESHOLD_SECONDS - 1)
    assert tracker.observe_idle(IDLE_THRESHOLD_SECONDS)
    assert not tracker.observe_idle(IDLE_THRESHOLD_SECONDS + 600)
    assert not tracker.observe_idle(4)
    assert tracker.observe_idle(IDLE_THRESHOLD_SECONDS + 1)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"session_locked": True}, ProactiveBlockReason.SESSION_LOCKED),
        ({"fullscreen": True}, ProactiveBlockReason.FULLSCREEN),
        ({"pet_visible": False}, ProactiveBlockReason.PET_HIDDEN),
        ({"conversation_active": True}, ProactiveBlockReason.CONVERSATION_ACTIVE),
        ({"data_writable": False}, ProactiveBlockReason.DATA_UNAVAILABLE),
        ({"mode": ProactiveMode.OFF}, ProactiveBlockReason.MODE_OFF),
        (
            {"mode": ProactiveMode.STARTUP_ONLY, "trigger": ProactiveTrigger.IDLE},
            ProactiveBlockReason.TRIGGER_DISABLED,
        ),
    ],
)
def test_presence_and_runtime_suppression_fail_closed(
    updates: dict[str, object],
    expected: ProactiveBlockReason,
) -> None:
    base = ProactivePolicyInput(
        now=datetime(2026, 8, 3, 10),
        trigger=ProactiveTrigger.STARTUP,
        mode=ProactiveMode.RESTRAINED,
        quiet_start_minute=23 * 60,
        quiet_end_minute=8 * 60,
        daily_limit=2,
        displayed_today=0,
        paused_local_date=None,
        pet_visible=True,
        conversation_active=False,
        settings_open=False,
        session_locked=False,
        fullscreen=False,
        data_writable=True,
    )

    assert evaluate_proactive_policy(replace(base, **updates)) is expected


def test_ai_request_has_no_conversation_or_memory_payload() -> None:
    request = build_proactive_request(
        datetime(2026, 8, 3, 10, 30),
        ProactiveTrigger.STARTUP,
    )
    assert request.options.purpose.value == "proactive_greeting"
    assert len(request.messages) == 3
    joined = "\n".join(message.content for message in request.messages)
    assert "PRIVATE_CHAT_SENTINEL" not in joined
    assert "PRIVATE_MEMORY_SENTINEL" not in joined
    assert request.turn_id.startswith("proactive:")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  先喝口水吧。  ", "先喝口水吧。"),
        ("“休息一下。”", "休息一下。"),
        ("第一行\n第二行", "第一行 第二行"),
    ],
)
def test_generated_greeting_is_bounded_and_single_line(value: str, expected: str) -> None:
    assert validate_generated_greeting(value) == expected


class _FakeData(QObject):
    proactive_count_loaded = Signal(str, int)
    proactive_event_displayed = Signal(object)
    proactive_event_dismissed = Signal(object)
    proactive_greeting_persisted = Signal(object, object)
    proactive_presentation_loaded = Signal(object)

    def __init__(
        self,
        *,
        count: int = 0,
        presentation: ProactivePresentation | None = None,
    ) -> None:
        super().__init__()
        self.count = count
        self.presentation = presentation
        self.context_calls: list[bool] = []
        self.display_calls: list[tuple[str, str]] = []
        self.display_event_ids: list[str] = []
        self.display_times: list[datetime] = []
        self.display_cue_ids: list[str | None] = []
        self.persist_calls: list[tuple[str, str]] = []
        self.persist_metadata: list[tuple[str | None, str | None]] = []
        self.click_times: list[datetime] = []
        self.dismiss_calls: list[str] = []

    def load_proactive_display_count(self, local_date) -> bool:
        self.proactive_count_loaded.emit(local_date.isoformat(), self.count)
        return True

    def load_contextual_proactive_presentation(self, *, include_deep: bool = True) -> bool:
        self.context_calls.append(include_deep)
        self.proactive_presentation_loaded.emit(self.presentation)
        return True

    def record_proactive_display(self, trigger, local_date, **kwargs) -> bool:
        self.display_calls.append((trigger.value, local_date.isoformat()))
        event_id = kwargs["event_id"]
        self.display_event_ids.append(event_id)
        self.display_times.append(kwargs["displayed_at"])
        self.display_cue_ids.append(kwargs["cue_id"])
        self.proactive_event_displayed.emit(SimpleNamespace(event_id=event_id))
        return True

    def dismiss_proactive_event(self, event_id: str) -> bool:
        self.dismiss_calls.append(event_id)
        self.proactive_event_dismissed.emit(SimpleNamespace(event_id=event_id))
        return True

    def persist_proactive_greeting(self, event_id: str, greeting: str, **kwargs) -> bool:
        self.persist_calls.append((event_id, greeting))
        self.persist_metadata.append((kwargs["provider_name"], kwargs["model_name"]))
        self.click_times.append(kwargs["clicked_at"])
        event = SimpleNamespace(event_id=event_id)
        self.proactive_greeting_persisted.emit(event, SimpleNamespace())
        return True


class _FakeBubble(QObject):
    clicked = Signal()
    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.visible = False
        self.text = ""

    def isVisible(self) -> bool:  # noqa: N802 - Qt-compatible fake
        return self.visible

    def show_message(self, text: str, *_args, **_kwargs) -> None:
        self.text = text
        self.visible = True

    def dismiss(self, *, emit_signal: bool = False) -> None:
        self.visible = False
        if emit_signal:
            self.dismissed.emit()


class _FakeRunner:
    def __init__(self) -> None:
        self.starts = 0

    def start(self, _request, *, on_success, on_failure) -> bool:
        del on_success
        self.starts += 1
        on_failure("synthetic")
        return True


class _DeferredRunner:
    def __init__(self) -> None:
        self.starts = 0
        self._on_success = None
        self._on_failure = None
        self.request = None
        self.resumes = 0
        self.pauses: list[int] = []

    def start(self, request, *, on_success, on_failure) -> bool:
        self.starts += 1
        self.request = request
        self._on_success = on_success
        self._on_failure = on_failure
        return True

    def resume(self) -> None:
        self.resumes += 1

    def pause(self, wait_ms: int) -> bool:
        self.pauses.append(wait_ms)
        return True

    def succeed(self, content: str = "我在这里。") -> None:
        assert self._on_success is not None
        self._on_success(content)

    def fail(self) -> None:
        assert self._on_failure is not None
        self._on_failure("synthetic")


class _FakePresence(PresenceProbe):
    def __init__(self) -> None:
        self.value = PresenceSnapshot(0, False, False)

    def snapshot(self) -> PresenceSnapshot:
        return self.value


def _deferred_ai_controller(
    *,
    data: _FakeData,
    bubble: _FakeBubble,
    runner: _DeferredRunner,
    settings: dict[str, object],
    releases: list[bool],
    cancellations: list[int],
) -> ProactiveInteractionController:
    now = datetime(2026, 8, 3, 10)
    return ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=runner,  # type: ignore[arg-type]
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: True,
        provider_metadata=lambda: ("provider", "model"),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: releases.append(True),
        cancel_ai_lane=lambda wait_ms: cancellations.append(wait_ms) is None,
        open_chat=lambda: None,
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )


def _ai_settings(*, daily_limit: int = 2) -> dict[str, object]:
    return {
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": daily_limit,
            "paused_local_date": None,
            "ai_greetings_enabled": True,
        }
    }


def _complete_deferred(runner: _DeferredRunner, outcome: str) -> None:
    if outcome == "success":
        runner.succeed()
    else:
        runner.fail()


def test_controller_local_first_display_counts_only_after_show_and_click_persists(qtbot) -> None:
    now = datetime(2026, 8, 3, 10)
    data = _FakeData()
    bubble = _FakeBubble()
    runner = _FakeRunner()
    opened: list[bool] = []
    settings = {
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": 2,
            "paused_local_date": None,
            "ai_greetings_enabled": False,
        }
    }
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=runner,  # type: ignore[arg-type]
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(0, 0, 10, 10),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: False,
        provider_metadata=lambda: (None, None),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: opened.append(True),
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )
    controller.start()
    qtbot.waitUntil(lambda: bubble.visible)

    assert data.display_calls == [("startup", "2026-08-03")]
    assert data.display_times[0].tzinfo is not None
    assert runner.starts == 0
    bubble.clicked.emit()
    assert data.persist_calls == [(data.display_event_ids[0], bubble.text)]
    assert data.click_times[0].tzinfo is not None
    assert opened == [True]
    controller.stop()


def test_contextual_followup_uses_generic_bubble_and_frozen_click_text_without_network(
    qtbot,
) -> None:
    now = datetime(2026, 8, 3, 10)
    presentation = ProactivePresentation(
        preview_text="有件你之前提过的事，我还记着。想继续聊聊吗？",
        expanded_text="你确认过：等项目结果出来后，再继续聊这个话题。",
        cue_id="cue-1",
        source_label="待续话题",
    )
    data = _FakeData(presentation=presentation)
    bubble = _FakeBubble()
    runner = _FakeRunner()
    opened: list[bool] = []
    settings = {
        "memory": {"enabled": True, "deep_memory_enabled": True},
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": 2,
            "paused_local_date": None,
            "ai_greetings_enabled": True,
            "contextual_followups_enabled": True,
        },
    }
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=runner,  # type: ignore[arg-type]
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: True,
        provider_metadata=lambda: ("must-not-run", "must-not-run"),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: opened.append(True),
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )

    controller.start()
    qtbot.waitUntil(lambda: bubble.visible)

    assert data.context_calls == [True]
    assert bubble.text == presentation.preview_text
    assert presentation.expanded_text not in bubble.text
    assert data.display_cue_ids == [presentation.cue_id]
    assert runner.starts == 0
    bubble.clicked.emit()
    assert data.persist_calls == [(data.display_event_ids[0], presentation.expanded_text)]
    assert data.persist_metadata == [(None, None)]
    assert opened == [True]
    controller.stop()


def test_contextual_followup_off_skips_context_lookup_and_keeps_generic_path(qtbot) -> None:
    now = datetime(2026, 8, 3, 10)
    data = _FakeData(
        presentation=ProactivePresentation(
            preview_text="private-preview-must-not-be-used",
            expanded_text="private-expanded-must-not-be-used",
            cue_id="cue-disabled",
        )
    )
    bubble = _FakeBubble()
    runner = _FakeRunner()
    settings = {
        "memory": {"enabled": True, "deep_memory_enabled": True},
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": 2,
            "paused_local_date": None,
            "ai_greetings_enabled": False,
            "contextual_followups_enabled": False,
        },
    }
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=runner,  # type: ignore[arg-type]
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: False,
        provider_metadata=lambda: (None, None),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: None,
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )

    controller.start()
    qtbot.waitUntil(lambda: bubble.visible)

    assert data.context_calls == []
    assert data.display_cue_ids == [None]
    assert "private" not in bubble.text
    controller.stop()


def test_contextual_display_transaction_failure_closes_private_preview_and_falls_back(
    qtbot,
) -> None:
    now = datetime(2026, 8, 3, 10)
    presentation = ProactivePresentation(
        preview_text="有件你之前提过的事，我还记着。想继续聊聊吗？",
        expanded_text="不应在失败气泡中出现的冻结原文",
        cue_id="cue-race",
    )
    data = _FakeData(presentation=presentation)
    bubble = _FakeBubble()
    runner = _FakeRunner()
    settings = {
        "memory": {"enabled": True, "deep_memory_enabled": True},
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": 2,
            "paused_local_date": None,
            "ai_greetings_enabled": False,
            "contextual_followups_enabled": True,
        },
    }
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=runner,  # type: ignore[arg-type]
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: False,
        provider_metadata=lambda: (None, None),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: None,
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )

    controller.start()
    qtbot.waitUntil(lambda: bubble.visible and data.display_cue_ids == ["cue-race"])
    controller.persistence_failed("proactive_display", "conflict")
    qtbot.waitUntil(lambda: len(data.display_cue_ids) == 2)

    assert bubble.visible
    assert data.display_cue_ids == ["cue-race", None]
    assert bubble.text != presentation.preview_text
    assert presentation.expanded_text not in bubble.text
    controller.stop()


def test_controller_ai_is_opt_in_and_failure_falls_back_to_local(qtbot) -> None:
    now = datetime(2026, 8, 3, 10)
    data = _FakeData()
    bubble = _FakeBubble()
    runner = _FakeRunner()
    settings = {
        "proactive": {
            "mode": "restrained",
            "quiet_start_minute": 23 * 60,
            "quiet_end_minute": 8 * 60,
            "daily_limit": 2,
            "paused_local_date": None,
            "ai_greetings_enabled": True,
        }
    }
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=runner,  # type: ignore[arg-type]
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: True,
        provider_metadata=lambda: ("provider", "model"),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: None,
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )
    controller.start()
    qtbot.waitUntil(lambda: bubble.visible)

    assert runner.starts == 1
    assert data.display_calls == [("startup", "2026-08-03")]
    controller.stop()


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_ai_callback_rechecks_hot_daily_limit_before_display(qtbot, outcome: str) -> None:
    data = _FakeData(count=1)
    bubble = _FakeBubble()
    runner = _DeferredRunner()
    settings = _ai_settings(daily_limit=2)
    releases: list[bool] = []
    cancellations: list[int] = []
    controller = _deferred_ai_controller(
        data=data,
        bubble=bubble,
        runner=runner,
        settings=settings,
        releases=releases,
        cancellations=cancellations,
    )
    controller.start()
    qtbot.waitUntil(lambda: runner.starts == 1)

    proactive_settings = settings["proactive"]
    assert isinstance(proactive_settings, dict)
    proactive_settings["daily_limit"] = 1
    _complete_deferred(runner, outcome)

    assert not bubble.visible
    assert data.display_calls == []
    assert releases == [True]
    assert cancellations == []
    assert controller.stop()


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_ai_callback_arriving_after_ai_is_disabled_is_discarded(qtbot, outcome: str) -> None:
    data = _FakeData()
    bubble = _FakeBubble()
    runner = _DeferredRunner()
    settings = _ai_settings()
    releases: list[bool] = []
    cancellations: list[int] = []
    controller = _deferred_ai_controller(
        data=data,
        bubble=bubble,
        runner=runner,
        settings=settings,
        releases=releases,
        cancellations=cancellations,
    )
    controller.start()
    qtbot.waitUntil(lambda: runner.starts == 1)

    proactive_settings = settings["proactive"]
    assert isinstance(proactive_settings, dict)
    proactive_settings["ai_greetings_enabled"] = False
    _complete_deferred(runner, outcome)

    assert not bubble.visible
    assert data.display_calls == []
    assert releases == [True]
    assert cancellations == []
    assert controller.stop()


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_ai_callback_after_stop_is_discarded_without_double_release(qtbot, outcome: str) -> None:
    data = _FakeData()
    bubble = _FakeBubble()
    runner = _DeferredRunner()
    settings = _ai_settings()
    releases: list[bool] = []
    cancellations: list[int] = []
    controller = _deferred_ai_controller(
        data=data,
        bubble=bubble,
        runner=runner,
        settings=settings,
        releases=releases,
        cancellations=cancellations,
    )
    controller.start()
    qtbot.waitUntil(lambda: runner.starts == 1)

    assert controller.stop(wait_ms=37)
    assert cancellations == [37]
    assert releases == [True]

    _complete_deferred(runner, outcome)

    assert not bubble.visible
    assert data.display_calls == []
    assert releases == [True]


def test_active_visual_claims_existing_opportunity_and_uses_volatile_runner(qtbot) -> None:
    now = datetime(2026, 8, 3, 10)
    data = _FakeData()
    bubble = _FakeBubble()
    text_runner = _DeferredRunner()
    visual_runner = _DeferredRunner()
    settings = _ai_settings()
    settings["proactive"]["ai_greetings_enabled"] = False  # type: ignore[index]
    statuses: list[str] = []
    releases: list[bool] = []
    request = build_proactive_request(now, ProactiveTrigger.STARTUP)
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=text_runner,  # type: ignore[arg-type]
        visual_generation_runner=visual_runner,  # type: ignore[arg-type]
        visual_plan_builder=lambda _now, _trigger: ProactiveVisualPlan(
            request,
            ("vision-provider", "vision-model"),
        ),
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: False,
        provider_metadata=lambda: (None, None),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: releases.append(True),
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: None,
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )
    controller.status_changed.connect(statuses.append)

    controller.start()
    qtbot.waitUntil(lambda: visual_runner.starts == 1)
    assert text_runner.starts == 0
    assert visual_runner.request is request
    assert visual_runner.resumes == 1

    visual_runner.succeed("画面很安静，慢慢来就好。")
    qtbot.waitUntil(lambda: bubble.visible)
    bubble.clicked.emit()

    assert releases == [True]
    assert data.persist_metadata == [("vision-provider", "vision-model")]
    assert "visual_generation_failed" not in statuses
    assert controller.stop()


@pytest.mark.parametrize("outcome", ["failure", "invalid-success"])
def test_active_visual_failure_drops_frame_without_local_or_text_fallback(
    qtbot,
    outcome: str,
) -> None:
    now = datetime(2026, 8, 3, 10)
    data = _FakeData()
    bubble = _FakeBubble()
    text_runner = _DeferredRunner()
    visual_runner = _DeferredRunner()
    settings = _ai_settings()
    statuses: list[str] = []
    controller = ProactiveInteractionController(
        data=data,
        bubble=bubble,  # type: ignore[arg-type]
        generation_runner=text_runner,  # type: ignore[arg-type]
        visual_generation_runner=visual_runner,  # type: ignore[arg-type]
        visual_plan_builder=lambda _now, _trigger: ProactiveVisualPlan(
            build_proactive_request(now, ProactiveTrigger.STARTUP)
        ),
        presence_probe=_FakePresence(),
        settings_reader=lambda: settings,
        clock=lambda: now,
        pet_visible=lambda: True,
        conversation_active=lambda: False,
        settings_open=lambda: False,
        data_writable=lambda: True,
        exiting=lambda: False,
        pet_geometry=lambda: QRect(),
        work_areas=lambda: [QRect(0, 0, 100, 100)],
        provider_configured=lambda: True,
        provider_metadata=lambda: ("text", "text"),
        acquire_ai_lane=lambda: True,
        release_ai_lane=lambda: None,
        cancel_ai_lane=lambda _wait: True,
        open_chat=lambda: None,
        startup_delay_ms=1,
        poll_interval_ms=60_000,
    )
    controller.status_changed.connect(statuses.append)

    controller.start()
    qtbot.waitUntil(lambda: visual_runner.starts == 1)
    if outcome == "failure":
        visual_runner.fail()
    else:
        visual_runner.succeed("x" * 10_000)
    qtbot.waitUntil(lambda: "visual_generation_failed" in statuses)

    assert not bubble.visible
    assert not data.display_calls
    assert text_runner.starts == 0
    assert controller.stop()
