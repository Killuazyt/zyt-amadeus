"""Application lifecycle, desktop-pet, and attached-chat coordination."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QScreen
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QSystemTrayIcon

from amadeus_desktop import __version__
from amadeus_desktop.autostart import AutostartError, AutostartManager
from amadeus_desktop.background_generation import BackgroundGenerationRunner
from amadeus_desktop.build_info import BuildInfo, load_build_info
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
from amadeus_desktop.data_management import (
    DataManagementError,
    RestoreCleanupError,
    RestoreRollbackError,
    SQLiteExportRepository,
    ValidatedRestorePayload,
    apply_validated_restore,
    create_backup_archive,
    discard_staged_restore,
    export_chat_json,
    export_memory_json,
    plan_factory_reset,
    stage_backup_for_restore,
)
from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.database import SCHEMA_VERSION
from amadeus_desktop.diagnostics import DiagnosticStatusService
from amadeus_desktop.embedding_backend import FastEmbedEmbeddingBackend
from amadeus_desktop.embedding_calibration import calibrate_backend
from amadeus_desktop.embedding_model import resolve_runtime_model_directory
from amadeus_desktop.greetings import GreetingCatalog, load_greeting_catalog
from amadeus_desktop.local_data_service import (
    ConversationSnapshot,
    LocalDataService,
    LocalDataStores,
    MemoryListSnapshot,
    OlderMessagesSnapshot,
    create_local_data_stores,
)
from amadeus_desktop.memory_job_coordinator import (
    JobRepositoryBundle,
    MemoryJobCoordinator,
)
from amadeus_desktop.paths import AppDirectory, AppPaths
from amadeus_desktop.persona_loader import load_persona_knowledge_jsonl
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
from amadeus_desktop.presence import PresenceProbe, WindowsPresenceProbe
from amadeus_desktop.proactive_controller import ProactiveInteractionController
from amadeus_desktop.provider_config import ProviderConfig
from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION,
    SettingsError,
    SettingsFileSnapshot,
    SettingsRepository,
)
from amadeus_desktop.single_instance import SingleInstance
from amadeus_desktop.storage_models import PersonaKnowledgeDraft
from amadeus_desktop.ui.chat_panel import ChatPanel
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.greeting_bubble import GreetingBubble
from amadeus_desktop.ui.model_settings import ModelSettingsWindow
from amadeus_desktop.ui.pet_window import PetWindow
from amadeus_desktop.ui.settings_window import SettingsWindow
from amadeus_desktop.ui.tray import TrayController
from amadeus_desktop.vector_index import VectorIndexCoordinator, VectorIndexRepositories
from amadeus_desktop.vector_runtime import PriorityVectorRuntime


def _local_now() -> datetime:
    """Return timezone-aware local wall time for policy and durable timestamps."""

    return datetime.now().astimezone()


_AUTOSTART_RECONCILE_WARNING = "Windows 开机启动状态已同步，但设置文件未能保存。"
_FOREGROUND_LANE_TIMEOUT_MS = 2_000


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
        background_jobs_enabled: bool | None = None,
        autostart_manager: AutostartManager | None = None,
        presence_probe: PresenceProbe | None = None,
        clock: Callable[[], datetime] = _local_now,
        proactive_startup_delay_ms: int = 5_000,
        proactive_poll_interval_ms: int = 60_000,
        build_info: BuildInfo | None = None,
    ) -> None:
        self.application = application
        self.instance_guard = instance_guard
        self.logger = logger
        self.paths = paths
        self.settings_repository = settings_repository
        self.settings = settings
        self.build_info = build_info or load_build_info()
        self._clock = clock
        self._exiting = False
        self._shutdown_clean: bool | None = None
        self._restore_scheduled = False
        self._chat_reposition_scheduled = False
        self._turn_started_at: dict[str, float] = {}
        self._data_initialized = False
        self._data_writable = False
        self._pending_initial_message: str | None = None
        self._conversation_switch_pending = False
        self._pending_foreground_action: tuple[str, str] | None = None
        self._provider_switch_pending = False
        self._pending_provider_configuration: ProviderConfig | None = None
        self._pending_provider_secret: str | None = None
        self._provider_switch_generation = 0
        self._initial_index_refresh_requested = False
        self._vector_index_available = False
        self._last_safe_error_category = ""
        self._autostart_reconcile_error: str | None = None
        self._staged_restore: ValidatedRestorePayload | None = None
        self._data_change_state = "idle"
        self._services_stopped_for_data_change = False
        self._proactive_pause_persist_pending = False
        self._proactive_pause_retry_timer = QTimer(application)
        self._proactive_pause_retry_timer.setSingleShot(True)
        self._proactive_pause_retry_timer.setInterval(60_000)
        self._proactive_pause_retry_timer.timeout.connect(self._retry_expired_proactive_pause_save)
        self._foreground_lane_timer = QTimer(application)
        self._foreground_lane_timer.setSingleShot(True)
        self._foreground_lane_timer.setInterval(_FOREGROUND_LANE_TIMEOUT_MS)
        self._foreground_lane_timer.timeout.connect(self._on_foreground_lane_timeout)
        self._mock_chat = mock_chat
        self._settings_trusted = allow_saved_provider
        self.credential_store = credential_store or WinCredentialStore()
        self.autostart_manager = autostart_manager or AutostartManager()
        self._reconcile_autostart_setting()
        self._clear_expired_proactive_pause()
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
        always_on_top = bool(settings["general"]["always_on_top"])
        self.pet_asset_service = PetAssetService(paths.directory(AppDirectory.PETS))
        asset = self.pet_asset_service.load_active(pet_settings["active_pet_id"])
        if asset.is_fallback:
            logger.warning("Configured pet unavailable; using bundled fallback")
            candidate = deepcopy(self.settings)
            candidate["pet"]["active_pet_id"] = asset.manifest.pet_id
            self._save_settings(candidate, category="pet_fallback")
            pet_settings = self.settings["pet"]
            fallback_message = "当前桌宠资源不可用，已安全回退到内置默认宠物。"
            status_message = (
                f"{status_message}\n{fallback_message}" if status_message else fallback_message
            )
        self.pet_window = PetWindow(
            asset,
            scale_percent=pet_settings["scale_percent"],
            animation_speed_percent=pet_settings["animation_speed_percent"],
            always_on_top=always_on_top,
        )
        self.pet_window.drag_finished.connect(self._on_pet_drag_finished)
        self.pet_window.position_changed.connect(self._schedule_chat_reposition)
        self.pet_window.visibility_changed.connect(self._on_pet_visibility_changed)
        self._restore_pet_position()

        self.chat_panel = ChatPanel(always_on_top=always_on_top)
        self.greeting_bubble = GreetingBubble(always_on_top=always_on_top)
        self.chat_panel.set_storage_availability(False)
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
        if background_jobs_enabled is None:
            background_jobs_enabled = not explicit_provider
        self._background_jobs_enabled = bool(background_jobs_enabled)
        provider_ready = (
            has_provider_secret
            and allow_saved_provider
            and settings.get("provider_enabled") is True
        )
        self._chat_available = explicit_provider or mock_chat or provider_ready
        self.data_runtime = SerialDataThread(
            lambda: create_local_data_stores(
                self.paths.database_file,
                self.paths.migration_backup_directory,
            ),
            resource_close=lambda stores: stores.close(),
            parent=application,
        )
        self.vector_runtime = PriorityVectorRuntime()
        prepared_model_directory = resolve_runtime_model_directory(
            self.paths.embedding_model_directory
        )
        self.vector_index = VectorIndexCoordinator(
            self.data_runtime,
            self.vector_runtime,
            lambda: FastEmbedEmbeddingBackend(prepared_model_directory),
            lambda resource: VectorIndexRepositories(
                resource.memories,
                resource.personas,
                resource.vectors,
            ),
            backend_calibrator=lambda backend: calibrate_backend(backend).calibration.threshold,
            parent=application,
        )
        self.data_service = LocalDataService(
            self.data_runtime,
            memory_enabled=bool(settings["memory"]["enabled"]),
            follow_user_language=bool(settings["persona"]["follow_user_language"]),
            vector_query=self.vector_index.query,
            parent=application,
        )
        self.memory_maintenance_timer = QTimer(application)
        self.memory_maintenance_timer.setInterval(24 * 60 * 60 * 1_000)
        self.memory_maintenance_timer.timeout.connect(self.data_service.run_memory_maintenance)
        self.background_generation = BackgroundGenerationRunner(
            selected_provider,
            parent=application,
        )
        self.background_generation.idle.connect(self._on_background_provider_idle)
        self.memory_jobs = MemoryJobCoordinator(
            self.data_runtime,
            self.background_generation,
            lambda resource: JobRepositoryBundle(
                resource.conversations,
                resource.jobs,
                resource.memories,
            ),
            memory_enabled=bool(settings["memory"]["enabled"]),
            parent=application,
        )
        self.data_service.jobs_enqueued.connect(self.memory_jobs.poll)
        self.memory_jobs.job_failed.connect(
            lambda _job_id, _kind, _category: self.data_service.refresh_memories()
        )
        self.memory_jobs.job_status_changed.connect(self._on_background_job_status_changed)
        self.memory_jobs.scheduler_error.connect(
            lambda category: self.logger.warning(
                "Background memory scheduler error_type=%s", category
            )
        )
        if mock_chat or isinstance(selected_provider, ScriptedChatProvider):
            self.data_service.set_provider_metadata("explicit_mock", "scripted")
        else:
            self.data_service.set_provider_metadata(
                self.provider_config.preset.value,
                self.provider_config.model,
            )
        self.conversation = ConversationCoordinator(
            selected_provider,
            first_chunk_timeout_ms=first_chunk_timeout_ms,
            stream_idle_timeout_ms=stream_idle_timeout_ms,
            persistence=self.data_service,
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
        self.settings_window = SettingsWindow(self.model_settings_window)
        self.general_page = self.settings_window.general_page
        self.pet_page = self.settings_window.pet_page
        self.persona_page = self.settings_window.persona_page
        self.history_page = self.settings_window.history_page
        self.memory_page = self.settings_window.memory_page
        self.proactive_page = self.settings_window.proactive_page
        self.diagnostics_page = self.settings_window.diagnostics_page
        self._sync_settings_pages()
        self.memory_page.set_memory_enabled(bool(settings["memory"]["enabled"]))
        self._connect_settings_ui()
        self._connect_data_ui()
        self.diagnostic_service = DiagnosticStatusService(
            app_version=self.build_info.version,
            commit_sha=self.build_info.commit_sha,
            build_date_utc=self.build_info.build_date_utc,
            settings_schema=CURRENT_SCHEMA_VERSION,
            sqlite_schema=SCHEMA_VERSION,
            data_root=self.paths.root,
            database_status=self._database_diagnostic_status,
            model_status=lambda: self.vector_index.status.model_status,
            user_index_status=lambda: self.vector_index.status.user_generation_status,
            persona_index_status=lambda: self.vector_index.status.persona_generation_status,
            provider_configured=self._provider_is_configured,
            last_error_category=lambda: self._last_safe_error_category,
        )
        self.proactive_interactions = ProactiveInteractionController(
            data=self.data_service,
            bubble=self.greeting_bubble,
            generation_runner=self.background_generation,
            presence_probe=presence_probe or WindowsPresenceProbe(),
            settings_reader=lambda: self.settings,
            clock=self._clock,
            pet_visible=self.pet_window.isVisible,
            conversation_active=lambda: (
                self._provider_switch_pending
                or self._pending_foreground_action is not None
                or self.conversation.state is not ConversationState.IDLE
                or self.conversation.is_active
            ),
            settings_open=self.settings_window.isVisible,
            data_writable=lambda: self._data_writable,
            exiting=lambda: self._exiting,
            pet_geometry=self.pet_window.geometry,
            work_areas=lambda: [
                screen.availableGeometry() for screen in self.application.screens()
            ],
            provider_configured=self._provider_is_configured,
            provider_metadata=self._proactive_provider_metadata,
            acquire_ai_lane=self._acquire_proactive_ai_lane,
            release_ai_lane=self._release_proactive_ai_lane,
            cancel_ai_lane=self.background_generation.pause,
            open_chat=self.show_chat,
            greeting_catalog_path=(
                self.paths.directory(AppDirectory.PERSONAS) / "kurisu" / "greetings.json"
            ),
            startup_delay_ms=proactive_startup_delay_ms,
            poll_interval_ms=proactive_poll_interval_ms,
            parent=application,
        )
        self.proactive_interactions.status_changed.connect(self._on_proactive_status)
        self.proactive_interactions.local_date_changed.connect(
            self._on_proactive_local_date_changed
        )
        self.proactive_page.set_greeting_source(
            "已加载的本地角色问候文件" if self.proactive_interactions.using_local_catalog else None
        )
        if (
            self.paths.directory(AppDirectory.PERSONAS)
            .joinpath("kurisu", "greetings.json")
            .exists()
            and not self.proactive_interactions.using_local_catalog
        ):
            self.proactive_page.set_status(
                "本地问候文件无效，已使用内置安全短句。",
                error=True,
            )

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
            self.tray.open_chat_requested.connect(self.show_chat)
            self.tray.exit_requested.connect(self.request_exit)
            self.tray.memory_requested.connect(self.show_memory_settings)
            self.tray.settings_requested.connect(self.show_settings)
            self.tray.always_on_top_changed.connect(self._set_always_on_top)
            self.tray.pause_proactive_today_changed.connect(self._set_proactive_paused_today)
            self.tray.launch_at_login_changed.connect(self._set_launch_at_login)
            self.pet_window.visibility_changed.connect(self.tray.set_pet_visible)
            self.tray.apply_state(
                pet_visible=False,
                always_on_top=bool(self.settings["general"]["always_on_top"]),
                proactive_paused_today=self._proactive_is_paused_today(),
                launch_at_login=self._read_autostart_state(),
            )
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
        self.data_service.start()

    @property
    def shutdown_clean(self) -> bool | None:
        return self._shutdown_clean

    def show_control_window(self) -> None:
        self.window.show_and_activate()

    def show_settings(self) -> None:
        self.settings_window.show_and_activate("general")

    def _sync_settings_pages(self) -> None:
        general = self.settings["general"]
        pet = self.settings["pet"]
        persona = self.settings["persona"]
        proactive = self.settings["proactive"]
        self.general_page.apply_settings(
            always_on_top=bool(general["always_on_top"]),
            launch_at_login=bool(general["launch_at_login"]),
        )
        if self._autostart_reconcile_error is not None:
            self.general_page.set_launch_at_login_result(
                bool(general["launch_at_login"]),
                error=self._autostart_reconcile_error,
            )
        self.general_page.set_paths(
            data_path=os.fspath(self.paths.root),
            log_path=os.fspath(self.paths.directory(AppDirectory.LOGS)),
        )
        self.pet_page.set_assets(
            self.pet_asset_service.list_installed(),
            str(pet["active_pet_id"]),
        )
        self.pet_page.apply_settings(
            scale_percent=int(pet["scale_percent"]),
            animation_speed_percent=int(pet["animation_speed_percent"]),
        )
        self.persona_page.apply_settings(follow_user_language=bool(persona["follow_user_language"]))
        self.proactive_page.apply_settings(
            mode=str(proactive["mode"]),
            quiet_start_minute=int(proactive["quiet_start_minute"]),
            quiet_end_minute=int(proactive["quiet_end_minute"]),
            daily_limit=int(proactive["daily_limit"]),
            paused_today=self._proactive_is_paused_today(),
            ai_greetings_enabled=bool(proactive["ai_greetings_enabled"]),
        )
        greeting_file = self.paths.directory(AppDirectory.PERSONAS) / "kurisu" / "greetings.json"
        self.proactive_page.set_greeting_source(
            "已加载的本地角色问候文件" if greeting_file.exists() else None
        )

    def _connect_settings_ui(self) -> None:
        self.general_page.always_on_top_changed.connect(self._set_always_on_top)
        self.general_page.launch_at_login_changed.connect(self._set_launch_at_login)
        self.general_page.open_data_requested.connect(
            lambda: self._open_local_directory(self.paths.root)
        )
        self.general_page.open_logs_requested.connect(
            lambda: self._open_local_directory(self.paths.directory(AppDirectory.LOGS))
        )
        self.general_page.backup_requested.connect(self._request_backup)
        self.general_page.restore_requested.connect(self._request_restore)
        self.general_page.factory_reset_requested.connect(self._request_factory_reset)

        self.pet_page.active_pet_changed.connect(self._set_active_pet)
        self.pet_page.import_requested.connect(self._import_pet_asset)
        self.pet_page.remove_requested.connect(self._remove_pet_asset)
        self.pet_page.scale_changed.connect(self._set_pet_scale)
        self.pet_page.animation_speed_changed.connect(self._set_animation_speed)
        self.pet_page.reset_position_requested.connect(self._reset_pet_position)

        self.persona_page.follow_user_language_changed.connect(self._set_follow_user_language)
        self.persona_page.import_requested.connect(self._import_persona_knowledge)
        self.persona_page.rebuild_index_requested.connect(self._rebuild_persona_index)

        self.proactive_page.mode_changed.connect(
            lambda value: self._set_proactive_value("mode", value)
        )
        self.proactive_page.quiet_hours_changed.connect(self._set_quiet_hours)
        self.proactive_page.daily_limit_changed.connect(
            lambda value: self._set_proactive_value("daily_limit", value)
        )
        self.proactive_page.pause_today_changed.connect(self._set_proactive_paused_today)
        self.proactive_page.ai_greetings_enabled_changed.connect(
            lambda value: self._set_proactive_value("ai_greetings_enabled", value)
        )
        self.proactive_page.greeting_file_requested.connect(self._import_greeting_catalog)

        self.history_page.export_requested.connect(self._request_chat_export)
        self.memory_page.export_requested.connect(self._request_memory_export)
        self.memory_page.backup_requested.connect(self._request_backup)
        self.memory_page.clear_all_requested.connect(self._request_clear_all_memories)
        self.diagnostics_page.refresh_requested.connect(self._refresh_diagnostics)
        self.settings_window.page_changed.connect(self._on_settings_page_changed)

    def _on_settings_page_changed(self, page: str) -> None:
        if page == "history":
            self.data_service.refresh_history()
        elif page == "memory":
            self.data_service.refresh_memories()
        elif page == "pet":
            self.pet_page.set_assets(
                self.pet_asset_service.list_installed(),
                str(self.settings["pet"]["active_pet_id"]),
            )
        elif page == "persona":
            self._refresh_persona_summary()
        elif page == "diagnostics":
            self._refresh_diagnostics()

    def _save_settings(self, candidate: dict[str, Any], *, category: str) -> bool:
        had_autostart_reconcile_error = self._autostart_reconcile_error is not None
        try:
            self.settings_repository.save(candidate)
        except SettingsError as exc:
            self.logger.warning(
                "Settings update failed category=%s error_type=%s",
                category,
                type(exc).__name__,
            )
            self._last_safe_error_category = "storage_error"
            return False
        self.settings.clear()
        self.settings.update(candidate)
        # Every settings save writes a complete snapshot. A newer explicit
        # pause choice therefore supersedes an older expiry retry as well.
        self._proactive_pause_persist_pending = False
        self._proactive_pause_retry_timer.stop()
        if had_autostart_reconcile_error:
            self._autostart_reconcile_error = None
            if hasattr(self, "general_page"):
                self.general_page.set_launch_at_login_result(
                    bool(self.settings["general"]["launch_at_login"])
                )
        return True

    def _reconcile_autostart_setting(self) -> None:
        try:
            actual = self.autostart_manager.is_enabled()
        except AutostartError as exc:
            self.logger.warning("Autostart state unavailable error_type=%s", type(exc).__name__)
            self._last_safe_error_category = "storage_error"
            return
        if actual == bool(self.settings["general"]["launch_at_login"]):
            return
        candidate = deepcopy(self.settings)
        candidate["general"]["launch_at_login"] = actual
        if not self._save_settings(candidate, category="autostart_reconcile"):
            # HKCU is the runtime source of truth. Keep the current process and
            # both UI surfaces aligned even when the JSON snapshot is temporarily
            # unwritable; a later successful settings save will persist this value.
            self.settings.clear()
            self.settings.update(candidate)
            self._autostart_reconcile_error = _AUTOSTART_RECONCILE_WARNING

    def _read_autostart_state(self) -> bool:
        try:
            return self.autostart_manager.is_enabled()
        except AutostartError as exc:
            self.logger.warning("Autostart read failed error_type=%s", type(exc).__name__)
            self._last_safe_error_category = "storage_error"
            return bool(self.settings["general"]["launch_at_login"])

    def _clear_expired_proactive_pause(self) -> None:
        paused = self.settings["proactive"]["paused_local_date"]
        if paused is None or paused == self._clock().date().isoformat():
            return
        candidate = deepcopy(self.settings)
        candidate["proactive"]["paused_local_date"] = None
        if self._save_settings(candidate, category="proactive_pause_expired"):
            return
        # Yesterday's pause is no longer semantically active even when the
        # settings file is temporarily unavailable. Keep runtime/UI truthful
        # and retry the durable snapshot without restoring the stale date.
        self.settings["proactive"]["paused_local_date"] = None
        self._proactive_pause_persist_pending = True
        self._proactive_pause_retry_timer.start()

    def _retry_expired_proactive_pause_save(self) -> None:
        if not self._proactive_pause_persist_pending or self._exiting:
            self._proactive_pause_retry_timer.stop()
            return
        candidate = deepcopy(self.settings)
        candidate["proactive"]["paused_local_date"] = None
        if not self._save_settings(candidate, category="proactive_pause_expired_retry"):
            self._proactive_pause_retry_timer.start()

    def _set_always_on_top(self, enabled: bool) -> None:
        enabled = bool(enabled)
        previous = bool(self.settings["general"]["always_on_top"])
        if enabled != previous:
            candidate = deepcopy(self.settings)
            candidate["general"]["always_on_top"] = enabled
            if not self._save_settings(candidate, category="always_on_top"):
                self.general_page.apply_settings(
                    always_on_top=previous,
                    launch_at_login=bool(self.settings["general"]["launch_at_login"]),
                )
                if self.tray is not None:
                    self.tray.set_always_on_top(previous)
                return
        self.pet_window.set_always_on_top(enabled)
        self.chat_panel.set_always_on_top(enabled)
        self.greeting_bubble.set_always_on_top(enabled)
        self.general_page.apply_settings(
            always_on_top=enabled,
            launch_at_login=bool(self.settings["general"]["launch_at_login"]),
        )
        if self.tray is not None:
            self.tray.set_always_on_top(enabled)

    def _set_launch_at_login(self, enabled: bool) -> None:
        enabled = bool(enabled)
        previous_setting = bool(self.settings["general"]["launch_at_login"])
        previous_actual = self._read_autostart_state()
        self._autostart_reconcile_error = None
        try:
            self.autostart_manager.set_enabled(enabled)
        except AutostartError as exc:
            self.logger.warning("Autostart update failed error_type=%s", type(exc).__name__)
            self._last_safe_error_category = "storage_error"
            self.general_page.set_launch_at_login_result(
                previous_actual,
                error="开机启动设置失败，原状态已恢复。",
            )
            if self.tray is not None:
                self.tray.set_launch_at_login(previous_actual)
            return
        candidate = deepcopy(self.settings)
        candidate["general"]["launch_at_login"] = enabled
        if not self._save_settings(candidate, category="launch_at_login"):
            rollback_ok = True
            try:
                self.autostart_manager.set_enabled(previous_actual)
            except AutostartError:
                rollback_ok = False
            restored = previous_actual if rollback_ok else self._read_autostart_state()
            if not rollback_ok:
                # The registry is the runtime source of truth after a failed
                # rollback. Keep subsequent complete settings snapshots from
                # persisting the stale pre-change value, and retain the same
                # bounded warning used by startup reconciliation until any
                # later settings save persists the observed state.
                observed = deepcopy(self.settings)
                observed["general"]["launch_at_login"] = restored
                self.settings.clear()
                self.settings.update(observed)
                self._autostart_reconcile_error = _AUTOSTART_RECONCILE_WARNING
            self.general_page.set_launch_at_login_result(
                restored,
                error=(
                    "设置保存失败，已恢复原状态。" if rollback_ok else _AUTOSTART_RECONCILE_WARNING
                ),
            )
            if self.tray is not None:
                self.tray.set_launch_at_login(restored)
            return
        self.general_page.set_launch_at_login_result(enabled)
        if self.tray is not None:
            self.tray.set_launch_at_login(enabled)
        if previous_setting != enabled:
            self.logger.info("Autostart setting changed enabled=%s", enabled)

    def _open_local_directory(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("Local directory unavailable error_type=%s", type(exc).__name__)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path)))

    def _set_pet_scale(self, value: int) -> None:
        candidate = deepcopy(self.settings)
        candidate["pet"]["scale_percent"] = int(value)
        if not self._save_settings(candidate, category="pet_scale"):
            self.pet_page.apply_settings(
                scale_percent=int(self.settings["pet"]["scale_percent"]),
                animation_speed_percent=int(self.settings["pet"]["animation_speed_percent"]),
            )
            return
        self.pet_window.set_scale_percent(int(value))
        self._schedule_restore()

    def _set_animation_speed(self, value: int) -> None:
        candidate = deepcopy(self.settings)
        candidate["pet"]["animation_speed_percent"] = int(value)
        if not self._save_settings(candidate, category="animation_speed"):
            self.pet_page.apply_settings(
                scale_percent=int(self.settings["pet"]["scale_percent"]),
                animation_speed_percent=int(self.settings["pet"]["animation_speed_percent"]),
            )
            return
        self.pet_window.set_animation_speed_percent(int(value))

    def _set_active_pet(self, pet_id: str) -> None:
        if pet_id == self.settings["pet"]["active_pet_id"]:
            return
        asset = self.pet_asset_service.load_active(pet_id)
        if asset.is_fallback and pet_id != asset.manifest.pet_id:
            self.pet_page.set_assets(
                self.pet_asset_service.list_installed(),
                str(self.settings["pet"]["active_pet_id"]),
            )
            return
        candidate = deepcopy(self.settings)
        candidate["pet"]["active_pet_id"] = asset.manifest.pet_id
        if not self._save_settings(candidate, category="active_pet"):
            self.pet_page.set_assets(
                self.pet_asset_service.list_installed(),
                str(self.settings["pet"]["active_pet_id"]),
            )
            return
        self.pet_window.replace_asset(asset)
        self.pet_page.set_assets(self.pet_asset_service.list_installed(), asset.manifest.pet_id)
        self._schedule_restore()

    def _import_pet_asset(self, path: str) -> None:
        self.pet_page.setEnabled(False)
        request_id = self.data_runtime.submit(
            lambda _stores: self.pet_asset_service.import_package(Path(path)),
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_pet_asset_imported,
            on_failure=lambda category: self._on_pet_asset_operation_failed(
                "导入失败，请检查资源包格式。", category
            ),
        )
        if request_id is None:
            self._on_pet_asset_operation_failed("本地服务尚未就绪。", "DataThreadStopped")

    def _on_pet_asset_imported(self, asset: object) -> None:
        self.pet_page.setEnabled(True)
        pet_id = getattr(getattr(asset, "manifest", None), "pet_id", None)
        if isinstance(pet_id, str):
            self.pet_page.set_assets(self.pet_asset_service.list_installed(), pet_id)
            self._set_active_pet(pet_id)

    def _remove_pet_asset(self, pet_id: str) -> None:
        if pet_id == "builtin-amadeus":
            return
        answer = QMessageBox.question(
            self.settings_window,
            "移除桌宠资源",
            "确定移除这个本地桌宠资源包吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.pet_page.set_assets(
                self.pet_asset_service.list_installed(),
                str(self.settings["pet"]["active_pet_id"]),
            )
            return
        if self.settings["pet"]["active_pet_id"] == pet_id:
            self._set_active_pet("builtin-amadeus")
            if self.settings["pet"]["active_pet_id"] == pet_id:
                return
        self.pet_page.setEnabled(False)
        request_id = self.data_runtime.submit(
            lambda _stores: self.pet_asset_service.remove(pet_id),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda _removed: self._on_pet_asset_removed(),
            on_failure=lambda category: self._on_pet_asset_operation_failed(
                "资源包移除失败。", category
            ),
        )
        if request_id is None:
            self._on_pet_asset_operation_failed("本地服务尚未就绪。", "DataThreadStopped")

    def _on_pet_asset_removed(self) -> None:
        self.pet_page.setEnabled(True)
        self.pet_page.set_assets(
            self.pet_asset_service.list_installed(),
            str(self.settings["pet"]["active_pet_id"]),
        )

    def _on_pet_asset_operation_failed(self, message: str, category: str) -> None:
        self.pet_page.setEnabled(True)
        self.logger.warning("Pet asset operation failed error_type=%s", category)
        QMessageBox.warning(self.settings_window, "桌宠资源", message)
        self.pet_page.set_assets(
            self.pet_asset_service.list_installed(),
            str(self.settings["pet"]["active_pet_id"]),
        )

    def _reset_pet_position(self) -> None:
        candidate = deepcopy(self.settings)
        candidate["pet"]["position"] = None
        if self._save_settings(candidate, category="pet_position_reset"):
            self._restore_pet_position()
            self._schedule_chat_reposition()

    def _set_follow_user_language(self, enabled: bool) -> None:
        enabled = bool(enabled)
        candidate = deepcopy(self.settings)
        candidate["persona"]["follow_user_language"] = enabled
        if not self._save_settings(candidate, category="persona_language"):
            self.persona_page.apply_settings(
                follow_user_language=bool(self.settings["persona"]["follow_user_language"])
            )
            return
        setter = getattr(self.data_service, "set_follow_user_language", None)
        if callable(setter):
            setter(enabled)

    def _set_quiet_hours(self, start: int, end: int) -> None:
        candidate = deepcopy(self.settings)
        candidate["proactive"]["quiet_start_minute"] = int(start)
        candidate["proactive"]["quiet_end_minute"] = int(end)
        if not self._save_settings(candidate, category="proactive_quiet_hours"):
            self._sync_settings_pages()

    def _set_proactive_value(self, key: str, value: object) -> None:
        candidate = deepcopy(self.settings)
        candidate["proactive"][key] = value
        if not self._save_settings(candidate, category=f"proactive_{key}"):
            self._sync_settings_pages()
            return
        if (key == "ai_greetings_enabled" and value is False) or key == "mode":
            self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        if key == "mode" and value == "off":
            self.proactive_interactions.dismiss_current()

    def _proactive_is_paused_today(self) -> bool:
        return self.settings["proactive"]["paused_local_date"] == self._clock().date().isoformat()

    def _set_proactive_paused_today(self, paused: bool) -> None:
        candidate = deepcopy(self.settings)
        candidate["proactive"]["paused_local_date"] = (
            self._clock().date().isoformat() if paused else None
        )
        if not self._save_settings(candidate, category="proactive_pause_today"):
            paused = self._proactive_is_paused_today()
        self.proactive_page.set_paused_local_date(
            self.settings["proactive"]["paused_local_date"],
            today=self._clock().date(),
        )
        if self.tray is not None:
            self.tray.set_proactive_paused_today(bool(paused))
        if paused:
            self.proactive_interactions.cancel_ai_generation(wait_ms=0)
            self.proactive_interactions.dismiss_current()

    def _on_proactive_local_date_changed(self, _local_date_iso: str) -> None:
        """Expire yesterday's pause and keep both control surfaces truthful."""

        self._clear_expired_proactive_pause()
        self.proactive_page.set_paused_local_date(
            self.settings["proactive"]["paused_local_date"],
            today=self._clock().date(),
        )
        if self.tray is not None:
            self.tray.set_proactive_paused_today(self._proactive_is_paused_today())

    def _provider_is_configured(self) -> bool:
        return bool(
            not self._mock_chat
            and self._chat_available
            and self.settings.get("provider_enabled") is True
        )

    def _proactive_provider_metadata(self) -> tuple[str | None, str | None]:
        if not self._provider_is_configured():
            return None, None
        return self.provider_config.preset.value, self.provider_config.model

    def _acquire_proactive_ai_lane(self) -> bool:
        if (
            self._exiting
            or self._provider_switch_pending
            or self.conversation.state is not ConversationState.IDLE
            or self.memory_jobs.has_active_job
            or self.background_generation.is_running
        ):
            return False
        if not self.memory_jobs.pause(wait_ms=0):
            self.memory_jobs.resume()
            return False
        self.background_generation.resume()
        return True

    def _release_proactive_ai_lane(self) -> None:
        if not self._exiting and not self._provider_switch_pending:
            self.memory_jobs.resume()

    def _database_diagnostic_status(self) -> str:
        if not self._data_initialized:
            return "unknown"
        return "read_write" if self._data_writable else "read_only"

    def _refresh_diagnostics(self) -> None:
        if self._exiting:
            return
        self.diagnostics_page.set_diagnostics(self.diagnostic_service.snapshot())

    def _on_proactive_status(self, category: str) -> None:
        safe_messages = {
            "allowed": "主动互动已就绪。",
            "local_greeting_invalid": "本地问候文件无效，已使用内置安全短句。",
            "storage_error": "主动互动账本暂时不可用。",
            "storage_unavailable": "主动互动账本尚未就绪。",
            "display_failed": "问候气泡未能显示。",
        }
        if category in {"storage_error", "storage_unavailable"}:
            self._last_safe_error_category = "storage_error"
        self.proactive_page.set_status(safe_messages.get(category, ""))

    def _refresh_persona_summary(self) -> None:
        request_id = self.data_runtime.submit(
            lambda stores: len(stores.personas.list_active_documents("kurisu")),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda count: self.persona_page.set_summary(
                name="克里斯蒂娜",
                source=(
                    "已导入的本地 JSONL"
                    if self.paths.persona_knowledge_file.exists()
                    else "公开安全的内置核心设定"
                ),
                knowledge_count=int(count),
            ),
            on_failure=lambda category: self._on_persona_operation_failed(
                "角色知识状态读取失败。", category
            ),
        )
        if request_id is None:
            self._on_persona_operation_failed("本地服务尚未就绪。", "DataThreadStopped")

    def _import_persona_knowledge(self, path: str) -> None:
        self.persona_page.setEnabled(False)
        request_id = self.data_runtime.submit(
            lambda stores: _install_persona_knowledge(
                stores,
                Path(path),
                self.paths.persona_knowledge_file,
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_persona_imported,
            on_failure=lambda category: self._on_persona_operation_failed(
                "角色知识导入失败，请检查 JSONL 格式。", category
            ),
        )
        if request_id is None:
            self._on_persona_operation_failed("本地服务尚未就绪。", "DataThreadStopped")

    def _on_persona_imported(self, count: object) -> None:
        self.persona_page.setEnabled(True)
        self.persona_page.set_summary(
            name="克里斯蒂娜",
            source="已导入的本地 JSONL",
            knowledge_count=int(count),
        )
        self.persona_page.set_status("角色知识已导入，正在单独重建角色索引。")
        self._rebuild_persona_index()

    def _on_persona_operation_failed(self, message: str, category: str) -> None:
        self.persona_page.setEnabled(True)
        self.logger.warning("Persona operation failed error_type=%s", category)
        self.persona_page.set_status(message, error=True)

    def _rebuild_persona_index(self) -> None:
        rebuild = getattr(self.vector_index, "rebuild_persona", None)
        started = bool(rebuild()) if callable(rebuild) else self.vector_index.rebuild()
        if started:
            self.persona_page.set_status("角色索引正在后台重建。")
        else:
            self.persona_page.set_status(
                "角色索引当前无法重建；对话会继续使用 FTS5。",
                error=True,
            )

    def _import_greeting_catalog(self, path: str) -> None:
        target = self.paths.directory(AppDirectory.PERSONAS) / "kurisu" / "greetings.json"
        self.proactive_page.setEnabled(False)
        request_id = self.data_runtime.submit(
            lambda _stores: _install_greeting_catalog(Path(path), target),
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_greeting_catalog_imported,
            on_failure=lambda category: self._on_greeting_catalog_failed(category),
        )
        if request_id is None:
            self._on_greeting_catalog_failed("DataThreadStopped")

    def _on_greeting_catalog_imported(self, catalog: object) -> None:
        from amadeus_desktop.greetings import GreetingCatalog

        self.proactive_page.setEnabled(True)
        if not isinstance(catalog, GreetingCatalog):
            self._on_greeting_catalog_failed("InvalidGreetingCatalog")
            return
        self.proactive_interactions.set_catalog(catalog)
        self.proactive_page.set_greeting_source("已加载的本地角色问候文件")
        self.proactive_page.set_status("本地问候文件已验证并启用。")

    def _on_greeting_catalog_failed(self, category: str) -> None:
        self.proactive_page.setEnabled(True)
        self.logger.warning("Greeting catalog import failed error_type=%s", category)
        self.proactive_page.set_status("本地问候文件无效，继续使用内置安全短句。", error=True)

    def _request_chat_export(self) -> None:
        if self._data_change_state != "idle":
            return
        destination, _filter = QFileDialog.getSaveFileName(
            self.settings_window,
            "导出聊天历史",
            "amadeus-chat-export.json",
            "JSON (*.json)",
        )
        if not destination:
            return
        request_id = self.data_runtime.submit(
            lambda stores: export_chat_json(
                destination,
                SQLiteExportRepository(stores.database.connection).load_chat_bundle,
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda path: self.history_page.set_status(
                f"聊天历史已导出到 {Path(path).name}。"
            ),
            on_failure=lambda category: self._on_export_failed("history", category),
        )
        if request_id is None:
            self._on_export_failed("history", "DataThreadStopped")

    def _request_memory_export(self) -> None:
        if self._data_change_state != "idle":
            return
        destination, _filter = QFileDialog.getSaveFileName(
            self.settings_window,
            "导出长期记忆",
            "amadeus-memory-export.json",
            "JSON (*.json)",
        )
        if not destination:
            return
        request_id = self.data_runtime.submit(
            lambda stores: export_memory_json(
                destination,
                SQLiteExportRepository(stores.database.connection).load_memory_bundle,
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=lambda path: self.memory_page.set_status(
                f"长期记忆已导出到 {Path(path).name}。"
            ),
            on_failure=lambda category: self._on_export_failed("memory", category),
        )
        if request_id is None:
            self._on_export_failed("memory", "DataThreadStopped")

    def _on_export_failed(self, page: str, category: str) -> None:
        self.logger.warning("JSON export failed page=%s error_type=%s", page, category)
        self._last_safe_error_category = "storage_error"
        if page == "memory":
            self.memory_page.set_status("记忆导出失败。", error=True)
        else:
            self.history_page.set_status("聊天导出失败。", error=True)

    def _request_backup(self) -> None:
        if self._data_change_state != "idle":
            return
        destination, _filter = QFileDialog.getSaveFileName(
            self.settings_window,
            "创建 Amadeus 备份",
            "amadeus-backup.amadeus-backup",
            "Amadeus Backup (*.amadeus-backup *.zip)",
        )
        if not destination:
            return
        self._start_data_operation("backup")
        settings_snapshot = deepcopy(self.settings)
        request_id = self.data_runtime.submit(
            lambda stores: create_backup_archive(
                destination,
                database_backup=stores.database.create_backup,
                settings_snapshot=settings_snapshot,
                app_version=__version__,
            ),
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_backup_created,
            on_failure=lambda category: self._on_backup_failed(category),
        )
        if request_id is None:
            self._on_backup_failed("DataThreadStopped")

    def _on_backup_created(self, path: object) -> None:
        if self._data_change_state == "backup":
            self._finish_data_operation()
        filename = Path(path).name
        self.memory_page.set_status(f"一致性备份已创建：{filename}。")
        QMessageBox.information(self.settings_window, "备份完成", "一致性备份已安全创建。")

    def _on_backup_failed(self, category: str) -> None:
        if self._data_change_state == "backup":
            self._finish_data_operation()
        self.logger.warning("Backup failed error_type=%s", category)
        self._last_safe_error_category = "storage_error"
        self.memory_page.set_status("备份创建失败。", error=True)
        QMessageBox.warning(self.settings_window, "备份失败", "无法安全创建备份。")

    def _request_restore(self) -> None:
        if not self._can_start_data_change():
            return
        archive, _filter = QFileDialog.getOpenFileName(
            self.settings_window,
            "选择 Amadeus 备份",
            "",
            "Amadeus Backup (*.amadeus-backup *.zip)",
        )
        if not archive:
            return
        self._start_data_operation("restore_validation")
        staging_parent = self.paths.ensure(AppDirectory.BACKUPS) / "restore-staging"
        request_id = self.data_runtime.submit(
            lambda _stores: stage_backup_for_restore(archive, staging_parent),
            priority=DataPriority.INTERACTIVE,
            on_success=self._on_restore_validated,
            on_failure=lambda category: self._on_restore_validation_failed(category),
        )
        if request_id is None:
            self._on_restore_validation_failed("DataThreadStopped")

    def _on_restore_validated(self, payload: object) -> None:
        if self._data_change_state != "restore_validation":
            if isinstance(payload, ValidatedRestorePayload):
                with suppress(DataManagementError):
                    discard_staged_restore(payload)
            return
        if not isinstance(payload, ValidatedRestorePayload):
            self._on_restore_validation_failed("InvalidRestorePayload")
            return
        answer = QMessageBox.question(
            self.settings_window,
            "确认恢复",
            "备份已通过格式、校验和与数据库完整性检查。继续后应用会创建恢复前备份、"
            "替换本地数据并退出；请随后手动重新启动。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            with suppress(DataManagementError):
                discard_staged_restore(payload)
            self._finish_data_operation()
            return
        self._staged_restore = payload
        self._data_change_state = "restore_prebackup"
        if not self._begin_data_change():
            self._abort_restore_before_replace("BackgroundTasksBusy")
            return
        backup_directory = self.paths.ensure(AppDirectory.BACKUPS)
        timestamp = self._clock().strftime("%Y%m%d-%H%M%S")
        destination = backup_directory / f"pre-restore-{timestamp}.amadeus-backup"
        settings_snapshot = deepcopy(self.settings)
        request_id = self.data_runtime.submit(
            lambda stores: create_backup_archive(
                destination,
                database_backup=stores.database.create_backup,
                settings_snapshot=settings_snapshot,
                app_version=__version__,
            ),
            priority=DataPriority.FOREGROUND,
            on_success=lambda _path: self._apply_staged_restore(),
            on_failure=lambda category: self._abort_restore_before_replace(category),
        )
        if request_id is None:
            self._abort_restore_before_replace("DataThreadStopped")

    def _on_restore_validation_failed(self, category: str) -> None:
        if self._data_change_state == "restore_validation":
            self._finish_data_operation()
        self.logger.warning("Restore validation failed error_type=%s", category)
        self._last_safe_error_category = "storage_error"
        QMessageBox.warning(
            self.settings_window,
            "备份无效",
            "该备份未通过格式、校验和或数据库完整性检查，未更改任何本地数据。",
        )

    def _apply_staged_restore(self) -> None:
        payload = self._staged_restore
        if payload is None:
            self._abort_restore_before_replace("MissingRestorePayload")
            return
        self._data_change_state = "restore_applying"
        self._services_stopped_for_data_change = True
        clean = self._shutdown_background_tasks(timeout_ms=10_000)
        success = False
        cleanup_warning = False
        rollback_uncertain = False
        try:
            if not clean:
                raise DataManagementError("services did not stop safely")
            apply_validated_restore(payload, self.paths)
            success = True
        except RestoreCleanupError as exc:
            success = True
            cleanup_warning = True
            self._last_safe_error_category = "storage_error"
            self.logger.warning(
                "Restore committed with cleanup warning error_type=%s", type(exc).__name__
            )
        except RestoreRollbackError as exc:
            rollback_uncertain = True
            self._last_safe_error_category = "storage_error"
            self.logger.error("Restore rollback failed error_type=%s", type(exc).__name__)
        except DataManagementError as exc:
            self._last_safe_error_category = "storage_error"
            self.logger.error("Restore apply failed error_type=%s", type(exc).__name__)
        finally:
            try:
                discard_staged_restore(payload)
            except DataManagementError:
                cleanup_warning = True
            self._staged_restore = None
        if rollback_uncertain:
            title = "恢复与回滚未完成"
            message = (
                "文件系统阻止了完整回滚。恢复前备份已保留；Amadeus 将安全退出，"
                "请在重新启动前使用该备份恢复。"
            )
        elif success and cleanup_warning:
            title = "恢复完成（有清理警告）"
            message = (
                "本地数据已恢复，但部分临时文件未能清理。Amadeus 将退出；"
                "主数据可在重新启动后继续使用。"
            )
        elif success:
            title = "恢复完成"
            message = "本地数据已恢复。Amadeus 将退出，请手动重新启动。"
        else:
            title = "恢复失败"
            message = "恢复未完成，原数据库与设置已回滚。Amadeus 将安全退出。"
        QMessageBox.information(
            None,
            title,
            message,
        )
        self._exit_after_data_change()

    def _abort_restore_before_replace(self, category: str) -> None:
        self.logger.warning("Pre-restore backup failed error_type=%s", category)
        payload, self._staged_restore = self._staged_restore, None
        if payload is not None:
            with suppress(DataManagementError):
                discard_staged_restore(payload)
        self._resume_after_aborted_data_change()
        self._finish_data_operation()
        QMessageBox.warning(
            self.settings_window,
            "恢复已取消",
            "恢复前备份未能安全创建，现有数据未更改。",
        )

    def _can_start_data_change(self) -> bool:
        if (
            self._exiting
            or self._data_change_state != "idle"
            or self._staged_restore is not None
            or self._services_stopped_for_data_change
            or self._conversation_switch_pending
            or self._pending_foreground_action is not None
            or self._provider_switch_pending
            or self.conversation.state is not ConversationState.IDLE
            or self.conversation.is_active
        ):
            QMessageBox.information(
                self.settings_window,
                "请稍后",
                "请等待当前对话或模型切换结束后再执行此操作。",
            )
            return False
        return True

    def _start_data_operation(self, state: str) -> None:
        if self._data_change_state != "idle":
            raise RuntimeError("an exclusive data operation is already active")
        self._data_change_state = state
        self._set_data_management_controls_enabled(False)

    def _finish_data_operation(self) -> None:
        self._data_change_state = "idle"
        self._services_stopped_for_data_change = False
        self._set_data_management_controls_enabled(True)

    def _set_data_management_controls_enabled(self, enabled: bool) -> None:
        self.general_page.backup_button.setEnabled(enabled)
        self.general_page.restore_button.setEnabled(enabled)
        self.general_page.factory_reset_button.setEnabled(enabled)
        self.memory_page.backup_button.setEnabled(enabled)
        self.memory_page.clear_all_button.setEnabled(enabled and self._data_writable)

    def _begin_data_change(self) -> bool:
        if (
            self._provider_switch_pending
            or self.conversation.state is not ConversationState.IDLE
            or self.conversation.is_active
        ):
            return False
        self._conversation_switch_pending = True
        self.chat_panel.set_storage_availability(False, read_only=True)
        self.memory_maintenance_timer.stop()
        proactive_clean = self.proactive_interactions.stop(wait_ms=2_000)
        memory_clean = self.memory_jobs.pause(wait_ms=2_000)
        if not proactive_clean or not memory_clean:
            self._resume_after_aborted_data_change()
            return False
        return True

    def _resume_after_aborted_data_change(self) -> None:
        self._conversation_switch_pending = False
        self.chat_panel.set_storage_availability(
            self._data_writable,
            read_only=not self._data_writable,
        )
        if self._data_writable:
            self.memory_maintenance_timer.start()
            self.memory_jobs.resume()
            self.proactive_interactions.start()

    def _request_factory_reset(self) -> None:
        if not self._can_start_data_change():
            return
        first = QMessageBox.warning(
            self.settings_window,
            "清除全部本地数据",
            "这会删除聊天、记忆、设置、日志、应用内备份、模型缓存、导入宠物、"
            "角色资料、模型凭据和开机启动项。应用目录外的导出文件不会删除。继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return
        second = QMessageBox.warning(
            self.settings_window,
            "再次确认恢复出厂",
            "最后确认：此操作不可撤销，完成后 Amadeus 会立即退出。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if second != QMessageBox.StandardButton.Yes:
            return
        try:
            reset_plan = plan_factory_reset(self.paths)
        except DataManagementError as exc:
            self.logger.error("Factory reset plan rejected error_type=%s", type(exc).__name__)
            QMessageBox.warning(self.settings_window, "无法清除", "本地数据路径未通过安全检查。")
            return
        self._start_data_operation("factory_reset")
        if not self._begin_data_change():
            self._finish_data_operation()
            QMessageBox.warning(
                self.settings_window,
                "清除已取消",
                "后台任务未能安全暂停，本地数据未更改。",
            )
            return
        self._services_stopped_for_data_change = True
        clean = self._shutdown_background_tasks(timeout_ms=10_000)
        if not clean:
            QMessageBox.warning(
                None,
                "清除已取消",
                "后台服务未能安全停止，本地数据、凭据和开机启动项均未删除。Amadeus 将安全退出。",
            )
            self._exit_after_data_change()
            return
        failures = False
        try:
            verified_plan = plan_factory_reset(self.paths)
            if verified_plan != reset_plan:
                raise DataManagementError("factory reset paths changed after validation")
        except DataManagementError as exc:
            self.logger.error("Factory reset revalidation failed error_type=%s", type(exc).__name__)
            QMessageBox.warning(
                None,
                "清除已取消",
                "本地数据路径在停止服务后未通过复核，任何本地数据均未删除。Amadeus 将安全退出。",
            )
            self._exit_after_data_change()
            return
        try:
            self.autostart_manager.set_enabled(False)
        except AutostartError:
            failures = True
        try:
            self.credential_store.delete_secret()
        except CredentialStoreError:
            failures = True
        logging.shutdown()
        for target in reset_plan.targets:
            try:
                if target.exists():
                    current_plan = plan_factory_reset(self.paths)
                    if current_plan != reset_plan or not target.is_dir() or target.is_symlink():
                        raise OSError
                    shutil.rmtree(target)
            except OSError:
                failures = True
        QMessageBox.information(
            None,
            "本地数据已清除" if not failures else "清除未完全成功",
            (
                "本地数据已清除，Amadeus 将退出。"
                if not failures
                else "部分本地数据未能清除，Amadeus 将退出；重新启动前请检查数据目录。"
            ),
        )
        self._exit_after_data_change()

    def _request_clear_all_memories(self) -> None:
        if self._data_change_state != "idle":
            return
        self._start_data_operation("clear_memories")
        if not self.memory_jobs.pause(wait_ms=2_000):
            self.memory_jobs.resume()
            self._finish_data_operation()
            self.memory_page.set_status(
                "后台记忆任务尚未安全停止，未清空记忆。",
                error=True,
            )
            return
        clear = getattr(self.data_service, "clear_all_memories", None)
        if not callable(clear) or not clear():
            self.memory_jobs.resume()
            self._finish_data_operation()
            self.memory_page.set_status("清空记忆请求未能提交。", error=True)

    def _on_memories_cleared(self, count: int) -> None:
        self.memory_jobs.resume()
        if self._data_change_state == "clear_memories":
            self._finish_data_operation()
        self.memory_page.set_status(f"已清空 {max(0, int(count))} 条长期记忆。")

    def _exit_after_data_change(self) -> None:
        self._exiting = True
        self._data_change_state = "exiting"
        self.window.prepare_to_exit()
        self.window.hide()
        self.chat_panel.hide()
        self.settings_window.hide()
        self.greeting_bubble.hide()
        self.pet_window.hide()
        if self.tray is not None:
            self.tray.close()
        self.instance_guard.close()
        self.application.quit()

    def show_model_settings(self) -> None:
        """Deep-link to the model page in the reusable P5 settings shell."""

        self.settings_window.show_and_activate("model")

    def show_history_settings(self) -> None:
        self.settings_window.show_and_activate("history")
        self.data_service.refresh_history()

    def show_memory_settings(self) -> None:
        self.settings_window.show_and_activate("memory")
        self.data_service.refresh_memories()

    def _connect_data_ui(self) -> None:
        self.data_service.startup_loaded.connect(self._on_data_startup_loaded)
        self.data_service.startup_failed.connect(self._on_data_startup_failed)
        self.data_service.write_availability_changed.connect(
            self._on_data_write_availability_changed
        )
        self.data_service.conversation_loaded.connect(self._on_persisted_conversation_loaded)
        self.data_service.older_messages_loaded.connect(self._on_older_messages_loaded)
        self.data_service.history_loaded.connect(self.history_page.set_conversations)
        self.data_service.memories_loaded.connect(self._on_memories_loaded)
        self.data_service.memories_cleared.connect(self._on_memories_cleared)
        self.data_service.memory_sources_loaded.connect(self.memory_page.set_sources)
        self.data_service.source_context_loaded.connect(self._on_source_context_loaded)
        self.data_service.operation_failed.connect(self._on_data_operation_failed)
        self.data_service.index_rebuild_requested.connect(self._request_incremental_index_refresh)
        self.vector_index.status_changed.connect(self.memory_page.set_retrieval_status)
        self.vector_index.status_changed.connect(self._queue_vector_index_status)
        self.vector_index.status_changed.connect(self._on_vector_status_for_p6)

        self.chat_panel.load_older_requested.connect(self.data_service.load_older_messages)
        self.history_page.refresh_requested.connect(self.data_service.refresh_history)
        self.history_page.conversation_selected.connect(self._request_conversation_switch)
        self.history_page.new_conversation_requested.connect(self._request_new_conversation)
        self.history_page.rename_conversation_requested.connect(
            self.data_service.rename_conversation
        )
        self.history_page.delete_conversation_requested.connect(self._request_delete_conversation)
        self.history_page.clear_history_requested.connect(self._request_clear_history)
        self.history_page.load_older_messages_requested.connect(
            self._request_older_history_messages
        )

        self.memory_page.refresh_requested.connect(self.data_service.refresh_memories)
        self.memory_page.search_requested.connect(self._request_memory_search)
        self.memory_page.memory_selected.connect(self.data_service.load_memory_sources)
        self.memory_page.enabled_changed.connect(self._set_memory_enabled)
        self.memory_page.edit_requested.connect(self.data_service.edit_memory)
        self.memory_page.pin_requested.connect(self.data_service.set_memory_pinned)
        self.memory_page.archive_requested.connect(self.data_service.archive_memory)
        self.memory_page.restore_requested.connect(self.data_service.restore_memory)
        self.memory_page.delete_requested.connect(self.data_service.delete_memory)
        self.memory_page.source_requested.connect(self.data_service.load_source_context)
        self.memory_page.retry_task_requested.connect(self.data_service.retry_failed_job)
        self.memory_page.verify_model_requested.connect(self._verify_local_embedding_model)
        self.memory_page.rebuild_index_requested.connect(self._request_index_rebuild)

    def _on_data_startup_loaded(self, snapshot_object: object) -> None:
        if not isinstance(snapshot_object, ConversationSnapshot):
            self._on_data_startup_failed("InvalidStartupSnapshot")
            return
        self._data_initialized = True
        self._data_writable = not snapshot_object.read_only
        self._apply_conversation_snapshot(snapshot_object)
        self.chat_panel.set_storage_availability(
            self._data_writable,
            read_only=not self._data_writable,
        )
        self.memory_page.set_memory_enabled(self.data_service.memory_enabled)
        self.data_service.refresh_memories()
        self._refresh_persona_summary()
        self._refresh_diagnostics()
        if self._background_jobs_enabled and self._data_writable:
            self.memory_jobs.start()
        if self._data_writable:
            self.vector_index.start()
            self.data_service.run_memory_maintenance()
            self.memory_maintenance_timer.start()
            self.proactive_interactions.start()
        if snapshot_object.read_only:
            self.logger.warning(
                "Local database opened read-only migration_error=%s",
                snapshot_object.migration_error_category or "unknown",
            )
        pending = self._pending_initial_message
        self._pending_initial_message = None
        if pending is not None and self._data_writable:
            self._send_chat_message(pending)

    def _on_data_startup_failed(self, category: str) -> None:
        self._data_initialized = True
        self._data_writable = False
        self._pending_initial_message = None
        self.chat_panel.set_storage_availability(False, read_only=True)
        self._last_safe_error_category = "database_unavailable"
        self._refresh_diagnostics()
        self.logger.warning("Local database unavailable error_type=%s", category)

    def _on_data_write_availability_changed(self, writable: bool) -> None:
        self._data_writable = writable
        if not writable and self.memory_jobs.is_accepting:
            # Fail closed without waiting on the Qt thread.  A migration or
            # persistence failure must not leave the scheduler polling writes
            # against a query-only/unavailable database.
            self.memory_jobs.shutdown(wait_ms=0)
        if self._data_initialized:
            self.chat_panel.set_storage_availability(writable, read_only=not writable)
        if self._data_change_state == "idle":
            self.memory_page.clear_all_button.setEnabled(writable)

    def _apply_conversation_snapshot(self, snapshot: ConversationSnapshot) -> bool:
        if not self.conversation.restore_turns(snapshot.turns):
            self.chat_panel.set_status("当前回复尚未结束，暂时不能切换会话。", kind="error")
            return False
        self.chat_panel.clear_messages()
        presentation = snapshot.presentation_entries or snapshot.turns
        for entry in presentation:
            self.chat_panel.add_turn(entry)
        self.chat_panel.set_message_pagination(has_older=snapshot.next_before_sequence is not None)
        selected_id = (
            None if snapshot.conversation is None else snapshot.conversation.conversation_id
        )
        self.history_page.set_conversations(snapshot.conversations, selected_id)
        if selected_id is not None:
            self.history_page.set_messages(
                selected_id,
                snapshot.messages,
                has_older=snapshot.next_before_sequence is not None,
            )
        return True

    def _on_persisted_conversation_loaded(self, snapshot_object: object) -> None:
        self._conversation_switch_pending = False
        if not isinstance(snapshot_object, ConversationSnapshot):
            self._on_data_operation_failed("conversation", "InvalidConversationSnapshot")
            return
        self._apply_conversation_snapshot(snapshot_object)

    def _on_older_messages_loaded(self, snapshot_object: object) -> None:
        if not isinstance(snapshot_object, OlderMessagesSnapshot):
            return
        if snapshot_object.conversation_id != self.data_service.current_conversation_id:
            return
        presentation = snapshot_object.presentation_entries or snapshot_object.turns
        self.chat_panel.prepend_turns(presentation)
        self.chat_panel.set_message_pagination(
            has_older=snapshot_object.next_before_sequence is not None
        )
        self.history_page.set_messages(
            snapshot_object.conversation_id,
            snapshot_object.messages,
            prepend=True,
            has_older=snapshot_object.next_before_sequence is not None,
        )

    def _request_conversation_switch(self, conversation_id: str) -> None:
        if not self._can_change_conversation():
            self.data_service.refresh_history()
            return
        self._conversation_switch_pending = True
        self.data_service.switch_conversation(conversation_id)

    def _request_new_conversation(self) -> None:
        if not self._can_change_conversation():
            return
        self._conversation_switch_pending = True
        self.data_service.create_conversation()

    def _request_delete_conversation(self, conversation_id: str) -> None:
        if not self._can_change_conversation():
            return
        self._conversation_switch_pending = True
        self.data_service.delete_conversation(conversation_id)

    def _request_clear_history(self) -> None:
        if not self._can_change_conversation():
            return
        self._conversation_switch_pending = True
        self.data_service.clear_conversations()

    def _request_older_history_messages(self, conversation_id: str) -> None:
        if conversation_id == self.data_service.current_conversation_id:
            self.data_service.load_older_messages()

    def _can_change_conversation(self) -> bool:
        if (
            self._conversation_switch_pending
            or self._pending_foreground_action is not None
            or self.conversation.state is not ConversationState.IDLE
            or self.conversation.is_active
        ):
            self.history_page.set_status("请等当前回复结束后再管理会话。", error=True)
            return False
        return True

    def _request_memory_search(
        self,
        query: str,
        kind: str,
        status: str,
        sort: str,
    ) -> None:
        self.data_service.refresh_memories(
            query,
            kind,
            status,
            sort,
            pinned=self.memory_page.pinned_filter,
        )

    def _on_memories_loaded(self, snapshot_object: object) -> None:
        if not isinstance(snapshot_object, MemoryListSnapshot):
            self.memory_page.set_status("记忆列表返回了无效数据。", error=True)
            return
        selected = self.memory_page.current_memory_id
        self.memory_page.set_memories(snapshot_object.rows, selected)
        self.memory_page.set_failed_tasks(snapshot_object.failed_jobs)
        self.memory_page.set_status(f"已加载 {len(snapshot_object.rows)} 条本地记忆。")

    def _on_vector_index_status_changed(self, status: object) -> None:
        if self._exiting:
            return
        available = bool(getattr(status, "available", False))
        category = str(getattr(status, "category", ""))
        if not available:
            self._vector_index_available = False
            return
        # retry_backend() publishes a transient loading state after creating
        # the backend but before persisted caches are installed.  Reindex only
        # once that recovery reaches the stable ready state.
        if category != "ready":
            return
        recovered = available and not self._vector_index_available
        self._vector_index_available = available
        if available and (not self._initial_index_refresh_requested or recovered):
            self._initial_index_refresh_requested = True
            self.vector_index.refresh_incremental()

    def _queue_vector_index_status(self, status: object) -> None:
        if self._exiting:
            return
        QTimer.singleShot(
            0,
            self.application,
            lambda: self._on_vector_index_status_changed(status),
        )

    def _on_vector_status_for_p6(self, status: object) -> None:
        if self._exiting:
            return
        category = str(getattr(status, "category", "unknown"))
        generation = getattr(status, "persona_generation_id", None)
        count = int(getattr(status, "persona_count", 0))
        state = "active" if generation and category == "ready" else category
        self.persona_page.set_index_status(state, generation=generation, count=count)
        safe_error = str(getattr(status, "safe_error_category", ""))
        if safe_error:
            self._last_safe_error_category = safe_error
        self._refresh_diagnostics()

    def _disconnect_vector_status_ui(self) -> None:
        """Stop shutdown-time status emissions from entering Qt widgets."""

        for callback in (
            self.memory_page.set_retrieval_status,
            self._queue_vector_index_status,
            self._on_vector_status_for_p6,
        ):
            try:
                self.vector_index.status_changed.disconnect(callback)
            except (RuntimeError, TypeError):
                # Shutdown is idempotent and a callback may already be disconnected.
                continue

    def _verify_local_embedding_model(self) -> None:
        self.memory_page.set_status("正在后台验证本地模型。")
        self.vector_index.retry_backend()

    def _request_index_rebuild(self, _corpus: object | None = None) -> None:
        if self.vector_index.rebuild():
            self.memory_page.set_status("索引正在后台重新构建。")
        else:
            self.memory_page.set_status(
                "索引当前无法重建；聊天会继续使用 FTS5。",
                error=True,
            )

    def _request_incremental_index_refresh(self, _corpus: object | None = None) -> None:
        if not self.vector_index.refresh_incremental():
            category = getattr(self.vector_index.status, "category", "")
            if category not in {"rebuilding", "incremental"}:
                self.memory_page.set_status(
                    "增量索引当前不可用；聊天会继续使用 FTS5。",
                    error=True,
                )

    def _on_background_job_status_changed(
        self,
        _job_id: str,
        kind: str,
        status: str,
    ) -> None:
        if kind == "memory_extraction" and status == "completed":
            self._request_incremental_index_refresh("user_memory")

    def _set_memory_enabled(self, enabled: bool) -> None:
        previous = bool(self.settings["memory"]["enabled"])
        if enabled == previous:
            return
        candidate = deepcopy(self.settings)
        candidate["memory"]["enabled"] = enabled
        try:
            self.settings_repository.save(candidate)
        except SettingsError as exc:
            self.logger.warning(
                "Memory setting could not be saved error_type=%s", type(exc).__name__
            )
            self.memory_page.set_memory_enabled(previous)
            self.memory_page.set_status("长期记忆开关保存失败，设置未改变。", error=True)
            return
        self.settings.clear()
        self.settings.update(candidate)
        self.data_service.set_memory_enabled(enabled)
        self.memory_jobs.set_memory_enabled(enabled)
        self.memory_page.set_memory_enabled(enabled)
        self.memory_page.set_status(
            "长期记忆已启用。" if enabled else "长期记忆已停用；聊天与摘要仍会保存。"
        )

    def _on_source_context_loaded(
        self,
        snapshot_object: object,
        message_id: str,
    ) -> None:
        if not isinstance(snapshot_object, ConversationSnapshot):
            return
        conversation_id = (
            ""
            if snapshot_object.conversation is None
            else snapshot_object.conversation.conversation_id
        )
        self.settings_window.show_and_activate("history")
        self.history_page.set_conversations(snapshot_object.conversations, conversation_id)
        self.history_page.set_messages(
            conversation_id,
            snapshot_object.messages,
            has_older=snapshot_object.next_before_sequence is not None,
        )
        self.history_page.focus_message(message_id)

    def _on_data_operation_failed(self, operation: str, category: str) -> None:
        self.logger.warning(
            "Local data operation failed operation=%s error_type=%s",
            operation,
            category,
        )
        self._conversation_switch_pending = False
        self._last_safe_error_category = "storage_error"
        self.proactive_interactions.persistence_failed(operation)
        if operation == "clear_memories":
            self.memory_jobs.resume()
            if self._data_change_state == "clear_memories":
                self._finish_data_operation()
        message = "本地数据操作失败，请稍后重试。"
        if operation in {"finalize", "checkpoint"}:
            self.chat_panel.set_status(message, kind="error")
        if operation.startswith("memory") or operation in {
            "edit_memory",
            "pin_memory",
            "archive_memory",
            "restore_memory",
            "delete_memory",
            "clear_memories",
            "retry_job",
        }:
            self.memory_page.set_status(message, error=True)
        else:
            self.history_page.set_status(message, error=True)

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
        """Pause background generation without blocking Qt, then save fail-closed."""

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
        if self._provider_switch_pending:
            self.model_settings_window.apply_save_result(
                success=False,
                message="已有模型配置正在安全保存，请稍候。",
            )
            return
        if self._pending_foreground_action is not None:
            self.model_settings_window.apply_save_result(
                success=False,
                message="正在为前台对话让出模型资源，请稍候。",
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

        self._provider_switch_pending = True
        self._provider_switch_generation += 1
        generation = self._provider_switch_generation
        # An opt-in AI greeting shares the background provider lane. Cancel its
        # opportunity before changing credentials/provider state; a late worker
        # callback is generation-guarded by the proactive controller and cannot
        # display a fallback greeting during the switch.
        self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        background_clean = self.background_generation.pause(wait_ms=0)
        if not background_clean or self.background_generation.is_running:
            self._pending_provider_configuration = config_object
            self._pending_provider_secret = secret
            self.model_settings_window.status_label.setText(
                "正在取消后台生成并等待模型安全空闲；界面仍可使用。"
            )
            QTimer.singleShot(
                2_000,
                self.application,
                lambda: self._on_provider_switch_timeout(generation),
            )
            return
        if not self._background_jobs_enabled:
            self._perform_provider_configuration_transaction(
                config_object,
                secret,
                resume_background=False,
            )
            return
        if self.memory_jobs.pause(wait_ms=0):
            self._perform_provider_configuration_transaction(
                config_object,
                secret,
                resume_background=True,
            )
            return

        self._pending_provider_configuration = config_object
        self._pending_provider_secret = secret
        self.model_settings_window.status_label.setText(
            "正在等待后台记忆任务安全停止；界面仍可使用。"
        )
        QTimer.singleShot(
            2_000,
            self.application,
            lambda: self._on_provider_switch_timeout(generation),
        )

    def _on_background_provider_idle(self) -> None:
        if self._pending_foreground_action is not None:
            self._dispatch_pending_foreground_action()
            return
        config = self._pending_provider_configuration
        if not self._provider_switch_pending or config is None:
            return
        secret = self._pending_provider_secret
        self._pending_provider_configuration = None
        self._pending_provider_secret = None
        self._provider_switch_generation += 1
        self._perform_provider_configuration_transaction(
            config,
            secret,
            resume_background=self._background_jobs_enabled,
        )

    def _on_provider_switch_timeout(self, generation: int) -> None:
        if (
            not self._provider_switch_pending
            or generation != self._provider_switch_generation
            or self._pending_provider_configuration is None
        ):
            return
        self._pending_provider_configuration = None
        self._pending_provider_secret = None
        self._provider_switch_pending = False
        self._provider_switch_generation += 1
        self.background_generation.resume()
        if self._data_writable and not self.memory_maintenance_timer.isActive():
            self.memory_maintenance_timer.start()
        self.memory_jobs.resume()
        self.model_settings_window.apply_save_result(
            success=False,
            message="后台记忆任务未能及时停止，模型配置未更改。",
        )

    def _perform_provider_configuration_transaction(
        self,
        config_object: ProviderConfig,
        secret: str | None,
        *,
        resume_background: bool,
    ) -> None:
        """Apply WinCred + JSON only after no background request owns the provider."""

        try:
            self._perform_provider_configuration_transaction_inner(config_object, secret)
        finally:
            self._pending_provider_configuration = None
            self._pending_provider_secret = None
            self._provider_switch_pending = False
            self._provider_switch_generation += 1
            self.background_generation.resume()
            if resume_background:
                self.memory_jobs.resume()

    def _perform_provider_configuration_transaction_inner(
        self,
        config_object: ProviderConfig,
        secret: str | None,
    ) -> None:
        if (
            self._exiting
            or self.conversation.state is not ConversationState.IDLE
            or self.conversation.is_active
        ):
            self.model_settings_window.apply_save_result(
                success=False,
                message="当前状态已变化，模型配置未更改。",
            )
            return
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
        background_provider_switched = False
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
                if not self.background_generation.set_provider(disabled_provider):
                    raise SettingsError("Background provider could not be disabled while saving.")
                background_provider_switched = True
            if secret is not None:
                credential_write_attempted = True
                self.credential_store.write_secret(secret)
            if not self._mock_chat and not self.conversation.set_provider(candidate_provider):
                raise SettingsError("Provider could not be switched while saving.")
            if not self._mock_chat and not self.background_generation.set_provider(
                candidate_provider
            ):
                raise SettingsError("Background provider could not be switched while saving.")
            self.settings_repository.save(candidate_settings)
        except (CredentialStoreError, SettingsError, ValueError) as exc:
            if provider_switched:
                self.conversation.set_provider(disabled_provider)
            if background_provider_switched:
                self.background_generation.set_provider(disabled_provider)
            settings_restored, credential_restored = self._restore_provider_transaction(
                settings_snapshot,
                previous_secret,
                restore_settings=disabled_marker_saved,
                restore_credential=credential_write_attempted,
            )
            provider_restored = not provider_switched or self.conversation.set_provider(
                previous_provider
            )
            background_provider_restored = (
                not background_provider_switched
                or self.background_generation.set_provider(previous_provider)
            )
            restored = (
                settings_restored
                and credential_restored
                and provider_restored
                and background_provider_restored
            )
            self.logger.warning(
                "Provider configuration save failed error_type=%s settings_rollback=%s "
                "credential_rollback=%s provider_rollback=%s "
                "background_provider_rollback=%s",
                type(exc).__name__,
                settings_restored,
                credential_restored,
                provider_restored,
                background_provider_restored,
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
                    self.background_generation.set_provider(safe_provider)
                    self._active_chat_provider = safe_provider
                    self.chat_panel.set_provider_mode("unconfigured")
            self.model_settings_window.apply_save_result(success=False, message=message)
            self._refresh_diagnostics()
            return

        self.settings.clear()
        self.settings.update(candidate_settings)
        self.provider_config = config_object
        if not self._mock_chat:
            self._active_chat_provider = candidate_provider
        self._settings_trusted = True
        self._chat_available = True
        self.data_service.set_provider_metadata(
            config_object.preset.value,
            config_object.model,
        )
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
        self._refresh_diagnostics()

    def show_pet(self) -> None:
        self._ensure_pet_visible()
        self.pet_window.show_without_activate()

    def show_chat(self) -> None:
        self.proactive_interactions.dismiss_current()
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
            self.proactive_interactions.dismiss_current()
            self.hide_chat()
            self.pet_window.hide()
        else:
            self.show_pet()

    def _on_pet_visibility_changed(self, visible: bool) -> None:
        if not visible and hasattr(self, "proactive_interactions"):
            self.proactive_interactions.dismiss_current()

    def _send_chat_message(self, text: str) -> None:
        if self._provider_switch_pending:
            self.chat_panel.set_status("对话模型切换中，请稍候。", kind="error")
            return
        if not self._chat_available:
            self.chat_panel.set_status("请先配置并测试对话模型。", kind="error")
            return
        if not self._data_initialized:
            self._pending_initial_message = text
            self.chat_panel.set_status("正在初始化本地聊天数据，稍后会自动发送。")
            return
        if not self._data_writable:
            self.chat_panel.set_status("本地数据当前无法安全写入，消息未发送。", kind="error")
            return
        if self._conversation_switch_pending:
            self.chat_panel.set_status("会话切换中，请稍候。", kind="error")
            return
        self._begin_foreground_action("send", text)

    def _retry_chat_turn(self, turn_id: str) -> None:
        if self._provider_switch_pending:
            self.chat_panel.set_status("对话模型切换中，请稍候。", kind="error")
            return
        if not self._chat_available:
            self.chat_panel.set_status("请先配置并测试对话模型。", kind="error")
            return
        if not self._data_initialized or not self._data_writable:
            self.chat_panel.set_status("本地数据当前无法安全写入，无法重试。", kind="error")
            return
        if self._conversation_switch_pending:
            self.chat_panel.set_status("会话切换中，请稍候。", kind="error")
            return
        self._begin_foreground_action("retry", turn_id)

    def _begin_foreground_action(self, kind: str, payload: str) -> None:
        """Give visible chat exclusive provider priority without blocking Qt."""

        if self._pending_foreground_action is not None:
            self.chat_panel.set_status("正在为前台对话让出模型资源，请稍候。")
            return
        self._pending_foreground_action = (kind, payload)
        self.chat_panel.set_foreground_preparing(True)
        self.chat_panel.set_status("正在停止后台生成并准备对话。")
        self._foreground_lane_timer.start()

        # The proactive controller invalidates its opportunity before the shared
        # runner can report cancellation.  Marking the memory scheduler as
        # foreground-active also prevents a newly claimed job from taking the
        # lane between cancellation and the visible request.
        self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        self._set_foreground_lane_active(True)
        if (
            self._pending_foreground_action is not None
            and not self.background_generation.is_running
        ):
            self._dispatch_pending_foreground_action()

    def _dispatch_pending_foreground_action(self) -> None:
        action = self._pending_foreground_action
        if action is None:
            return
        if self.background_generation.is_running:
            return
        self._pending_foreground_action = None
        self._foreground_lane_timer.stop()
        kind, payload = action
        if (
            self._exiting
            or self._provider_switch_pending
            or self._conversation_switch_pending
            or not self._chat_available
            or not self._data_initialized
            or not self._data_writable
            or self.conversation.state is not ConversationState.IDLE
            or self.conversation.is_active
        ):
            self.chat_panel.set_foreground_preparing(False)
            self._set_foreground_lane_active(False)
            self.chat_panel.set_status("当前状态已变化，消息未发送。", kind="error")
            return

        started = time.perf_counter()
        if kind == "send":
            turn = self.conversation.send_message(payload)
            if turn is not None:
                self._turn_started_at[turn.turn_id] = started
                return
        elif kind == "retry" and self.conversation.retry(payload):
            self._turn_started_at[payload] = started
            return

        self.chat_panel.set_foreground_preparing(False)
        self._set_foreground_lane_active(False)
        message = "当前无法重试这轮对话。" if kind == "retry" else "消息未能发送，请重试。"
        self.chat_panel.set_status(message, kind="error")

    def _on_foreground_lane_timeout(self) -> None:
        if self._pending_foreground_action is None:
            return
        self._pending_foreground_action = None
        self.chat_panel.set_foreground_preparing(False)
        self._set_foreground_lane_active(False)
        self.chat_panel.set_status("后台生成未能及时停止，消息未发送，请重试。", kind="error")

    def _set_foreground_lane_active(self, active: bool) -> None:
        if self._background_jobs_enabled:
            self.memory_jobs.set_foreground_active(active)
        elif active:
            self.background_generation.pause(wait_ms=0)
        else:
            self.background_generation.resume()

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
        provider_category = str(category)
        diagnostic_categories = {
            "authentication": "provider_authentication",
            "network": "provider_network",
            "rate_limit": "provider_rate_limited",
            "timeout": "provider_timeout",
            "not_configured": "provider_unconfigured",
        }
        if provider_category in diagnostic_categories:
            self._last_safe_error_category = diagnostic_categories[provider_category]
            self._refresh_diagnostics()
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
        # The runner is also used by opt-in proactive greetings even when the
        # durable memory scheduler is disabled by a test/development injection.
        self._set_foreground_lane_active(state is not ConversationState.IDLE)
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
        candidate = deepcopy(self.settings)
        candidate["pet"]["position"] = position.to_document()
        if not self._save_settings(candidate, category="pet_position"):
            self._restore_pet_position()
            self._schedule_chat_reposition()

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
        self._data_change_state = "exiting"
        self.logger.info("Application exit requested")
        self._shutdown_clean = self._shutdown_background_tasks()
        self.window.prepare_to_exit()
        self.window.hide()
        self.chat_panel.hide()
        self.settings_window.hide()
        self.greeting_bubble.hide()
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
            self._shutdown_clean = self._shutdown_background_tasks()
            self.chat_panel.hide()
            self.settings_window.hide()
            self.greeting_bubble.hide()
            self.pet_window.hide()
            self.instance_guard.close()

    def _shutdown_background_tasks(self, timeout_ms: int = 5_000) -> bool:
        """Cancel network work, persist terminal states, then drain the data thread."""

        self._pending_provider_configuration = None
        self._pending_provider_secret = None
        self._provider_switch_pending = False
        self._provider_switch_generation += 1
        self.memory_maintenance_timer.stop()
        self._proactive_pause_retry_timer.stop()
        self._foreground_lane_timer.stop()
        self._pending_foreground_action = None
        self._disconnect_vector_status_ui()
        self.data_service.stop_prompt_preparations()
        total_ms = max(0, timeout_ms)
        deadline = time.monotonic() + total_ms / 1_000

        def slice_ms(fraction: float) -> int:
            remaining = max(0, round((deadline - time.monotonic()) * 1_000))
            return min(remaining, round(total_ms * fraction))

        proactive_clean = self.proactive_interactions.stop(wait_ms=slice_ms(0.10))
        background_clean = self.memory_jobs.shutdown(wait_ms=slice_ms(0.15))
        # Start cancellation for the connection test before waiting on conversation cleanup.
        self.model_settings_window.cancel_test()
        conversation_clean = self.conversation.shutdown(wait_ms=slice_ms(0.25))
        vector_clean = True
        remaining_seconds = min(
            max(0.0, deadline - time.monotonic()),
            total_ms * 0.15 / 1_000,
        )
        try:
            self.vector_index.close().result(timeout=remaining_seconds)
        except (FutureTimeoutError, RuntimeError):
            vector_clean = False
        remaining_seconds = min(
            max(0.0, deadline - time.monotonic()),
            total_ms * 0.15 / 1_000,
        )
        try:
            self.vector_runtime.close(timeout=remaining_seconds, cancel_pending=True)
        except TimeoutError:
            vector_clean = False
        settings_clean = self.settings_window.shutdown(wait_ms=slice_ms(0.10))
        remaining_ms = max(0, round((deadline - time.monotonic()) * 1000))
        data_clean = self.data_service.shutdown(wait_ms=remaining_ms)
        if not conversation_clean:
            self.logger.error(
                "Conversation worker did not stop within the shared shutdown deadline"
            )
        if not background_clean:
            self.logger.error(
                "Background memory worker did not stop within the shared shutdown deadline"
            )
        if not settings_clean:
            self.logger.error(
                "Provider connection test did not stop within the shared shutdown deadline"
            )
        if not vector_clean:
            self.logger.error("Local vector runtime did not stop within the shutdown deadline")
        if not data_clean:
            self.logger.error("Local data thread did not stop within the shutdown deadline")
        return (
            proactive_clean
            and background_clean
            and conversation_clean
            and vector_clean
            and settings_clean
            and data_clean
        )


def _install_persona_knowledge(
    stores: LocalDataStores,
    source: Path,
    destination: Path,
) -> int:
    """Validate/import one persona corpus and retain its exact local source."""

    drafts = load_persona_knowledge_jsonl(source, persona_id="kurisu")
    previous = stores.personas.list_active_documents("kurisu")
    rollback = tuple(
        PersonaKnowledgeDraft(
            content=item.content,
            tags=item.tags,
            source_ref=item.source_ref,
            source_hash=item.source_hash,
            knowledge_id=item.knowledge_id,
        )
        for item in previous
    )
    payload = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        rows = stores.personas.replace_persona("kurisu", drafts)
        try:
            os.replace(temporary, destination)
        except OSError:
            stores.personas.replace_persona("kurisu", rollback)
            raise
        temporary = None
        return len(rows)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _install_greeting_catalog(source: Path, destination: Path) -> GreetingCatalog:
    """Strictly validate and atomically retain one local greeting catalog."""

    catalog = load_greeting_catalog(source)
    payload = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        return catalog
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
