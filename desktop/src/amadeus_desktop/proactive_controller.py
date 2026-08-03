"""Qt orchestration for restrained, auditable proactive greetings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from amadeus_desktop.background_generation import BackgroundGenerationRunner
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
from amadeus_desktop.storage_models import ProactiveTrigger as StorageProactiveTrigger
from amadeus_desktop.ui.greeting_bubble import GreetingBubble


class ProactiveDataGateway(Protocol):
    proactive_count_loaded: Any
    proactive_event_displayed: Any
    proactive_event_dismissed: Any
    proactive_greeting_persisted: Any

    def load_proactive_display_count(self, local_date: date) -> bool: ...

    def record_proactive_display(
        self,
        trigger: StorageProactiveTrigger,
        local_date: date,
        *,
        event_id: str | None = None,
        displayed_at: datetime | None = None,
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
        self._tracker = ProactiveOpportunityTracker()
        self._pending_trigger: ProactiveTrigger | None = None
        self._pending_date: date | None = None
        self._pending_displayed_count: int | None = None
        self._active_event_id: str | None = None
        self._active_greeting: str | None = None
        self._active_provider_metadata: tuple[str | None, str | None] = (None, None)
        self._click_pending = False
        self._dismiss_pending = False
        self._ai_lane_owned = False
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
        stopped = self._cancel_ai_lane(wait_ms)
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
        stopped = self._cancel_ai_lane(wait_ms)
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

    def persistence_failed(self, operation: str) -> None:
        if not operation.startswith("proactive"):
            return
        if operation == "proactive_display":
            self._bubble.dismiss()
            self._clear_active()
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
        if (
            bool(settings["ai_greetings_enabled"])
            and self._provider_configured()
            and self._start_ai_generation(trigger, now)
        ):
            return
        self._display_local(trigger, now)

    def _start_ai_generation(self, trigger: ProactiveTrigger, now: datetime) -> bool:
        if not self._acquire_ai_lane():
            return False
        self._ai_lane_owned = True
        request = build_proactive_request(now, trigger)
        started = self._generation_runner.start(
            request,
            on_success=lambda content: self._on_ai_success(trigger, now, content),
            on_failure=lambda _category: self._on_ai_failure(trigger, now),
        )
        if not started:
            self._release_ai()
        return started

    def _on_ai_success(self, trigger: ProactiveTrigger, now: datetime, content: str) -> None:
        self._release_ai()
        if (
            not self._running
            or self._exiting()
            or self._pending_trigger is not trigger
            or self._pending_date != now.date()
            or not bool(self._settings_reader()["proactive"]["ai_greetings_enabled"])
        ):
            self._clear_pending_opportunity()
            return
        try:
            greeting = validate_generated_greeting(content)
        except ValueError:
            self._display_local(trigger, now)
            return
        self._display(trigger, now, greeting, provider_metadata=self._provider_metadata())

    def _on_ai_failure(self, trigger: ProactiveTrigger, now: datetime) -> None:
        self._release_ai()
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
        self._active_greeting = greeting
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
        self._release_ai_lane()

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
