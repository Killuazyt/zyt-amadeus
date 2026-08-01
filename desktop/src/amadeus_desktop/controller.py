"""Application lifecycle, desktop-pet, and attached-chat coordination."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from amadeus_desktop.chat_geometry import calculate_chat_panel_placement
from amadeus_desktop.chat_models import ConversationState
from amadeus_desktop.chat_provider import ChatProvider, ScriptedChatProvider
from amadeus_desktop.conversation import ConversationCoordinator
from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.pet_assets import PetAssetService
from amadeus_desktop.pet_models import PetPosition
from amadeus_desktop.pet_position import (
    ScreenGeometry,
    capture_position,
    clamp_top_left,
    nearest_screen,
    restore_top_left,
    screen_geometry,
)
from amadeus_desktop.settings import SettingsError, SettingsRepository
from amadeus_desktop.single_instance import SingleInstance
from amadeus_desktop.ui.chat_panel import ChatPanel
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.pet_window import PetWindow
from amadeus_desktop.ui.tray import TrayController


class ApplicationController:
    """Own all desktop UI objects and provide one idempotent exit path."""

    def __init__(
        self,
        application: QApplication,
        instance_guard: SingleInstance,
        logger: logging.Logger,
        *,
        paths: AppPaths,
        settings_repository: SettingsRepository,
        settings: dict[str, Any],
        tray_available: bool | None = None,
        status_message: str | None = None,
        chat_provider: ChatProvider | None = None,
        first_chunk_timeout_ms: int = 15_000,
        stream_idle_timeout_ms: int = 30_000,
    ) -> None:
        self.application = application
        self.instance_guard = instance_guard
        self.logger = logger
        self.paths = paths
        self.settings_repository = settings_repository
        self.settings = settings
        self._exiting = False
        self._restore_scheduled = False
        self._chat_reposition_scheduled = False

        if tray_available is None:
            tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_available = tray_available

        pet_settings = settings["pet"]
        service = PetAssetService(paths.directory(AppDirectory.PETS))
        asset = service.load_active(pet_settings["active_pet_id"])
        if asset.is_fallback:
            logger.warning("Configured pet unavailable; using bundled fallback")
            fallback_message = "当前桌宠资源不可用，已安全回退到内置通用宠物。"
            status_message = (
                f"{status_message}\n{fallback_message}" if status_message else fallback_message
            )
        self.pet_window = PetWindow(asset, scale_percent=pet_settings["scale_percent"])
        self.pet_window.drag_finished.connect(self._on_pet_drag_finished)
        self.pet_window.position_changed.connect(self._schedule_chat_reposition)
        self._restore_pet_position()

        self.chat_panel = ChatPanel()
        self.conversation = ConversationCoordinator(
            chat_provider or ScriptedChatProvider(),
            first_chunk_timeout_ms=first_chunk_timeout_ms,
            stream_idle_timeout_ms=stream_idle_timeout_ms,
            parent=application,
        )
        self.pet_window.clicked.connect(self.toggle_chat)
        self.chat_panel.send_requested.connect(self._send_chat_message)
        self.chat_panel.stop_requested.connect(self.conversation.stop)
        self.chat_panel.retry_requested.connect(self._retry_chat_turn)
        self.chat_panel.hide_requested.connect(self.hide_chat)
        self.conversation.turn_added.connect(self.chat_panel.add_turn)
        self.conversation.turn_updated.connect(self.chat_panel.update_turn)
        self.conversation.state_changed.connect(self._on_conversation_state_changed)

        self.window = ControlWindow(
            tray_available=tray_available,
            status_message=status_message,
        )
        self.window.exit_requested.connect(self.request_exit)
        self.instance_guard.activation_requested.connect(self.show_pet)

        self.tray: TrayController | None = None
        if tray_available:
            self.tray = TrayController()
            self.tray.toggle_requested.connect(self.toggle_pet)
            self.tray.show_requested.connect(self.show_pet)
            self.tray.exit_requested.connect(self.request_exit)
            self.pet_window.visibility_changed.connect(self.tray.set_pet_visible)
            self.tray.show()
            self.window.hide()
            self.pet_window.show_without_activate()
        else:
            self.pet_window.show_without_activate()
            self.window.show_and_activate()

        self.application.screenAdded.connect(self._on_screen_added)
        self.application.screenRemoved.connect(self._on_screen_removed)
        for screen in self.application.screens():
            self._connect_screen(screen)

        self.application.aboutToQuit.connect(self._cleanup)

    def show_control_window(self) -> None:
        self.window.show_and_activate()

    def show_pet(self) -> None:
        self._ensure_pet_visible()
        self.pet_window.show_without_activate()

    def show_chat(self) -> None:
        self.show_pet()
        self._reposition_chat_panel(force=True)
        self.chat_panel.show_and_focus()
        if (
            self.conversation.state is ConversationState.IDLE
            and self.pet_window.animation.state == "idle"
        ):
            self.pet_window.animation.trigger("greeting")

    def hide_chat(self) -> None:
        self.chat_panel.hide()

    def toggle_chat(self) -> None:
        if self.chat_panel.isVisible():
            self.hide_chat()
        else:
            self.show_chat()

    def toggle_pet(self) -> None:
        if self.pet_window.isVisible():
            self.hide_chat()
            self.pet_window.hide()
        else:
            self.show_pet()

    def _send_chat_message(self, text: str) -> None:
        if self.conversation.send_message(text) is None:
            self.chat_panel.set_conversation_state(self.conversation.state)

    def _retry_chat_turn(self, turn_id: str) -> None:
        if not self.conversation.retry(turn_id):
            self.chat_panel.set_status("当前无法重试这轮对话。", kind="error")

    def _on_conversation_state_changed(self, state: ConversationState) -> None:
        self.chat_panel.set_conversation_state(state)
        animation = self.pet_window.animation
        if state is ConversationState.SENDING:
            animation.clear_transient()
            animation.set_activity("responding", False)
            animation.set_activity("waiting", True)
        elif state is ConversationState.WAITING_FIRST_CHUNK:
            animation.set_activity("responding", False)
            animation.set_activity("waiting", True)
        elif state is ConversationState.STREAMING:
            animation.set_activity("waiting", False)
            animation.set_activity("responding", True)
        elif state is ConversationState.COMPLETED:
            self._clear_conversation_activities()
            action = self._success_action()
            if action is not None:
                animation.trigger(action)
        elif state is ConversationState.FAILED:
            self._clear_conversation_activities()
            animation.trigger("error")
        elif state is ConversationState.STOPPED:
            self._clear_conversation_activities()
            animation.clear_transient()
        elif state is ConversationState.IDLE:
            # Preserve a terminal success/error action until it finishes naturally.
            self._clear_conversation_activities()

    def _clear_conversation_activities(self) -> None:
        self.pet_window.animation.set_activity("waiting", False)
        self.pet_window.animation.set_activity("responding", False)

    def _success_action(self) -> str | None:
        animations = self.pet_window.asset.manifest.animations
        if "success" in animations:
            return "success"
        if "jump" in animations:
            return "jump"
        return None

    def _screen_geometries(self) -> list[ScreenGeometry]:
        primary = self.application.primaryScreen()
        return [
            screen_geometry(screen, primary=screen is primary)
            for screen in self.application.screens()
        ]

    def _restore_pet_position(self) -> None:
        position = PetPosition.from_document(self.settings["pet"].get("position"))
        point = restore_top_left(position, self.pet_window.size(), self._screen_geometries())
        self.pet_window.move(point)

    def _schedule_chat_reposition(self, _position: object | None = None) -> None:
        if not self.chat_panel.isVisible() or self._chat_reposition_scheduled:
            return
        self._chat_reposition_scheduled = True
        QTimer.singleShot(0, self._apply_scheduled_chat_reposition)

    def _apply_scheduled_chat_reposition(self) -> None:
        self._chat_reposition_scheduled = False
        self._reposition_chat_panel()

    def _reposition_chat_panel(self, *, force: bool = False) -> None:
        if not force and not self.chat_panel.isVisible():
            return
        work_areas = [screen.availableGeometry() for screen in self.application.screens()]
        if not work_areas:
            return
        probe = calculate_chat_panel_placement(
            self.pet_window.geometry(),
            self.chat_panel.size(),
            work_areas,
        )
        self.chat_panel.resize_for_work_area(probe.work_area)
        placement = calculate_chat_panel_placement(
            self.pet_window.geometry(),
            self.chat_panel.size(),
            work_areas,
        )
        self.chat_panel.move(placement.top_left)

    def _ensure_pet_visible(self) -> None:
        screens = self._screen_geometries()
        if not screens:
            return
        center = self.pet_window.geometry().center()
        target = nearest_screen(center, screens)
        point = clamp_top_left(self.pet_window.pos(), self.pet_window.size(), target.available)
        self.pet_window.move(point)

    def _on_pet_drag_finished(self) -> None:
        screens = self._screen_geometries()
        if not screens:
            return
        target = nearest_screen(self.pet_window.geometry().center(), screens)
        point = clamp_top_left(self.pet_window.pos(), self.pet_window.size(), target.available)
        self.pet_window.move(point)
        position = capture_position(point, self.pet_window.size(), target)
        self.settings["pet"]["position"] = position.to_document()
        try:
            self.settings_repository.save(self.settings)
        except SettingsError as exc:
            self.logger.warning("Pet position could not be saved error_type=%s", type(exc).__name__)

    def _connect_screen(self, screen: QScreen) -> None:
        screen.geometryChanged.connect(self._schedule_restore)
        screen.availableGeometryChanged.connect(self._schedule_restore)
        screen.logicalDotsPerInchChanged.connect(self._schedule_restore)

    def _on_screen_added(self, screen: QScreen) -> None:
        self._connect_screen(screen)
        self._schedule_restore()

    def _on_screen_removed(self, screen: QScreen) -> None:
        del screen
        self._schedule_restore()

    def _schedule_restore(self) -> None:
        if self._restore_scheduled:
            return
        self._restore_scheduled = True
        QTimer.singleShot(0, self._apply_scheduled_restore)

    def _apply_scheduled_restore(self) -> None:
        self._restore_scheduled = False
        self._restore_pet_position()
        self._schedule_chat_reposition()

    def request_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        self.logger.info("Application exit requested")
        if not self.conversation.shutdown():
            self.logger.error("Conversation worker did not stop within the shutdown deadline")
        self.window.prepare_to_exit()
        self.window.hide()
        self.chat_panel.hide()
        self.pet_window.hide()
        if self.tray is not None:
            self.tray.hide()
        self.instance_guard.close()
        self.application.quit()

    def _cleanup(self) -> None:
        if not self._exiting:
            self._exiting = True
            if self.tray is not None:
                self.tray.hide()
            if not self.conversation.shutdown():
                self.logger.error("Conversation worker remained active during cleanup")
            self.chat_panel.hide()
            self.pet_window.hide()
            self.instance_guard.close()
