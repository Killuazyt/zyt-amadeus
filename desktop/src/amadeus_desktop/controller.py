"""Application lifecycle, desktop-pet, and attached-chat coordination."""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from amadeus_desktop.chat_geometry import calculate_chat_panel_placement
from amadeus_desktop.chat_models import ConversationState
from amadeus_desktop.chat_provider import (
    ChatProvider,
    OpenAICompatibleChatProvider,
    ProviderConnectionTester,
    ScriptedChatProvider,
    UnconfiguredChatProvider,
)
from amadeus_desktop.conversation import ConversationCoordinator
from amadeus_desktop.credential_store import (
    CredentialStore,
    CredentialStoreError,
    WinCredentialStore,
)
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
from amadeus_desktop.provider_config import ProviderConfig
from amadeus_desktop.settings import SettingsError, SettingsFileSnapshot, SettingsRepository
from amadeus_desktop.single_instance import SingleInstance
from amadeus_desktop.ui.chat_panel import ChatPanel
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.model_settings import ModelSettingsWindow
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
        mock_chat: bool = False,
        allow_saved_provider: bool = True,
        credential_store: CredentialStore | None = None,
        connection_tester: ProviderConnectionTester | None = None,
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
        self._turn_started_at: dict[str, float] = {}
        self._mock_chat = mock_chat
        self._settings_trusted = allow_saved_provider
        self.credential_store = credential_store or WinCredentialStore()
        self.provider_config = ProviderConfig.from_mapping(settings["provider"])
        has_provider_secret = False
        if (
            chat_provider is None
            and not mock_chat
            and allow_saved_provider
            and settings.get("provider_enabled") is True
        ):
            try:
                has_provider_secret = self.credential_store.has_secret()
            except CredentialStoreError as exc:
                logger.warning("Credential store unavailable error_type=%s", type(exc).__name__)
                credential_message = "Windows 凭据管理器不可用，真实对话模型已禁用。"
                status_message = (
                    f"{status_message}\n{credential_message}"
                    if status_message
                    else credential_message
                )

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
        explicit_provider = chat_provider is not None
        if chat_provider is not None:
            selected_provider = chat_provider
        elif mock_chat:
            selected_provider = ScriptedChatProvider()
        elif (
            has_provider_secret
            and allow_saved_provider
            and settings.get("provider_enabled") is True
        ):
            selected_provider = OpenAICompatibleChatProvider(
                self.provider_config,
                self.credential_store,
            )
        else:
            selected_provider = UnconfiguredChatProvider()
        self._active_chat_provider = selected_provider
        provider_ready = (
            has_provider_secret
            and allow_saved_provider
            and settings.get("provider_enabled") is True
        )
        self._chat_available = explicit_provider or mock_chat or provider_ready
        self.conversation = ConversationCoordinator(
            selected_provider,
            first_chunk_timeout_ms=first_chunk_timeout_ms,
            stream_idle_timeout_ms=stream_idle_timeout_ms,
            parent=application,
        )
        self.pet_window.clicked.connect(self.toggle_chat)
        self.chat_panel.send_requested.connect(self._send_chat_message)
        self.chat_panel.stop_requested.connect(self.conversation.stop)
        self.chat_panel.retry_requested.connect(self._retry_chat_turn)
        self.chat_panel.hide_requested.connect(self.hide_chat)
        self.chat_panel.configure_requested.connect(self.show_model_settings)
        self.conversation.turn_added.connect(self.chat_panel.add_turn)
        self.conversation.turn_updated.connect(self.chat_panel.update_turn)
        self.conversation.state_changed.connect(self._on_conversation_state_changed)
        self.conversation.request_finished.connect(self._record_conversation_evidence)

        self.model_settings_window = ModelSettingsWindow(
            self.provider_config,
            has_saved_secret=has_provider_secret,
            credential_reader=self._read_provider_secret,
            tester=connection_tester,
        )
        self.model_settings_window.save_requested.connect(self._save_provider_configuration)

        self.window = ControlWindow(
            tray_available=tray_available,
            status_message=status_message,
        )
        self.window.exit_requested.connect(self.request_exit)
        self.window.model_settings_requested.connect(self.show_model_settings)
        self.instance_guard.activation_requested.connect(self.show_pet)

        self.tray: TrayController | None = None
        if tray_available:
            self.tray = TrayController()
            self.tray.toggle_requested.connect(self.toggle_pet)
            self.tray.show_requested.connect(self.show_pet)
            self.tray.exit_requested.connect(self.request_exit)
            self.tray.model_settings_requested.connect(self.show_model_settings)
            self.pet_window.visibility_changed.connect(self.tray.set_pet_visible)
            self.tray.show()
            self.window.hide()
            self.pet_window.show_without_activate()
        else:
            self.pet_window.show_without_activate()
            self.window.show_and_activate()

        if mock_chat or (explicit_provider and isinstance(selected_provider, ScriptedChatProvider)):
            self.chat_panel.set_provider_mode("mock")
        elif explicit_provider or provider_ready:
            self.chat_panel.set_provider_mode(
                "provider",
                provider_name=f"{self.provider_config.display_name} · {self.provider_config.model}",
            )
        else:
            # Production starts fail-closed. P4 composition replaces the provider only
            # after a credential-backed configuration has been loaded.
            self.chat_panel.set_provider_mode("unconfigured")

        self.application.screenAdded.connect(self._on_screen_added)
        self.application.screenRemoved.connect(self._on_screen_removed)
        for screen in self.application.screens():
            self._connect_screen(screen)

        self.application.aboutToQuit.connect(self._cleanup)

    def show_control_window(self) -> None:
        self.window.show_and_activate()

    def show_model_settings(self) -> None:
        """Open the one reusable P4-only model settings window."""

        self.model_settings_window.show_and_activate()

    def _read_provider_secret(self) -> str | None:
        return self.credential_store.read_secret()

    def _restore_provider_transaction(
        self,
        settings_snapshot: SettingsFileSnapshot,
        previous_secret: str | None,
        *,
        restore_settings: bool,
        restore_credential: bool,
    ) -> tuple[bool, bool]:
        credential_restored = True
        if restore_credential:
            try:
                if previous_secret is None:
                    self.credential_store.delete_secret()
                else:
                    self.credential_store.write_secret(previous_secret)
            except CredentialStoreError:
                credential_restored = False
        settings_restored = True
        if restore_settings:
            if not credential_restored:
                # Keep the durable disabled marker when the old credential could
                # not be restored. Re-enabling old JSON first would pair it with
                # the candidate secret after a crash.
                settings_restored = False
            else:
                try:
                    self.settings_repository.restore_snapshot(settings_snapshot)
                except SettingsError:
                    settings_restored = False
        return settings_restored, credential_restored

    def _persist_provider_disabled(self, settings: dict[str, Any]) -> bool:
        disabled_settings = deepcopy(settings)
        disabled_settings["provider_enabled"] = False
        try:
            self.settings_repository.save(disabled_settings)
        except SettingsError:
            return False
        self.settings.clear()
        self.settings.update(disabled_settings)
        return True

    def _save_provider_configuration(
        self,
        config_object: object,
        secret_object: object,
    ) -> None:
        """Apply WinCred + JSON as a fail-closed transaction after a passed test."""

        if not isinstance(config_object, ProviderConfig):
            self.model_settings_window.apply_save_result(
                success=False,
                message="供应商配置无效，未保存。",
            )
            return
        try:
            config_object = config_object.validated()
        except ValueError:
            self.model_settings_window.apply_save_result(
                success=False,
                message="供应商配置不安全或格式无效，未保存。",
            )
            return
        if self.conversation.state is not ConversationState.IDLE or self.conversation.is_active:
            self.model_settings_window.apply_save_result(
                success=False,
                message="请等当前回复结束后再切换对话模型。",
            )
            return
        secret = secret_object if isinstance(secret_object, str) and secret_object else None
        if secret is None and (
            not self._settings_trusted
            or self.settings.get("provider_enabled") is not True
            or config_object.credential_scope != self.provider_config.credential_scope
        ):
            self.model_settings_window.apply_save_result(
                success=False,
                message="供应商、地址或鉴权方式已变化，请输入对应的新 API 密钥并重新测试。",
            )
            return
        previous_settings = deepcopy(self.settings)
        try:
            settings_snapshot = self.settings_repository.capture_snapshot()
        except SettingsError as exc:
            self.logger.warning("Settings snapshot failed error_type=%s", type(exc).__name__)
            self.model_settings_window.apply_save_result(
                success=False,
                message="设置文件无法安全备份，配置未保存。",
            )
            return
        try:
            previous_secret = self.credential_store.read_secret()
        except CredentialStoreError as exc:
            self.logger.warning("Credential read failed error_type=%s", type(exc).__name__)
            self.model_settings_window.apply_save_result(
                success=False,
                message="Windows 凭据管理器不可用，配置未保存。",
            )
            return
        effective_secret = secret or previous_secret
        if not effective_secret:
            self.model_settings_window.apply_save_result(
                success=False,
                message="请输入 API 密钥并重新测试。",
            )
            return

        candidate_settings = deepcopy(previous_settings)
        candidate_settings["provider"] = config_object.to_mapping()
        candidate_settings["provider_enabled"] = True
        disabled_settings = deepcopy(previous_settings)
        disabled_settings["provider_enabled"] = False
        previous_provider = self._active_chat_provider
        disabled_provider = UnconfiguredChatProvider()
        disabled_marker_saved = False
        credential_write_attempted = False
        provider_switched = False
        try:
            candidate_provider = OpenAICompatibleChatProvider(
                config_object,
                self.credential_store,
            )
            # Commit a durable disabled marker before changing the single WinCred
            # value. A process or machine crash at any later intermediate point
            # therefore restarts fail-closed instead of pairing a new secret with
            # the previous provider URL.
            self.settings_repository.save(disabled_settings)
            disabled_marker_saved = True
            if not self._mock_chat:
                if not self.conversation.set_provider(disabled_provider):
                    raise SettingsError("Provider could not be disabled while saving.")
                provider_switched = True
            if secret is not None:
                credential_write_attempted = True
                self.credential_store.write_secret(secret)
            if not self._mock_chat and not self.conversation.set_provider(candidate_provider):
                raise SettingsError("Provider could not be switched while saving.")
            self.settings_repository.save(candidate_settings)
        except (CredentialStoreError, SettingsError, ValueError) as exc:
            if provider_switched:
                self.conversation.set_provider(disabled_provider)
            settings_restored, credential_restored = self._restore_provider_transaction(
                settings_snapshot,
                previous_secret,
                restore_settings=disabled_marker_saved,
                restore_credential=credential_write_attempted,
            )
            provider_restored = not provider_switched or self.conversation.set_provider(
                previous_provider
            )
            restored = settings_restored and credential_restored and provider_restored
            self.logger.warning(
                "Provider configuration save failed error_type=%s settings_rollback=%s "
                "credential_rollback=%s provider_rollback=%s",
                type(exc).__name__,
                settings_restored,
                credential_restored,
                provider_restored,
            )
            message = (
                "配置保存失败，原配置已恢复。"
                if restored
                else "配置保存与恢复失败，真实对话已禁用。"
            )
            if not restored:
                self.model_settings_window.require_new_secret()
                marker_saved = self._persist_provider_disabled(previous_settings)
                if not marker_saved:
                    disabled_runtime = deepcopy(previous_settings)
                    disabled_runtime["provider_enabled"] = False
                    self.settings.clear()
                    self.settings.update(disabled_runtime)
                self.logger.warning("Provider fail-closed marker saved=%s", marker_saved)
                if not self._mock_chat:
                    self._chat_available = False
                    safe_provider = UnconfiguredChatProvider()
                    self.conversation.set_provider(safe_provider)
                    self._active_chat_provider = safe_provider
                    self.chat_panel.set_provider_mode("unconfigured")
            self.model_settings_window.apply_save_result(success=False, message=message)
            return

        self.settings.clear()
        self.settings.update(candidate_settings)
        self.provider_config = config_object
        if not self._mock_chat:
            self._active_chat_provider = candidate_provider
        self._settings_trusted = True
        self._chat_available = True
        if self._mock_chat:
            self.chat_panel.set_provider_mode("mock")
        else:
            self.chat_panel.set_provider_mode(
                "provider",
                provider_name=f"{config_object.display_name} · {config_object.model}",
            )
        self.model_settings_window.apply_save_result(
            success=True,
            message="对话模型配置已安全保存并启用。",
        )

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
        if not self._chat_available:
            self.chat_panel.set_status("请先配置并测试对话模型。", kind="error")
            return
        started = time.perf_counter()
        turn = self.conversation.send_message(text)
        if turn is None:
            self.chat_panel.set_conversation_state(self.conversation.state)
        else:
            self._turn_started_at[turn.turn_id] = started

    def _retry_chat_turn(self, turn_id: str) -> None:
        if not self._chat_available:
            self.chat_panel.set_status("请先配置并测试对话模型。", kind="error")
            return
        started = time.perf_counter()
        if not self.conversation.retry(turn_id):
            self.chat_panel.set_status("当前无法重试这轮对话。", kind="error")
        else:
            self._turn_started_at[turn_id] = started

    def _record_conversation_evidence(
        self,
        request_id: str,
        turn: object,
        state: object,
    ) -> None:
        """Write privacy-safe local acceptance metadata without conversation text."""

        del request_id
        turn_id = getattr(turn, "turn_id", "unknown")
        started = self._turn_started_at.pop(str(turn_id), None)
        if started is None:
            return
        elapsed_ms = max(0, round((time.perf_counter() - started) * 1_000))
        attempt = getattr(turn, "attempt", 1)
        terminal_reason = getattr(turn, "terminal_reason", None)
        category = getattr(turn, "provider_error_code", None)
        if not category:
            category = getattr(terminal_reason, "value", terminal_reason) or "unknown"
        state_name = getattr(state, "value", state)
        if self.chat_panel.provider_mode == "mock":
            provider_name = "explicit_mock"
            model_name = "scripted"
        else:
            provider_name = self.provider_config.preset.value
            model_name = self.provider_config.model
        self.logger.info(
            "Conversation evidence provider=%s model=%s turn=%s attempt=%s status=%s "
            "latency_ms=%s category=%s",
            provider_name,
            model_name,
            turn_id,
            attempt,
            state_name,
            elapsed_ms,
            category,
        )

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
        self._shutdown_background_tasks()
        self.window.prepare_to_exit()
        self.window.hide()
        self.chat_panel.hide()
        self.model_settings_window.hide()
        self.pet_window.hide()
        if self.tray is not None:
            self.tray.close()
        self.instance_guard.close()
        self.application.quit()

    def _cleanup(self) -> None:
        if not self._exiting:
            self._exiting = True
            if self.tray is not None:
                self.tray.close()
            self._shutdown_background_tasks()
            self.chat_panel.hide()
            self.model_settings_window.hide()
            self.pet_window.hide()
            self.instance_guard.close()

    def _shutdown_background_tasks(self, timeout_ms: int = 2_000) -> bool:
        """Cancel both worker families and share one bounded exit deadline."""

        deadline = time.monotonic() + max(0, timeout_ms) / 1_000
        # Start cancellation for the connection test before waiting on conversation cleanup.
        self.model_settings_window.cancel_test()
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1_000))
        conversation_clean = self.conversation.shutdown(wait_ms=remaining_ms)
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1_000))
        settings_clean = self.model_settings_window.shutdown(wait_ms=remaining_ms)
        if not conversation_clean:
            self.logger.error(
                "Conversation worker did not stop within the shared shutdown deadline"
            )
        if not settings_clean:
            self.logger.error(
                "Provider connection test did not stop within the shared shutdown deadline"
            )
        return conversation_clean and settings_clean
