"""Qt orchestration for restrained, auditable proactive greetings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from amadeus_desktop.background_generation import BackgroundGenerationRunner
from amadeus_desktop.chat_models import ChatRequest
from amadeus_desktop.greetings import GreetingCatalog, GreetingCatalogError, load_greeting_catalog
from amadeus_desktop.presence import PresenceProbe, PresenceSnapshot
from amadeus_desktop.proactive import (
    POLL_INTERVAL_MS,
    STARTUP_DELAY_MS,
    ProactiveBlockReason,
    ProactiveMode,
    ProactiveOpportunityTracker,
    ProactivePolicyInput,
    ProactiveTrigger,
    build_proactive_request,
    evaluate_proactive_policy,
    validate_generated_greeting,
)
from amadeus_desktop.storage_models import (
    ProactivePresentation,
)
from amadeus_desktop.storage_models import (
    ProactiveTrigger as StorageProactiveTrigger,
)
from amadeus_desktop.ui.greeting_bubble import GreetingBubble


class ProactiveDataGateway(Protocol):
    proactive_count_loaded: Any
    proactive_event_displayed: Any
    proactive_event_dismissed: Any
    proactive_greeting_persisted: Any
    proactive_presentation_loaded: Any

    def load_proactive_display_count(self, local_date: date) -> bool: ...

    def record_proactive_display(
        self,
        trigger: StorageProactiveTrigger,
        local_date: date,
        *,
        event_id: str | None = None,
        displayed_at: datetime | None = None,
        cue_id: str | None = None,
    ) -> bool: ...

    def load_contextual_proactive_presentation(
        self,
        *,
        include_deep: bool = True,
    ) -> bool: ...

    def dismiss_proactive_event(self, event_id: str) -> bool: ...

    def persist_proactive_greeting(
        self,
        event_id: str,
        greeting: str,
        *,
        clicked_at: datetime | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> bool: ...


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ProactiveVisualPlan:
    """A volatile visual request, or a fail-closed claimed opportunity."""

    request: ChatRequest | None
    provider_metadata: tuple[str | None, str | None] = (None, None)


VisualPlanBuilder = Callable[[datetime, ProactiveTrigger], ProactiveVisualPlan | None]


class ProactiveInteractionController(QObject):
    """Own timers and one greeting at a time without ever taking focus."""

    status_changed = Signal(str)
    greeting_displayed = Signal(str)
    greeting_persisted = Signal()
    local_date_changed = Signal(str)

    def __init__(
        self,
        *,
        data: ProactiveDataGateway,
        bubble: GreetingBubble,
        generation_runner: BackgroundGenerationRunner,
        presence_probe: PresenceProbe,
        settings_reader: Callable[[], Mapping[str, object]],
        clock: Clock,
        pet_visible: Callable[[], bool],
        conversation_active: Callable[[], bool],
        settings_open: Callable[[], bool],
        data_writable: Callable[[], bool],
        exiting: Callable[[], bool],
        pet_geometry: Callable[[], object],
        work_areas: Callable[[], list[object]],
        provider_configured: Callable[[], bool],
        provider_metadata: Callable[[], tuple[str | None, str | None]],
        acquire_ai_lane: Callable[[], bool],
        release_ai_lane: Callable[[], None],
        cancel_ai_lane: Callable[[int], bool],
        open_chat: Callable[[], None],
        visual_generation_runner: BackgroundGenerationRunner | None = None,
        visual_plan_builder: VisualPlanBuilder | None = None,
        greeting_catalog_path: Path | None = None,
        startup_delay_ms: int = STARTUP_DELAY_MS,
        poll_interval_ms: int = POLL_INTERVAL_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if startup_delay_ms < 0 or poll_interval_ms <= 0:
            raise ValueError("proactive timer intervals are invalid")
        self._data = data
        self._bubble = bubble
        self._generation_runner = generation_runner
        self._presence_probe = presence_probe
        self._settings_reader = settings_reader
        self._clock = clock
        self._pet_visible = pet_visible
        self._conversation_active = conversation_active
        self._settings_open = settings_open
        self._data_writable = data_writable
        self._exiting = exiting
        self._pet_geometry = pet_geometry
        self._work_areas = work_areas
        self._provider_configured = provider_configured
        self._provider_metadata = provider_metadata
        self._acquire_ai_lane = acquire_ai_lane
        self._release_ai_lane = release_ai_lane
        self._cancel_ai_lane = cancel_ai_lane
        self._open_chat = open_chat
        self._visual_generation_runner = visual_generation_runner
        self._visual_plan_builder = visual_plan_builder
        self._tracker = ProactiveOpportunityTracker()
        self._pending_trigger: ProactiveTrigger | None = None
        self._pending_date: date | None = None
        self._pending_displayed_count: int | None = None
        self._active_event_id: str | None = None
        self._active_greeting: str | None = None
        self._active_preview: str | None = None
        self._active_cue_id: str | None = None
        self._active_trigger: ProactiveTrigger | None = None
        self._active_displayed_count = 0
        self._active_provider_metadata: tuple[str | None, str | None] = (None, None)
        self._click_pending = False
        self._dismiss_pending = False
        self._ai_lane_owned = False
        self._active_generation_runner: BackgroundGenerationRunner | None = None
        self._active_generation_is_visual = False
        self._active_generation_metadata: tuple[str | None, str | None] = (None, None)
        self._running = False
        self._observed_local_date = self._clock().date()
        self._using_local_catalog = False
        try:
            self._catalog = load_greeting_catalog(greeting_catalog_path)
            self._using_local_catalog = bool(
                greeting_catalog_path is not None and greeting_catalog_path.exists()
            )
        except GreetingCatalogError:
            self._catalog = load_greeting_catalog(None)
            self.status_changed.emit("local_greeting_invalid")

        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.setInterval(startup_delay_ms)
        self._startup_timer.timeout.connect(self._on_startup_due)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self.poll)

        data.proactive_count_loaded.connect(self._on_count_loaded)
        data.proactive_event_displayed.connect(self._on_event_displayed)
        data.proactive_event_dismissed.connect(self._on_event_dismissed)
        data.proactive_greeting_persisted.connect(self._on_greeting_persisted)
        presentation_signal = getattr(data, "proactive_presentation_loaded", None)
        if presentation_signal is not None:
            presentation_signal.connect(self._on_presentation_loaded)
        bubble.clicked.connect(self._on_bubble_clicked)
        bubble.dismissed.connect(self._on_bubble_dismissed)

    @property
    def has_visible_greeting(self) -> bool:
        return self._bubble.isVisible()

    @property
    def using_local_catalog(self) -> bool:
        return self._using_local_catalog

    def start(self) -> None:
        if self._running:
            return
        self._emit_local_date_change_if_needed()
        self._running = True
        self._startup_timer.start()
        self._poll_timer.start()

    def stop(self, *, wait_ms: int = 2_000) -> bool:
        self._running = False
        self._startup_timer.stop()
        self._poll_timer.stop()
        self._bubble.dismiss(emit_signal=True)
        self._clear_pending_opportunity()
        self._clear_active()
        if not self._ai_lane_owned:
            return True
        stopped = self._cancel_active_generation(wait_ms)
        self._release_ai()
        return stopped

    def cancel_ai_generation(self, *, wait_ms: int = 0) -> bool:
        """Cancel only an automatic model request before a provider switch.

        Clearing the pending opportunity first makes any late worker callback
        stale, so it cannot display a local fallback after the provider switch
        has started.
        """

        self._clear_pending_opportunity()
        if not self._ai_lane_owned:
            return True
        stopped = self._cancel_active_generation(wait_ms)
        self._release_ai()
        return stopped

    @Slot()
    def poll(self) -> None:
        if not self._running or self._exiting():
            return
        self._emit_local_date_change_if_needed()
        presence = self._safe_presence()
        if self._tracker.observe_idle(presence.idle_seconds):
            self._request_opportunity(ProactiveTrigger.IDLE)

    def dismiss_current(self) -> None:
        if self._bubble.isVisible():
            self._bubble.dismiss(emit_signal=True)

    def set_catalog(self, catalog: GreetingCatalog) -> None:
        if not isinstance(catalog, GreetingCatalog):
            raise ValueError("greeting catalog is invalid")
        self._catalog = catalog
        self._using_local_catalog = True

    def persistence_failed(self, operation: str, category: str = "") -> None:
        if not operation.startswith("proactive"):
            return
        if operation == "proactive_context":
            trigger = self._pending_trigger
            if trigger is not None and self._pending_date == self._clock().date():
                self._continue_generic(trigger, self._clock())
                return
            self._clear_pending_opportunity()
            self.status_changed.emit("storage_error")
            return
        if operation == "proactive_display":
            trigger = self._active_trigger
            displayed_count = self._active_displayed_count
            contextual = self._active_cue_id is not None
            self._bubble.dismiss()
            self._clear_active()
            if contextual and trigger is not None and self._running and not self._exiting():
                now = self._clock()
                self._pending_trigger = trigger
                self._pending_date = now.date()
                self._pending_displayed_count = displayed_count
                self._display_local(trigger, now)
                return
        elif operation == "proactive_click":
            event_id = self._active_event_id
            self._clear_active()
            if event_id is not None:
                self._data.dismiss_proactive_event(event_id)
        elif operation == "proactive_dismiss":
            self._clear_active()
        self._clear_pending_opportunity()
        self.status_changed.emit("storage_error")

    @Slot()
    def _on_startup_due(self) -> None:
        if not self._running:
            return
        self._emit_local_date_change_if_needed()
        if self._tracker.consume_startup():
            self._request_opportunity(ProactiveTrigger.STARTUP)

    def _request_opportunity(self, trigger: ProactiveTrigger) -> None:
        if self._pending_trigger is not None or self._active_greeting is not None:
            return
        now = self._clock()
        self._pending_trigger = trigger
        self._pending_date = now.date()
        self._pending_displayed_count = None
        if not self._data.load_proactive_display_count(now.date()):
            self._clear_pending_opportunity()
            self.status_changed.emit("storage_unavailable")

    @Slot(str, int)
    def _on_count_loaded(self, local_date_iso: str, displayed_count: int) -> None:
        trigger = self._pending_trigger
        expected_date = self._pending_date
        if trigger is None or expected_date is None or expected_date.isoformat() != local_date_iso:
            return
        now = self._clock()
        if now.date() != expected_date:
            self._clear_pending_opportunity()
            return
        self._pending_displayed_count = max(0, int(displayed_count))
        settings = self._settings_reader()["proactive"]
        assert isinstance(settings, Mapping)
        presence = self._safe_presence()
        reason = evaluate_proactive_policy(
            ProactivePolicyInput(
                now=now,
                trigger=trigger,
                mode=ProactiveMode(str(settings["mode"])),
                quiet_start_minute=int(settings["quiet_start_minute"]),
                quiet_end_minute=int(settings["quiet_end_minute"]),
                daily_limit=int(settings["daily_limit"]),
                displayed_today=max(0, int(displayed_count)),
                paused_local_date=(
                    None
                    if settings["paused_local_date"] is None
                    else str(settings["paused_local_date"])
                ),
                pet_visible=self._pet_visible(),
                conversation_active=self._conversation_active() or self._exiting(),
                settings_open=self._settings_open(),
                session_locked=presence.session_locked,
                fullscreen=presence.fullscreen,
                data_writable=self._data_writable(),
            )
        )
        if reason is not ProactiveBlockReason.ALLOWED:
            self._clear_pending_opportunity()
            self.status_changed.emit(reason.value)
            return
        root_settings = self._settings_reader()
        memory_settings = root_settings.get("memory", {})
        if (
            bool(settings.get("contextual_followups_enabled", False))
            and isinstance(memory_settings, Mapping)
            and bool(memory_settings.get("enabled", True))
        ):
            loader = getattr(self._data, "load_contextual_proactive_presentation", None)
            if callable(loader) and loader(
                include_deep=bool(memory_settings.get("deep_memory_enabled", True))
            ):
                return
        self._continue_generic(trigger, now)

    @Slot(object)
    def _on_presentation_loaded(self, value: object) -> None:
        trigger = self._pending_trigger
        expected_date = self._pending_date
        if trigger is None or expected_date is None:
            return
        now = self._clock()
        if now.date() != expected_date:
            self._clear_pending_opportunity()
            return
        if isinstance(value, ProactivePresentation) and value.cue_id is not None:
            self._display(
                trigger,
                now,
                value.preview_text,
                expanded_text=value.expanded_text,
                cue_id=value.cue_id,
            )
            return
        self._continue_generic(trigger, now)

    def _continue_generic(self, trigger: ProactiveTrigger, now: datetime) -> None:
        """Continue the existing P7 generic greeting path without private context."""

        if self._pending_trigger is not trigger or self._pending_date != now.date():
            self._clear_pending_opportunity()
            return
        settings = self._settings_reader()["proactive"]
        assert isinstance(settings, Mapping)
        visual_plan = self._build_visual_plan(now, trigger)
        if visual_plan is not None:
            if visual_plan.request is None or self._visual_generation_runner is None:
                self._clear_pending_opportunity()
                self.status_changed.emit("visual_unavailable")
                return
            if self._start_ai_generation(
                trigger,
                now,
                request=visual_plan.request,
                runner=self._visual_generation_runner,
                visual=True,
                provider_metadata=visual_plan.provider_metadata,
            ):
                return
            self._clear_pending_opportunity()
            self.status_changed.emit("visual_busy")
            return
        if (
            bool(settings["ai_greetings_enabled"])
            and self._provider_configured()
            and self._start_ai_generation(trigger, now)
        ):
            return
        self._display_local(trigger, now)

    def _start_ai_generation(
        self,
        trigger: ProactiveTrigger,
        now: datetime,
        *,
        request: ChatRequest | None = None,
        runner: BackgroundGenerationRunner | None = None,
        visual: bool = False,
        provider_metadata: tuple[str | None, str | None] = (None, None),
    ) -> bool:
        if not self._acquire_ai_lane():
            return False
        self._ai_lane_owned = True
        selected_runner = runner or self._generation_runner
        selected_request = request or build_proactive_request(now, trigger)
        self._active_generation_runner = selected_runner
        self._active_generation_is_visual = visual
        self._active_generation_metadata = provider_metadata
        if selected_runner is not self._generation_runner:
            selected_runner.resume()
        started = selected_runner.start(
            selected_request,
            on_success=lambda content: self._on_ai_success(trigger, now, content),
            on_failure=lambda _category: self._on_ai_failure(trigger, now),
        )
        if not started:
            self._release_ai()
        return started

    def _on_ai_success(self, trigger: ProactiveTrigger, now: datetime, content: str) -> None:
        was_visual = self._active_generation_is_visual
        provider_metadata = (
            self._active_generation_metadata if was_visual else self._provider_metadata()
        )
        self._release_ai()
        if (
            not self._running
            or self._exiting()
            or self._pending_trigger is not trigger
            or self._pending_date != now.date()
            or (
                not was_visual
                and not bool(self._settings_reader()["proactive"]["ai_greetings_enabled"])
            )
        ):
            self._clear_pending_opportunity()
            return
        try:
            greeting = validate_generated_greeting(content)
        except ValueError:
            if was_visual:
                self._clear_pending_opportunity()
                self.status_changed.emit("visual_generation_failed")
                return
            self._display_local(trigger, now)
            return
        self._display(trigger, now, greeting, provider_metadata=provider_metadata)

    def _on_ai_failure(self, trigger: ProactiveTrigger, now: datetime) -> None:
        was_visual = self._active_generation_is_visual
        self._release_ai()
        if was_visual:
            self._clear_pending_opportunity()
            self.status_changed.emit("visual_generation_failed")
            return
        if (
            self._running
            and not self._exiting()
            and self._pending_trigger is trigger
            and self._pending_date == now.date()
            and bool(self._settings_reader()["proactive"]["ai_greetings_enabled"])
        ):
            self._display_local(trigger, now)
            return
        self._clear_pending_opportunity()

    def _display_local(self, trigger: ProactiveTrigger, now: datetime) -> None:
        greeting = self._catalog.choose(trigger, f"{now.date().isoformat()}:{trigger.value}")
        self._display(trigger, now, greeting)

    def _display(
        self,
        trigger: ProactiveTrigger,
        now: datetime,
        greeting: str,
        *,
        provider_metadata: tuple[str | None, str | None] = (None, None),
        expanded_text: str | None = None,
        cue_id: str | None = None,
    ) -> None:
        current = self._clock()
        settings = self._settings_reader()["proactive"]
        assert isinstance(settings, Mapping)
        presence = self._safe_presence()
        reason = evaluate_proactive_policy(
            ProactivePolicyInput(
                now=current,
                trigger=trigger,
                mode=ProactiveMode(str(settings["mode"])),
                quiet_start_minute=int(settings["quiet_start_minute"]),
                quiet_end_minute=int(settings["quiet_end_minute"]),
                daily_limit=int(settings["daily_limit"]),
                displayed_today=max(0, int(self._pending_displayed_count or 0)),
                paused_local_date=(
                    None
                    if settings["paused_local_date"] is None
                    else str(settings["paused_local_date"])
                ),
                pet_visible=self._pet_visible(),
                conversation_active=self._conversation_active() or self._exiting(),
                settings_open=self._settings_open(),
                session_locked=presence.session_locked,
                fullscreen=presence.fullscreen,
                data_writable=self._data_writable(),
            )
        )
        if (
            not self._running
            or self._exiting()
            or current.date() != now.date()
            or reason is not ProactiveBlockReason.ALLOWED
        ):
            self._clear_pending_opportunity()
            return
        event_id = uuid4().hex
        self._active_preview = greeting
        self._active_greeting = expanded_text if expanded_text is not None else greeting
        self._active_cue_id = cue_id
        self._active_trigger = trigger
        self._active_displayed_count = max(0, int(self._pending_displayed_count or 0))
        self._active_event_id = event_id
        self._active_provider_metadata = provider_metadata
        self._click_pending = False
        self._dismiss_pending = False
        try:
            self._bubble.show_message(
                greeting,
                self._pet_geometry(),
                self._work_areas(),
            )
        except (RuntimeError, ValueError):
            self._clear_active()
            self._clear_pending_opportunity()
            self.status_changed.emit("display_failed")
            return
        self.greeting_displayed.emit(trigger.value)
        submitted = self._data.record_proactive_display(
            StorageProactiveTrigger(trigger.value),
            current.date(),
            event_id=event_id,
            displayed_at=_aware_local_time(current),
            cue_id=cue_id,
        )
        self._clear_pending_opportunity()
        if not submitted:
            self._bubble.dismiss()
            self._clear_active()
            self.status_changed.emit("storage_unavailable")

    @Slot(object)
    def _on_event_displayed(self, event: object) -> None:
        if self._active_greeting is None:
            return
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, str) or event_id != self._active_event_id:
            return
        if self._click_pending:
            self._persist_clicked()
        elif self._dismiss_pending:
            self._dismiss_active()

    @Slot()
    def _on_bubble_clicked(self) -> None:
        if self._active_greeting is None:
            return
        self._click_pending = True
        if self._active_event_id is not None:
            self._persist_clicked()

    def _persist_clicked(self) -> None:
        event_id = self._active_event_id
        greeting = self._active_greeting
        if event_id is None or greeting is None or not self._click_pending:
            return
        self._click_pending = False
        provider_name, model_name = self._active_provider_metadata
        if not self._data.persist_proactive_greeting(
            event_id,
            greeting,
            clicked_at=_aware_local_time(self._clock()),
            provider_name=provider_name,
            model_name=model_name,
        ):
            self.status_changed.emit("storage_unavailable")

    @Slot()
    def _on_bubble_dismissed(self) -> None:
        if self._active_greeting is None:
            return
        self._dismiss_pending = True
        if self._active_event_id is not None:
            self._dismiss_active()

    def _dismiss_active(self) -> None:
        event_id = self._active_event_id
        if event_id is None or not self._dismiss_pending:
            return
        self._dismiss_pending = False
        if not self._data.dismiss_proactive_event(event_id):
            self.status_changed.emit("storage_unavailable")

    @Slot(object)
    def _on_event_dismissed(self, event: object) -> None:
        if getattr(event, "event_id", None) == self._active_event_id:
            self._clear_active()

    @Slot(object, object)
    def _on_greeting_persisted(self, event: object, _message: object) -> None:
        if getattr(event, "event_id", None) != self._active_event_id:
            return
        self._clear_active()
        self.greeting_persisted.emit()
        self._open_chat()

    def _clear_active(self) -> None:
        self._active_event_id = None
        self._active_greeting = None
        self._active_preview = None
        self._active_cue_id = None
        self._active_trigger = None
        self._active_displayed_count = 0
        self._active_provider_metadata = (None, None)
        self._click_pending = False
        self._dismiss_pending = False

    def _clear_pending_opportunity(self) -> None:
        self._pending_trigger = None
        self._pending_date = None
        self._pending_displayed_count = None

    def _release_ai(self) -> None:
        if not self._ai_lane_owned:
            return
        self._ai_lane_owned = False
        self._active_generation_runner = None
        self._active_generation_is_visual = False
        self._active_generation_metadata = (None, None)
        self._release_ai_lane()

    def _cancel_active_generation(self, wait_ms: int) -> bool:
        runner = self._active_generation_runner
        if runner is None or runner is self._generation_runner:
            return self._cancel_ai_lane(wait_ms)
        return runner.pause(wait_ms)

    def _build_visual_plan(
        self,
        now: datetime,
        trigger: ProactiveTrigger,
    ) -> ProactiveVisualPlan | None:
        builder = self._visual_plan_builder
        if builder is None:
            return None
        try:
            return builder(now, trigger)
        except Exception:  # noqa: BLE001 - no captured content enters diagnostics
            return ProactiveVisualPlan(None)

    def _emit_local_date_change_if_needed(self) -> None:
        current = self._clock().date()
        if current == self._observed_local_date:
            return
        self._observed_local_date = current
        self.local_date_changed.emit(current.isoformat())

    def _safe_presence(self) -> PresenceSnapshot:
        try:
            return self._presence_probe.snapshot()
        except Exception:  # noqa: BLE001 - fail closed without OS detail
            return PresenceSnapshot(0.0, True, True)


def _aware_local_time(value: datetime) -> datetime:
    """Normalize injectable local clocks before crossing the SQLite UTC boundary."""

    return value if value.tzinfo is not None else value.astimezone()
