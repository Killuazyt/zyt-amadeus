"""Application lifecycle, desktop-pet, and attached-chat coordination."""

from __future__ import annotations

import hmac
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QScreen
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QSystemTrayIcon

from amadeus_desktop import __version__
from amadeus_desktop.attachment_runtime import AttachmentImportRuntime
from amadeus_desktop.attachments import AttachmentStore
from amadeus_desktop.audio_runtime import (
    AudioDeviceCatalog,
    MicrophoneCapture,
    WavPlaybackQueue,
)
from amadeus_desktop.autostart import AutostartError, AutostartManager
from amadeus_desktop.background_generation import BackgroundGenerationRunner
from amadeus_desktop.build_info import BuildInfo, load_build_info
from amadeus_desktop.chat_geometry import calculate_chat_panel_placement
from amadeus_desktop.chat_models import (
    AttachmentSnapshot,
    AttachmentSource,
    ConversationState,
    InputModality,
    ProviderCapability,
)
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
    delete_all_amadeus_credentials,
)
from amadeus_desktop.data_management import (
    DataManagementError,
    RestoreCleanupError,
    RestoreRollbackError,
    SQLiteExportRepository,
    ValidatedRestorePayload,
    apply_validated_restore,
    create_backup_archive,
    disable_provider_credential_reuse_for_restore,
    discard_staged_restore,
    export_chat_json,
    export_memory_json,
    plan_factory_reset,
    stage_backup_for_restore,
)
from amadeus_desktop.data_runtime import DataPriority, SerialDataThread
from amadeus_desktop.database import SCHEMA_VERSION
from amadeus_desktop.deep_memory_coordinator import (
    DeepMemoryJobCoordinator,
    DeepMemoryRepositoryBundle,
)
from amadeus_desktop.diagnostics import DiagnosticStatusService
from amadeus_desktop.embedding_backend import FastEmbedEmbeddingBackend
from amadeus_desktop.embedding_calibration import calibrate_backend
from amadeus_desktop.embedding_model import resolve_runtime_model_directory
from amadeus_desktop.focus_mode import (
    DEFAULT_FOCUSED_FIRST_CHUNK_TIMEOUT_MS,
    FOCUS_STATUS_TEXT,
    FocusModeDecision,
    classify_focus_mode,
)
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
from amadeus_desktop.proactive import ProactiveTrigger, build_proactive_visual_request
from amadeus_desktop.proactive_controller import (
    ProactiveInteractionController,
    ProactiveVisualPlan,
)
from amadeus_desktop.provider_catalog import ProviderAuth, ProviderRole, load_provider_catalog
from amadeus_desktop.provider_config import (
    MIMO_SPEECH_CREDENTIAL_REF,
    MULTIMODAL_CREDENTIAL_REF,
    PROVIDER_CREDENTIAL_REF,
    AuthMode,
    ProviderConfig,
    ProviderPreset,
    TokenLimitField,
)
from amadeus_desktop.provider_profiles import (
    ProviderProfile,
    ProviderProfileError,
    ProviderSettings,
    transient_credential_fingerprint,
)
from amadeus_desktop.provider_router import ProviderRouter
from amadeus_desktop.settings import (
    CURRENT_SCHEMA_VERSION,
    InvalidSettingsError,
    SettingsError,
    SettingsFileSnapshot,
    SettingsRepository,
    validate_settings_document,
)
from amadeus_desktop.single_instance import SingleInstance
from amadeus_desktop.speech import MiMoSpeechClient, MiMoSpeechConfig, SpeechToken, VoiceState
from amadeus_desktop.speech_runtime import SpeechNetworkRuntime
from amadeus_desktop.storage_models import PersonaKnowledgeDraft
from amadeus_desktop.ui.chat_panel import ChatPanel
from amadeus_desktop.ui.control_window import ControlWindow
from amadeus_desktop.ui.greeting_bubble import GreetingBubble
from amadeus_desktop.ui.pet_window import PetWindow
from amadeus_desktop.ui.provider_settings import (
    ProviderSettingsChange,
    ProviderSettingsPage,
)
from amadeus_desktop.ui.settings_window import SettingsWindow
from amadeus_desktop.ui.tray import TrayController
from amadeus_desktop.ui.visual_settings import VisualSettingsPage
from amadeus_desktop.ui.voice_settings import VoiceSettingsPage
from amadeus_desktop.vector_index import VectorIndexCoordinator, VectorIndexRepositories
from amadeus_desktop.vector_runtime import PriorityVectorRuntime
from amadeus_desktop.visual import VisualSourceKind
from amadeus_desktop.visual_runtime import VisualSourceManager, _LatestImageEncoder
from amadeus_desktop.voice_session import VoiceSessionController


def _local_now() -> datetime:
    """Return timezone-aware local wall time for policy and durable timestamps."""

    return datetime.now().astimezone()


@dataclass(frozen=True, slots=True)
class _SendAction:
    text: str
    attachments: tuple[AttachmentSnapshot, ...] = ()
    input_modality: InputModality = InputModality.TEXT
    visual_sampled: bool = False


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
        multimodal_credential_store: CredentialStore | None = None,
        speech_credential_store: CredentialStore | None = None,
        profile_credential_store_factory: Callable[[ProviderProfile], CredentialStore]
        | None = None,
        connection_tester: ProviderConnectionTester | None = None,
        first_chunk_timeout_ms: int = 15_000,
        focused_first_chunk_timeout_ms: int = DEFAULT_FOCUSED_FIRST_CHUNK_TIMEOUT_MS,
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
        self._active_focus_decision = FocusModeDecision()
        if (
            isinstance(focused_first_chunk_timeout_ms, bool)
            or not isinstance(focused_first_chunk_timeout_ms, int)
            or focused_first_chunk_timeout_ms <= 0
        ):
            raise ValueError("focused_first_chunk_timeout_ms must be a positive integer")
        self._focused_first_chunk_timeout_ms = max(
            first_chunk_timeout_ms,
            focused_first_chunk_timeout_ms,
        )
        self._data_initialized = False
        self._data_writable = False
        self._pending_initial_message: _SendAction | None = None
        self._conversation_switch_pending = False
        self._pending_conversation_change_kind: str | None = None
        self._pending_conversation_change_target: str | None = None
        self._pending_conversation_previous_id: str | None = None
        self._pending_foreground_action: tuple[str, object] | None = None
        self._provider_switch_pending = False
        self._pending_provider_configuration: ProviderConfig | None = None
        self._pending_provider_secret: str | None = None
        self._provider_switch_generation = 0
        self.provider_catalog = load_provider_catalog()
        self._profile_credential_store_factory = (
            profile_credential_store_factory or WinCredentialStore.for_profile
        )
        self._explicit_profile_credential_store_factory = (
            profile_credential_store_factory is not None
        )
        self.model_provider_settings = ProviderSettings.from_mapping(
            settings["model_providers"],
            self.provider_catalog,
        )
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
        self.multimodal_config = ProviderConfig.from_mapping(settings["multimodal"]["provider"])
        self.multimodal_credential_store = multimodal_credential_store or WinCredentialStore(
            self.multimodal_config.credential_ref
        )
        self.speech_credential_store = speech_credential_store or WinCredentialStore(
            MIMO_SPEECH_CREDENTIAL_REF
        )
        provider_availability_message = self._reconcile_model_provider_credentials(
            persist=allow_saved_provider,
        )
        if provider_availability_message:
            status_message = (
                f"{status_message}\n{provider_availability_message}"
                if status_message
                else provider_availability_message
            )
        self._pending_voice_token: SpeechToken | None = None
        self.autostart_manager = autostart_manager or AutostartManager()
        self._reconcile_autostart_setting()
        self._clear_expired_proactive_pause()
        self.provider_config = ProviderConfig.from_mapping(self.settings["provider"])
        conversation_profile = self.model_provider_settings.assigned_profile(
            ProviderRole.CONVERSATION
        )
        has_provider_secret = bool(
            conversation_profile
            and conversation_profile.enabled
            and allow_saved_provider
            and self._profile_credential_available(conversation_profile)
        )

        vision_profile = self.model_provider_settings.assigned_profile(ProviderRole.VISION)
        has_multimodal_secret = bool(
            vision_profile
            and vision_profile.enabled
            and allow_saved_provider
            and self._profile_credential_available(vision_profile)
        )
        selected_multimodal_store: CredentialStore | None = None
        multimodal_settings = self.settings["multimodal"]
        if (
            chat_provider is None
            and not mock_chat
            and allow_saved_provider
            and not self.provider_catalog.degraded
        ):
            selected_multimodal_store = (
                self._credential_store_for_profile(vision_profile)
                if vision_profile is not None
                else None
            )
            if selected_multimodal_store is not None:
                try:
                    has_multimodal_secret = selected_multimodal_store.has_secret()
                except CredentialStoreError as exc:
                    logger.warning(
                        "Multimodal credential store unavailable error_type=%s",
                        type(exc).__name__,
                    )

        if tray_available is None:
            tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_available = tray_available

        pet_settings = self.settings["pet"]
        always_on_top = bool(self.settings["general"]["always_on_top"])
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
        if explicit_provider or mock_chat:
            selected_multimodal_provider: ChatProvider | None = selected_provider
        elif (
            has_multimodal_secret
            and allow_saved_provider
            and multimodal_settings["enabled"] is True
            and selected_multimodal_store is not None
        ):
            selected_multimodal_provider = OpenAICompatibleChatProvider(
                self.multimodal_config,
                selected_multimodal_store,
            )
        else:
            selected_multimodal_provider = None
        self.provider_router = ProviderRouter(
            selected_provider,
            selected_multimodal_provider,
        )
        if chat_provider is None and not mock_chat and allow_saved_provider:
            self.provider_router.replace_profiles(
                self.model_provider_settings,
                self.provider_catalog,
                self._credential_store_for_profile,
            )
        self._active_chat_provider = selected_provider
        if background_jobs_enabled is None:
            background_jobs_enabled = not explicit_provider
        self._background_jobs_enabled = bool(background_jobs_enabled)
        provider_ready = bool(
            conversation_profile
            and conversation_profile.enabled
            and conversation_profile.is_tested(ProviderRole.CONVERSATION)
            and has_provider_secret
            and allow_saved_provider
        )
        self._chat_available = explicit_provider or mock_chat or provider_ready
        self.attachment_store = AttachmentStore(self.paths.attachments_directory)
        self.attachment_runtime = AttachmentImportRuntime(
            self.attachment_store,
            parent=application,
        )
        self.chat_panel.set_attachment_root(self.paths.attachments_directory)
        self.chat_panel.attachment_paths_requested.connect(self._import_attachment_paths)
        self.chat_panel.attachment_image_requested.connect(self._import_attachment_image)
        self.attachment_runtime.imported.connect(self._on_attachment_imported)
        self.attachment_runtime.failed.connect(
            lambda message: self.chat_panel.set_status(message, kind="error")
        )
        self.attachment_runtime.busy_changed.connect(self.chat_panel.set_attachment_processing)
        self.attachment_runtime.context_imported.connect(self._on_context_attachment_imported)
        self.attachment_runtime.context_failed.connect(self._on_context_attachment_failed)
        self.visual_sources = VisualSourceManager(parent=application)
        self.region_screenshot_encoder = _LatestImageEncoder(parent=application)
        self.region_screenshot_encoder.encoded.connect(self._on_region_screenshot_encoded)
        self.region_screenshot_encoder.failed.connect(self._on_attachment_image_encode_failed)
        self._privacy_mode = False
        self._active_visual_turn_id: str | None = None
        self._region_screenshot_overlay = None
        self.visual_sources.state_changed.connect(self.chat_panel.set_visual_state)
        self.visual_sources.failed.connect(self._on_visual_source_failed)
        self.chat_panel.region_screenshot_requested.connect(self._request_region_screenshot)
        self.chat_panel.visual_source_requested.connect(self._start_visual_source)
        self.chat_panel.visual_stop_requested.connect(self._stop_visual_source)
        self.chat_panel.privacy_mode_requested.connect(self._set_privacy_mode)
        self.data_runtime = SerialDataThread(
            lambda: create_local_data_stores(
                self.paths.database_file,
                self.paths.migration_backup_directory,
                self.attachment_store,
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
                resource.deep_memories,
            ),
            backend_calibrator=lambda backend: calibrate_backend(backend).calibration.threshold,
            parent=application,
        )
        self.data_service = LocalDataService(
            self.data_runtime,
            memory_enabled=bool(self.settings["memory"]["enabled"]),
            deep_memory_enabled=bool(self.settings["memory"].get("deep_memory_enabled", True)),
            follow_user_language=bool(self.settings["persona"]["follow_user_language"]),
            vector_query=self.vector_index.query,
            parent=application,
        )
        self.memory_maintenance_timer = QTimer(application)
        self.memory_maintenance_timer.setInterval(24 * 60 * 60 * 1_000)
        self.memory_maintenance_timer.timeout.connect(self.data_service.run_memory_maintenance)
        self.background_generation = BackgroundGenerationRunner(
            self.provider_router,
            parent=application,
        )
        self.background_generation.idle.connect(self._on_background_provider_idle)
        self.visual_background_generation = BackgroundGenerationRunner(
            self.provider_router,
            parent=application,
        )
        self.visual_background_generation.idle.connect(self._on_visual_generation_idle)
        self.memory_jobs = MemoryJobCoordinator(
            self.data_runtime,
            self.background_generation,
            lambda resource: JobRepositoryBundle(
                resource.conversations,
                resource.jobs,
                resource.memories,
                resource.deep_memories,
            ),
            memory_enabled=bool(self.settings["memory"]["enabled"]),
            parent=application,
        )
        self.deep_memory_jobs = DeepMemoryJobCoordinator(
            self.data_runtime,
            self.background_generation,
            lambda resource: DeepMemoryRepositoryBundle(
                resource.jobs,
                resource.memories,
                resource.deep_memories,
            ),
            memory_enabled=bool(self.settings["memory"]["enabled"]),
            deep_memory_enabled=bool(self.settings["memory"].get("deep_memory_enabled", True)),
            parent=application,
        )
        self.data_service.jobs_enqueued.connect(self.memory_jobs.poll)
        self.data_service.jobs_enqueued.connect(self.deep_memory_jobs.poll)
        self.memory_jobs.job_failed.connect(
            lambda _job_id, _kind, _category: self.data_service.refresh_memories()
        )
        self.memory_jobs.job_status_changed.connect(self._on_background_job_status_changed)
        self.deep_memory_jobs.job_failed.connect(
            lambda _job_id, _kind, _category: self.data_service.refresh_memories()
        )
        self.deep_memory_jobs.job_status_changed.connect(self._on_background_job_status_changed)
        self.deep_memory_jobs.scheduler_error.connect(
            lambda category: self.logger.warning("Deep memory scheduler error_type=%s", category)
        )
        self.memory_jobs.scheduler_error.connect(
            lambda category: self.logger.warning(
                "Background memory scheduler error_type=%s", category
            )
        )
        if mock_chat or isinstance(selected_provider, ScriptedChatProvider):
            self.data_service.set_provider_metadata("explicit_mock", "scripted")
            self.data_service.set_multimodal_provider_metadata("explicit_mock", "scripted")
        else:
            conversation_metadata = self.model_provider_settings.assigned_profile(
                ProviderRole.CONVERSATION
            )
            vision_metadata = self.model_provider_settings.assigned_profile(ProviderRole.VISION)
            self.data_service.set_provider_metadata(
                conversation_metadata.display_name if conversation_metadata else None,
                conversation_metadata.model_for(ProviderRole.CONVERSATION)
                if conversation_metadata
                else None,
            )
            self.data_service.set_multimodal_provider_metadata(
                vision_metadata.display_name if vision_metadata else None,
                vision_metadata.model_for(ProviderRole.VISION) if vision_metadata else None,
            )
        self.conversation = ConversationCoordinator(
            self.provider_router,
            first_chunk_timeout_ms=first_chunk_timeout_ms,
            stream_idle_timeout_ms=stream_idle_timeout_ms,
            persistence=self.data_service,
            parent=application,
        )
        self.pet_window.clicked.connect(self.toggle_chat)
        self.chat_panel.send_requested.connect(self._send_chat_message)
        self.chat_panel.send_with_attachments_requested.connect(self._send_chat_message)
        self.chat_panel.stop_requested.connect(self.conversation.stop)
        self.chat_panel.retry_requested.connect(self._retry_chat_turn)
        self.chat_panel.hide_requested.connect(self.hide_chat)
        self.chat_panel.configure_requested.connect(self._show_model_settings_from_chat)
        self.conversation.turn_added.connect(self.chat_panel.add_turn)
        self.conversation.turn_updated.connect(self.chat_panel.update_turn)
        self.conversation.chunk_received.connect(self._bind_captured_provider_metadata)
        self.conversation.state_changed.connect(self._on_conversation_state_changed)
        self.conversation.request_finished.connect(self._record_conversation_evidence)

        voice_settings = self.settings["voice"]
        self.audio_device_catalog = AudioDeviceCatalog(parent=application)
        self.microphone_capture = MicrophoneCapture(
            self.audio_device_catalog,
            parent=application,
        )
        self.voice_playback = WavPlaybackQueue(
            self.audio_device_catalog,
            parent=application,
        )
        selected_speech_store = self._speech_store_for_source(
            str(voice_settings["credential_source"])
        )
        speech_client = MiMoSpeechClient(
            self._speech_config_from_settings(voice_settings),
            selected_speech_store or self.speech_credential_store,
        )
        self.speech_network = SpeechNetworkRuntime(
            speech_client,
            speech_client,
            parent=application,
        )
        self.voice_session = VoiceSessionController(
            self.microphone_capture,
            self.speech_network,
            self.voice_playback,
            input_device_id=str(voice_settings["input_device_id"]),
            output_device_id=str(voice_settings["output_device_id"]),
            parent=application,
        )
        has_own_speech_secret = False
        if allow_saved_provider:
            with suppress(CredentialStoreError):
                has_own_speech_secret = self.speech_credential_store.has_secret()
        has_selected_speech_secret = False
        if selected_speech_store is not None and allow_saved_provider:
            with suppress(CredentialStoreError):
                has_selected_speech_secret = selected_speech_store.has_secret()
        self._voice_configured = bool(
            voice_settings["enabled"]
            and selected_speech_store is not None
            and has_selected_speech_secret
        )
        self.chat_panel.push_to_talk_pressed.connect(self._press_to_talk)
        self.chat_panel.push_to_talk_released.connect(self.voice_session.release_to_send)
        self.chat_panel.hands_free_requested.connect(self._set_hands_free_session)
        self.chat_panel.voice_stop_requested.connect(self.voice_session.stop_session)
        self.voice_session.state_changed.connect(self.chat_panel.set_voice_state)
        self.voice_session.status_changed.connect(self.chat_panel.set_voice_status)
        self.voice_session.hands_free_changed.connect(self.chat_panel.set_hands_free_checked)
        self.voice_session.transcript_ready.connect(self._on_voice_transcript)
        self.voice_session.stop_chat_requested.connect(self._stop_voice_chat)
        self.conversation.chunk_received.connect(self._on_voice_chat_chunk)
        self.conversation.request_finished.connect(self._on_voice_chat_finished)

        self.model_settings_window = ProviderSettingsPage(
            self.model_provider_settings,
            self.provider_catalog,
            credential_store_factory=self._credential_store_for_profile,
            tester=connection_tester,
            blocked_saved_credential_profile_ids=(
                profile.profile_id
                for profile in self.model_provider_settings.profiles
                if not allow_saved_provider
                or (
                    profile.credential_slot == "legacy_chat"
                    and settings.get("provider_enabled") is not True
                )
                or (
                    profile.credential_slot == "legacy_vision"
                    and multimodal_settings.get("enabled") is not True
                )
            ),
        )
        self.model_settings_window.save_requested.connect(self._save_model_provider_settings)
        self.voice_settings_page = VoiceSettingsPage(
            voice_settings,
            self.audio_device_catalog,
            has_own_secret=has_own_speech_secret,
            text_credential_reusable=(
                has_provider_secret and self._mimo_credential_compatible(self.provider_config)
            ),
            multimodal_credential_reusable=(
                has_multimodal_secret and self._mimo_credential_compatible(self.multimodal_config)
            ),
            reusable_mimo_profiles=tuple(
                profile
                for profile in self.model_provider_settings.profiles
                if self._mimo_profile_credential_compatible(profile)
                and profile.enabled
                and self._profile_credential_available(profile)
            ),
        )
        self.voice_settings_page.save_requested.connect(self._save_voice_configuration)
        self.visual_settings_page = VisualSettingsPage(self.settings["visual"])
        self.visual_settings_page.save_requested.connect(self._save_visual_configuration)
        preferred_visual_index = self.chat_panel.visual_source_combo.findData(
            str(self.settings["visual"]["preferred_source"])
        )
        if preferred_visual_index >= 0:
            self.chat_panel.visual_source_combo.setCurrentIndex(preferred_visual_index)
        self.settings_window = SettingsWindow(
            self.model_settings_window,
            voice_page=self.voice_settings_page,
            visual_page=self.visual_settings_page,
        )
        self.general_page = self.settings_window.general_page
        self.pet_page = self.settings_window.pet_page
        self.persona_page = self.settings_window.persona_page
        self.history_page = self.settings_window.history_page
        self.memory_page = self.settings_window.memory_page
        self.proactive_page = self.settings_window.proactive_page
        self.diagnostics_page = self.settings_window.diagnostics_page
        self._sync_settings_pages()
        self.memory_page.set_memory_enabled(bool(self.settings["memory"]["enabled"]))
        self.memory_page.set_deep_memory_enabled(
            bool(self.settings["memory"].get("deep_memory_enabled", True))
        )
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
            visual_generation_runner=self.visual_background_generation,
            visual_plan_builder=self._build_proactive_visual_plan,
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
            runtime_profile = self.model_provider_settings.assigned_profile(
                ProviderRole.CONVERSATION
            )
            self.chat_panel.set_provider_mode(
                "provider",
                provider_name=(
                    f"{runtime_profile.display_name} · "
                    f"{runtime_profile.model_for(ProviderRole.CONVERSATION)}"
                    if runtime_profile is not None and not explicit_provider
                    else f"{self.provider_config.display_name} · {self.provider_config.model}"
                ),
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
        return bool(not self._mock_chat and self._chat_available)

    def _proactive_provider_metadata(self) -> tuple[str | None, str | None]:
        if not self._provider_is_configured():
            return None, None
        profile = self.model_provider_settings.assigned_profile(ProviderRole.CONVERSATION)
        if profile is None:
            return None, None
        return profile.display_name, profile.model_for(ProviderRole.CONVERSATION)

    def _build_proactive_visual_plan(
        self,
        now: datetime,
        trigger: ProactiveTrigger,
    ) -> ProactiveVisualPlan | None:
        """Claim only an existing opportunity and keep its sampled frame volatile."""

        visual = self.settings.get("visual")
        if (
            not isinstance(visual, dict)
            or visual.get("active_vision_enabled") is not True
            or self._privacy_mode
            or not self.visual_sources.active
        ):
            return None
        frame = self.visual_sources.latest()
        vision_profile = self.model_provider_settings.assigned_profile(ProviderRole.VISION)
        if (
            frame is None
            or vision_profile is None
            or not vision_profile.enabled
            or not vision_profile.is_tested(ProviderRole.VISION)
            or not self._profile_credential_available(vision_profile)
        ):
            return ProactiveVisualPlan(None)
        return ProactiveVisualPlan(
            build_proactive_visual_request(now, trigger, frame),
            (
                vision_profile.display_name,
                vision_profile.model_for(ProviderRole.VISION),
            ),
        )

    def _acquire_proactive_ai_lane(self) -> bool:
        if (
            self._exiting
            or self._provider_switch_pending
            or self.conversation.state is not ConversationState.IDLE
            or self.memory_jobs.has_active_job
            or self.deep_memory_jobs.has_active_job
            or self.background_generation.is_running
            or self.visual_background_generation.is_running
        ):
            return False
        deep_clean = self.deep_memory_jobs.pause(wait_ms=0)
        if not deep_clean or not self.memory_jobs.pause(wait_ms=0):
            self.deep_memory_jobs.resume()
            self.memory_jobs.resume()
            return False
        self.background_generation.resume()
        return True

    def _release_proactive_ai_lane(self) -> None:
        if not self._exiting and not self._provider_switch_pending:
            self.deep_memory_jobs.resume()
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
            "visual_unavailable": "主动视觉没有可用画面或多模态配置，请检查来源后重试。",
            "visual_busy": "主动视觉资源正忙，本次机会已安全跳过。",
            "visual_generation_failed": "主动视觉分析失败，本次画面未保留；可稍后重试。",
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
        # Data-management controls are disabled while another exclusive operation runs.
        # Keep direct/programmatic re-entry silent as in the established P6 contract.
        if self._data_change_state != "idle":
            return
        if not self._can_start_data_change():
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
                attachment_root=self.paths.attachments_directory,
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
            "替换本地数据并退出。\n\n"
            "选择“是”：仅当 Profile ID、凭据安全域、本机绑定和连接测试指纹全部匹配时，"
            "尝试复用本机仍存在的凭据。\n"
            "选择“否”：继续恢复，但停用全部模型 Profile 并要求重新测试。\n"
            "选择“取消”：不恢复。",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            with suppress(DataManagementError):
                discard_staged_restore(payload)
            self._finish_data_operation()
            return
        if answer == QMessageBox.StandardButton.No:
            try:
                payload = disable_provider_credential_reuse_for_restore(payload)
            except DataManagementError as exc:
                with suppress(DataManagementError):
                    discard_staged_restore(payload)
                self._on_restore_validation_failed(type(exc).__name__)
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
                attachment_root=self.paths.attachments_directory,
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
            or self.attachment_runtime.busy
            or self.region_screenshot_encoder.busy
            or self.voice_session.state is not VoiceState.OFF
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
        self._sync_voice_availability()

    def _finish_data_operation(self) -> None:
        self._data_change_state = "idle"
        self._services_stopped_for_data_change = False
        self._set_data_management_controls_enabled(True)
        self._sync_voice_availability()

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
        self._pending_conversation_change_kind = None
        self._pending_conversation_change_target = None
        self._pending_conversation_previous_id = self.data_service.current_conversation_id
        self.chat_panel.set_conversation_switch_pending(True)
        self.chat_panel.set_storage_availability(False, read_only=True)
        self.memory_maintenance_timer.stop()
        proactive_clean = self.proactive_interactions.stop(wait_ms=2_000)
        deep_clean = self.deep_memory_jobs.pause(wait_ms=2_000)
        memory_clean = self.memory_jobs.pause(wait_ms=2_000)
        if not proactive_clean or not deep_clean or not memory_clean:
            self._resume_after_aborted_data_change()
            return False
        return True

    def _resume_after_aborted_data_change(self) -> None:
        self._finish_conversation_change()
        self.chat_panel.set_storage_availability(
            self._data_writable,
            read_only=not self._data_writable,
        )
        self._sync_voice_availability()
        if self._data_writable:
            self.memory_maintenance_timer.start()
            self.deep_memory_jobs.resume()
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
        production_wincred = bool(
            not self._explicit_profile_credential_store_factory
            and isinstance(self.credential_store, WinCredentialStore)
            and isinstance(self.multimodal_credential_store, WinCredentialStore)
            and isinstance(self.speech_credential_store, WinCredentialStore)
        )
        if production_wincred:
            try:
                delete_all_amadeus_credentials()
            except CredentialStoreError:
                failures = True
        else:
            deleted_store_ids: set[int] = set()
            reset_stores: list[CredentialStore] = [
                self.credential_store,
                self.multimodal_credential_store,
                self.speech_credential_store,
            ]
            for profile in self.model_provider_settings.profiles:
                if profile.auth is not ProviderAuth.NONE:
                    reset_stores.append(self._credential_store_for_profile(profile))
            for store in reset_stores:
                if id(store) in deleted_store_ids:
                    continue
                deleted_store_ids.add(id(store))
                try:
                    store.delete_secret()
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
        deep_clean = self.deep_memory_jobs.pause(wait_ms=2_000)
        if not deep_clean or not self.memory_jobs.pause(wait_ms=2_000):
            self.deep_memory_jobs.resume()
            self.memory_jobs.resume()
            self._finish_data_operation()
            self.memory_page.set_status(
                "后台记忆任务尚未安全停止，未清空记忆。",
                error=True,
            )
            return
        clear = getattr(self.data_service, "clear_all_memories", None)
        if not callable(clear) or not clear():
            self.deep_memory_jobs.resume()
            self.memory_jobs.resume()
            self._finish_data_operation()
            self.memory_page.set_status("清空记忆请求未能提交。", error=True)

    def _on_memories_cleared(self, count: int) -> None:
        self.deep_memory_jobs.resume()
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

        if self._exiting:
            return
        # The chat panel may be always-on-top and otherwise cover the model form
        # and its save action while the settings window has focus.
        self.hide_chat()
        self.settings_window.show_and_activate("model")

    def _show_model_settings_from_chat(self) -> None:
        if self._exiting:
            return
        self.hide_chat()
        QTimer.singleShot(0, self.application, self.show_model_settings)

    def show_history_settings(self) -> None:
        if self._exiting:
            return
        self.hide_chat()
        already_current = self.settings_window.current_page == "history"
        self.settings_window.show_and_activate("history")
        if already_current:
            self.data_service.refresh_history()

    def _show_history_settings_from_chat(self) -> None:
        if self._exiting:
            return
        self.hide_chat()
        QTimer.singleShot(0, self.application, self.show_history_settings)

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
        self.data_service.history_loaded.connect(self._on_history_loaded)
        self.data_service.memories_loaded.connect(self._on_memories_loaded)
        self.data_service.memories_cleared.connect(self._on_memories_cleared)
        self.data_service.memory_sources_loaded.connect(self.memory_page.set_sources)
        self.data_service.layer_sources_loaded.connect(self.memory_page.set_layer_sources)
        self.data_service.layer_versions_loaded.connect(self.memory_page.set_versions)
        self.data_service.deletion_impact_loaded.connect(self.memory_page.confirm_delete_impact)
        self.data_service.source_context_loaded.connect(self._on_source_context_loaded)
        self.data_service.operation_failed.connect(self._on_data_operation_failed)
        self.data_service.index_rebuild_requested.connect(self._request_incremental_index_refresh)
        self.vector_index.status_changed.connect(self.memory_page.set_retrieval_status)
        self.vector_index.status_changed.connect(self._queue_vector_index_status)
        self.vector_index.status_changed.connect(self._on_vector_status_for_p6)

        self.chat_panel.load_older_requested.connect(self.data_service.load_older_messages)
        self.chat_panel.conversation_switch_requested.connect(self._request_conversation_switch)
        self.chat_panel.new_conversation_requested.connect(self._request_new_conversation)
        self.chat_panel.history_requested.connect(self._show_history_settings_from_chat)
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
        self.memory_page.layer_memory_selected.connect(self.data_service.load_layer_details)
        self.memory_page.enabled_changed.connect(self._set_memory_enabled)
        self.memory_page.deep_enabled_changed.connect(self._set_deep_memory_enabled)
        self.memory_page.edit_requested.connect(self.data_service.edit_memory)
        self.memory_page.pin_requested.connect(self.data_service.set_memory_pinned)
        self.memory_page.archive_requested.connect(self.data_service.archive_memory)
        self.memory_page.restore_requested.connect(self.data_service.restore_memory)
        self.memory_page.delete_requested.connect(self.data_service.delete_memory)
        self.memory_page.delete_impact_requested.connect(self.data_service.load_deletion_impact)
        self.memory_page.derived_edit_requested.connect(self.data_service.edit_derived_memory)
        self.memory_page.derived_pin_requested.connect(self.data_service.set_derived_memory_pinned)
        self.memory_page.derived_archive_requested.connect(self.data_service.archive_derived_memory)
        self.memory_page.derived_restore_requested.connect(self.data_service.restore_derived_memory)
        self.memory_page.derived_delete_requested.connect(self.data_service.delete_derived_memory)
        self.memory_page.derived_confirm_requested.connect(self.data_service.confirm_derived_memory)
        self.memory_page.derived_deny_requested.connect(self.data_service.deny_derived_memory)
        self.memory_page.rollback_requested.connect(self.data_service.rollback_memory)
        self.memory_page.conflict_resolution_requested.connect(
            self.data_service.resolve_memory_conflict
        )
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
        self.memory_page.set_deep_memory_enabled(self.data_service.deep_memory_enabled)
        self.data_service.refresh_memories()
        self._refresh_persona_summary()
        self._refresh_diagnostics()
        if self._background_jobs_enabled and self._data_writable:
            self.memory_jobs.start()
            self.deep_memory_jobs.start()
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
            self._queue_send_action(pending)

    def _on_data_startup_failed(self, category: str) -> None:
        self._data_initialized = True
        self._data_writable = False
        self._pending_initial_message = None
        self.chat_panel.set_storage_availability(False, read_only=True)
        self._sync_voice_availability()
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
            self.deep_memory_jobs.shutdown(wait_ms=0)
        if self._data_initialized:
            self.chat_panel.set_storage_availability(writable, read_only=not writable)
            self._sync_voice_availability()
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
        self.chat_panel.set_conversations(snapshot.conversations, selected_id)
        if selected_id is not None:
            self.history_page.set_messages(
                selected_id,
                snapshot.messages,
                has_older=snapshot.next_before_sequence is not None,
            )
        return True

    def _on_persisted_conversation_loaded(self, snapshot_object: object) -> None:
        if not isinstance(snapshot_object, ConversationSnapshot):
            self._on_data_operation_failed("conversation", "InvalidConversationSnapshot")
            return
        change_completed = self._pending_conversation_change_matches(snapshot_object)
        if change_completed:
            self._finish_conversation_change()
        if self._apply_conversation_snapshot(snapshot_object) and change_completed:
            self.chat_panel.set_status("会话已更新。", kind="success")

    def _on_history_loaded(self, conversations: object, selected_id: str) -> None:
        current_id = self.data_service.current_conversation_id
        effective_selected_id = selected_id if current_id is None else current_id
        self.history_page.set_conversations(conversations, effective_selected_id)
        self.chat_panel.set_conversations(conversations, effective_selected_id)

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
        if conversation_id == self.data_service.current_conversation_id:
            self.chat_panel.set_conversation_switch_pending(False)
            return
        if not self._can_change_conversation():
            self.chat_panel.set_conversation_switch_pending(self._conversation_switch_pending)
            self.data_service.refresh_history()
            return
        self._start_conversation_change("switch", conversation_id)
        self.chat_panel.set_status("正在切换会话…", kind="working")
        self.data_service.switch_conversation(conversation_id)

    def _request_new_conversation(self) -> None:
        if not self._can_change_conversation():
            self.chat_panel.set_conversation_switch_pending(self._conversation_switch_pending)
            return
        self._start_conversation_change("create")
        self.chat_panel.set_status("正在新建会话…", kind="working")
        self.data_service.create_conversation()

    def _request_delete_conversation(self, conversation_id: str) -> None:
        if not self._can_change_conversation():
            return
        self._start_conversation_change("delete", conversation_id)
        self.data_service.delete_conversation(
            conversation_id,
            protected_attachment_paths=self.chat_panel.draft_attachment_paths(
                excluding_conversation_ids=(conversation_id,)
            ),
        )

    def _request_clear_history(self) -> None:
        if not self._can_change_conversation():
            return
        self._start_conversation_change("clear")
        self.data_service.clear_conversations()

    def _request_older_history_messages(self, conversation_id: str) -> None:
        if conversation_id == self.data_service.current_conversation_id:
            self.data_service.load_older_messages()

    def _start_conversation_change(self, kind: str, target: str | None = None) -> None:
        self._conversation_switch_pending = True
        self._pending_conversation_change_kind = kind
        self._pending_conversation_change_target = target
        self._pending_conversation_previous_id = self.data_service.current_conversation_id
        self.chat_panel.set_conversation_switch_pending(True)

    def _finish_conversation_change(self) -> None:
        self._conversation_switch_pending = False
        self._pending_conversation_change_kind = None
        self._pending_conversation_change_target = None
        self._pending_conversation_previous_id = None
        self.chat_panel.set_conversation_switch_pending(False)

    def _pending_conversation_change_matches(self, snapshot: ConversationSnapshot) -> bool:
        if not self._conversation_switch_pending:
            return False
        kind = self._pending_conversation_change_kind
        if kind is None:
            return False
        current_id = (
            None if snapshot.conversation is None else snapshot.conversation.conversation_id
        )
        conversation_ids = {conversation.conversation_id for conversation in snapshot.conversations}
        if kind == "switch":
            return current_id == self._pending_conversation_change_target
        if kind == "create":
            return current_id is not None and current_id != self._pending_conversation_previous_id
        if kind == "delete":
            return self._pending_conversation_change_target not in conversation_ids
        if kind == "clear":
            return (
                len(conversation_ids) == 1
                and current_id is not None
                and current_id != self._pending_conversation_previous_id
            )
        return False

    def _can_change_conversation(self) -> bool:
        if (
            self._data_change_state != "idle"
            or self._conversation_switch_pending
            or self._provider_switch_pending
            or self._pending_foreground_action is not None
            or self.conversation.state is not ConversationState.IDLE
            or self.conversation.is_active
        ):
            if self._data_change_state != "idle":
                history_message = "本地数据操作进行中，请稍候。"
                chat_message = "本地数据操作进行中，请稍候。"
            elif self._conversation_switch_pending:
                history_message = "会话正在更新，请稍候。"
                chat_message = "会话正在更新，请稍候。"
            elif self._provider_switch_pending:
                history_message = "模型配置正在更新，请稍候。"
                chat_message = "模型配置正在更新，请稍候再切换会话。"
            else:
                history_message = "请等当前回复结束后再管理会话。"
                chat_message = "请等当前回复结束后再切换会话。"
            self.history_page.set_status(history_message, error=True)
            self.chat_panel.set_status(chat_message, kind="error")
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
        self.memory_page.set_layer_data(
            working=snapshot_object.working_rows,
            recent=snapshot_object.recent_rows,
            facts=snapshot_object.rows,
            reflections=snapshot_object.reflection_rows,
            personas=snapshot_object.persona_rows,
            static_persona=snapshot_object.static_persona_rows,
            timeline=snapshot_object.timeline_rows,
            audit=snapshot_object.audit_rows,
            conflicts=snapshot_object.conflict_rows,
            selected_id=selected,
        )
        self.memory_page.set_failed_tasks(snapshot_object.failed_jobs)
        persistent_count = (
            len(snapshot_object.rows)
            + len(snapshot_object.reflection_rows)
            + len(snapshot_object.persona_rows)
        )
        self.memory_page.set_status(f"已加载 {persistent_count} 条持久语义记忆。")

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
            self.deep_memory_jobs.poll()
        elif kind in {"deep_memory_cycle", "persona_promotion"} and status == "completed":
            self._request_incremental_index_refresh(kind)
            self.data_service.refresh_memories()

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
        self.deep_memory_jobs.set_memory_enabled(enabled)
        self.memory_page.set_memory_enabled(enabled)
        self.memory_page.set_status(
            "长期记忆已启用。" if enabled else "长期记忆已停用；聊天与摘要仍会保存。"
        )

    def _set_deep_memory_enabled(self, enabled: bool) -> None:
        previous = bool(self.settings["memory"].get("deep_memory_enabled", True))
        if enabled == previous:
            return
        candidate = deepcopy(self.settings)
        candidate["memory"]["deep_memory_enabled"] = enabled
        try:
            self.settings_repository.save(candidate)
        except SettingsError as exc:
            self.logger.warning(
                "Deep-memory setting could not be saved error_type=%s",
                type(exc).__name__,
            )
            self.memory_page.set_deep_memory_enabled(previous)
            self.memory_page.set_status("深层记忆开关保存失败，设置未改变。", error=True)
            return
        self.settings.clear()
        self.settings.update(candidate)
        self.data_service.set_deep_memory_enabled(enabled)
        self.deep_memory_jobs.set_deep_memory_enabled(enabled)
        self.memory_page.set_deep_memory_enabled(enabled)
        self.memory_page.set_status(
            "证据、反思与人格印象已启用。"
            if enabled
            else "深层派生与召回已暂停；事实、近期和角色资料继续工作。"
        )
        self.data_service.refresh_memories()

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
        self._last_safe_error_category = "storage_error"
        self.proactive_interactions.persistence_failed(operation)
        if operation == "clear_memories":
            self.deep_memory_jobs.resume()
            self.memory_jobs.resume()
            if self._data_change_state == "clear_memories":
                self._finish_data_operation()
        message = "本地数据操作失败，请稍后重试。"
        conversation_operations = {
            "conversation",
            "create_conversation",
            "delete_conversation",
            "clear_history",
        }
        expected_operation = {
            "switch": "conversation",
            "create": "create_conversation",
            "delete": "delete_conversation",
            "clear": "clear_history",
        }.get(self._pending_conversation_change_kind or "")
        failed_pending_change = (
            self._conversation_switch_pending and operation == expected_operation
        )
        if failed_pending_change:
            self._finish_conversation_change()
        if operation in {"finalize", "checkpoint", *conversation_operations}:
            self.chat_panel.set_status(message, kind="error")
        if failed_pending_change:
            self.data_service.refresh_history()
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

    def _read_multimodal_secret(self) -> str | None:
        return self.multimodal_credential_store.read_secret()

    def _credential_store_for_profile(self, profile: ProviderProfile) -> CredentialStore:
        """Resolve retained v9 slots or one deterministic dynamic Profile target."""

        if self._explicit_profile_credential_store_factory:
            return self._profile_credential_store_factory(profile)
        if profile.credential_slot == "legacy_chat":
            return self.credential_store
        if profile.credential_slot == "legacy_vision":
            return self.multimodal_credential_store
        return self._profile_credential_store_factory(profile)

    def _profile_credential_available(self, profile: ProviderProfile | None) -> bool:
        if profile is None:
            return False
        if profile.auth is ProviderAuth.NONE:
            return True
        try:
            return self._credential_store_for_profile(profile).has_secret()
        except CredentialStoreError:
            return False

    def _reconcile_model_provider_credentials(self, *, persist: bool) -> str | None:
        """Fail closed after restore/catalog drift without discarding Profile data."""

        assigned_roles: dict[str, set[ProviderRole]] = {}
        for role, profile_id in self.model_provider_settings.assignments.items():
            if profile_id is not None:
                assigned_roles.setdefault(profile_id, set()).add(role)
        profiles: list[ProviderProfile] = []
        disabled = False
        catalog_degraded = self.provider_catalog.degraded
        for profile in self.model_provider_settings.profiles:
            valid_catalog_contract = profile.catalog_id in self.provider_catalog.entries
            assigned_tests_valid = all(
                profile.is_tested(role) for role in assigned_roles.get(profile.profile_id, set())
            )
            credential_valid = not profile.enabled or self._profile_credential_available(profile)
            should_disable = bool(
                profile.enabled
                and (not valid_catalog_contract or not assigned_tests_valid or not credential_valid)
            )
            profiles.append(replace(profile, enabled=False) if should_disable else profile)
            disabled = disabled or should_disable
        if not disabled:
            return (
                "内置提供商目录损坏，当前仅开放安全自定义恢复入口。" if catalog_degraded else None
            )
        reconciled = ProviderSettings(
            tuple(profiles),
            self.model_provider_settings.assignments,
        ).runtime_validated(self.provider_catalog)
        self.model_provider_settings = reconciled
        candidate = deepcopy(self.settings)
        candidate["model_providers"] = reconciled.to_mapping()
        voice_source = str(candidate["voice"].get("credential_source", ""))
        if voice_source.startswith("profile:"):
            voice_profile = reconciled.profile(voice_source.removeprefix("profile:"))
            if (
                voice_profile is None
                or not voice_profile.enabled
                or not self._mimo_profile_credential_compatible(voice_profile)
            ):
                candidate["voice"]["enabled"] = False
                candidate["voice"]["credential_source"] = "independent"
        if persist:
            try:
                self.settings_repository.save(candidate)
            except SettingsError:
                self.logger.warning("Provider restore reconciliation could not be persisted")
            else:
                self.settings.clear()
                self.settings.update(candidate)
        return (
            "提供商目录、连接测试指纹或 Windows 凭据安全域不匹配；"
            "相关 Profile 已自动停用，请在模型提供商中心重新测试。"
        )

    def _save_model_provider_settings(self, change_object: object) -> None:
        """Commit profile settings, credentials and routing as one rollback unit."""

        if not isinstance(change_object, ProviderSettingsChange):
            self.model_settings_window.apply_save_result(
                success=False,
                message="模型提供商变更无效。",
            )
            return
        if self.conversation.is_active or self.conversation.state is not ConversationState.IDLE:
            self.model_settings_window.apply_save_result(
                success=False,
                message="请等待当前回复结束后再替换任务路由。",
            )
            return
        if self.background_generation.is_running or self.visual_background_generation.is_running:
            self.model_settings_window.apply_save_result(
                success=False,
                message="后台模型任务正在运行；已保留原路由，请稍后重试。",
            )
            return
        memory_was_paused = not self.memory_jobs.pause(wait_ms=0)
        deep_memory_was_paused = not self.deep_memory_jobs.pause(wait_ms=0)
        if memory_was_paused or deep_memory_was_paused:
            self.memory_jobs.resume()
            self.deep_memory_jobs.resume()
            self.model_settings_window.apply_save_result(
                success=False,
                message="记忆后台任务正在暂停，请稍后重试；原路由未更改。",
            )
            return
        try:
            if (
                not isinstance(change_object.secret_updates, Mapping)
                or not isinstance(change_object.tested_secret_fingerprints, Mapping)
                or not isinstance(change_object.deleted_profiles, tuple)
            ):
                raise ProviderProfileError("模型提供商事务载荷无效。")
            candidate_settings = change_object.settings.validated(
                self.provider_catalog
            ).require_assigned_tests()
            candidate_profile_ids = {profile.profile_id for profile in candidate_settings.profiles}
            current_profiles = {
                profile.profile_id: profile for profile in self.model_provider_settings.profiles
            }
            if not set(change_object.secret_updates).issubset(candidate_profile_ids):
                raise ProviderProfileError("模型凭据变更引用了不存在的 Profile。")
            secret_update_ids = set(change_object.secret_updates)
            tested_secret_ids = set(change_object.tested_secret_fingerprints)
            assigned_secret_update_ids = secret_update_ids.intersection(
                profile_id
                for profile_id in candidate_settings.assignments.values()
                if profile_id is not None
            )
            if not tested_secret_ids.issubset(
                secret_update_ids
            ) or not assigned_secret_update_ids.issubset(tested_secret_ids):
                raise ProviderProfileError("模型凭据变更缺少对应的连接测试。")
            deleted_profile_ids = [profile.profile_id for profile in change_object.deleted_profiles]
            expected_deleted_profile_ids = set(current_profiles) - candidate_profile_ids
            if (
                len(deleted_profile_ids) != len(set(deleted_profile_ids))
                or any(profile_id in candidate_profile_ids for profile_id in deleted_profile_ids)
                or set(deleted_profile_ids) != expected_deleted_profile_ids
                or any(
                    current_profiles.get(profile.profile_id) != profile
                    for profile in change_object.deleted_profiles
                )
            ):
                raise ProviderProfileError("删除 Profile 的事务边界无效。")
            for secret in change_object.secret_updates.values():
                if (
                    not isinstance(secret, str)
                    or not secret
                    or secret != secret.strip()
                    or "\x00" in secret
                    or secret.casefold().startswith("tp-")
                ):
                    raise ProviderProfileError("模型凭据变更无效。")
            for profile_id, tested_fingerprint in change_object.tested_secret_fingerprints.items():
                secret = change_object.secret_updates[profile_id]
                if (
                    not isinstance(profile_id, str)
                    or not isinstance(tested_fingerprint, str)
                    or len(tested_fingerprint) != 64
                    or not hmac.compare_digest(
                        transient_credential_fingerprint(secret),
                        tested_fingerprint,
                    )
                ):
                    raise ProviderProfileError("模型凭据变更未通过当前连接测试。")
            for profile_id in change_object.secret_updates:
                profile = candidate_settings.profile(profile_id)
                if profile is None or profile.auth is ProviderAuth.NONE:
                    raise ProviderProfileError("无鉴权 Profile 不得写入凭据。")
            voice_source = str(self.settings["voice"].get("credential_source", ""))
            if voice_source.startswith("profile:"):
                voice_profile = candidate_settings.profile(voice_source.removeprefix("profile:"))
                if (
                    voice_profile is None
                    or not voice_profile.enabled
                    or not self._mimo_profile_credential_compatible(voice_profile)
                ):
                    raise ProviderProfileError(
                        "语音正在复用该 MiMo PAYG Profile，请先调整语音凭据来源。"
                    )
        except ProviderProfileError as exc:
            self.model_settings_window.apply_save_result(
                success=False,
                message=str(exc),
            )
            self.memory_jobs.resume()
            self.deep_memory_jobs.resume()
            return
        previous_document = deepcopy(self.settings)
        previous_voice_source = str(previous_document["voice"].get("credential_source", ""))
        voice_profile_changed = False
        if previous_voice_source.startswith("profile:"):
            voice_profile_id = previous_voice_source.removeprefix("profile:")
            voice_profile_changed = (
                self.model_provider_settings.profile(voice_profile_id)
                != candidate_settings.profile(voice_profile_id)
                or voice_profile_id in change_object.secret_updates
            )
        if voice_profile_changed:
            self.voice_session.stop_session(stop_chat=False)
            if self.speech_network.busy:
                self.model_settings_window.apply_save_result(
                    success=False,
                    message="语音 ASR/TTS 网络任务尚未停止，模型 Profile 未更改。",
                )
                self.memory_jobs.resume()
                self.deep_memory_jobs.resume()
                return
        try:
            settings_snapshot = self.settings_repository.capture_snapshot()
        except SettingsError as exc:
            self.logger.warning(
                "Model provider settings snapshot failed error_type=%s",
                type(exc).__name__,
            )
            self.model_settings_window.apply_save_result(
                success=False,
                message="设置文件无法安全备份，模型 Profile 未更改。",
            )
            self.memory_jobs.resume()
            self.deep_memory_jobs.resume()
            return
        previous_profiles = {
            profile.profile_id: profile for profile in self.model_provider_settings.profiles
        }
        changed_profiles = {profile.profile_id: profile for profile in candidate_settings.profiles}
        retired_profiles: list[ProviderProfile] = []
        for profile_id, previous_profile in previous_profiles.items():
            candidate_profile = changed_profiles.get(profile_id)
            if candidate_profile is None or (
                self._provider_credential_domain(previous_profile)
                != self._provider_credential_domain(candidate_profile)
            ):
                retired_profiles.append(previous_profile)

        affected_domains: dict[str, ProviderProfile] = {}
        for profile_id in change_object.secret_updates:
            profile = changed_profiles[profile_id]
            domain = self._provider_credential_domain(profile)
            if domain is not None:
                affected_domains[domain] = profile
        for profile in retired_profiles:
            domain = self._provider_credential_domain(profile)
            if domain is not None:
                affected_domains[domain] = profile

        credential_snapshots: dict[str, tuple[CredentialStore, str | None]] = {}
        for domain, profile in affected_domains.items():
            store = self._credential_store_for_profile(profile)
            try:
                credential_snapshots[domain] = (store, store.read_secret())
            except CredentialStoreError:
                self.model_settings_window.apply_save_result(
                    success=False,
                    message="Windows 凭据管理器不可用，原配置未更改。",
                )
                self.memory_jobs.resume()
                self.deep_memory_jobs.resume()
                return

        candidate_document = deepcopy(self.settings)
        candidate_document["model_providers"] = candidate_settings.to_mapping()
        # Keep v9 compatibility mirrors synchronized while no longer using them
        # as the P7G runtime source of truth.
        conversation_profile = candidate_settings.assigned_profile(ProviderRole.CONVERSATION)
        vision_profile = candidate_settings.assigned_profile(ProviderRole.VISION)
        conversation_mirror = self._legacy_mirror_config_or_none(
            conversation_profile,
            ProviderRole.CONVERSATION,
            PROVIDER_CREDENTIAL_REF,
        )
        vision_mirror = self._legacy_mirror_config_or_none(
            vision_profile,
            ProviderRole.VISION,
            MULTIMODAL_CREDENTIAL_REF,
        )
        candidate_document["provider_enabled"] = bool(
            conversation_profile
            and conversation_profile.enabled
            and conversation_profile.credential_slot == "legacy_chat"
            and conversation_mirror is not None
        )
        if conversation_mirror is not None:
            candidate_document["provider"] = conversation_mirror.to_mapping()
        candidate_document["multimodal"]["enabled"] = bool(
            vision_profile
            and vision_profile.enabled
            and vision_profile.credential_slot == "legacy_vision"
            and vision_mirror is not None
        )
        candidate_document["multimodal"]["reuse_mimo_credential"] = False
        if vision_mirror is not None:
            candidate_document["multimodal"]["provider"] = vision_mirror.to_mapping()

        disabled_document = deepcopy(candidate_document)
        for raw_profile in disabled_document["model_providers"]["profiles"]:
            raw_profile["enabled"] = False
        disabled_document["provider_enabled"] = False
        disabled_document["multimodal"]["enabled"] = False
        disabled_document["voice"]["enabled"] = False
        mutated_domains: list[str] = []
        disabled_saved = False
        router_replaced = False
        try:
            self.settings_repository.save(disabled_document)
            disabled_saved = True
            for profile_id, secret in change_object.secret_updates.items():
                profile = changed_profiles[profile_id]
                domain = self._provider_credential_domain(profile)
                if domain is None:
                    raise ProviderProfileError("无鉴权 Profile 不得写入凭据。")
                mutated_domains.append(domain)
                credential_snapshots[domain][0].write_secret(secret)
            for role in ProviderRole:
                profile = candidate_settings.assigned_profile(role)
                if profile is None or not profile.enabled:
                    continue
                store = self._credential_store_for_profile(profile)
                if profile.auth is not ProviderAuth.NONE and not store.has_secret():
                    raise SettingsError("Assigned provider credential is unavailable.")
            for profile in retired_profiles:
                domain = self._provider_credential_domain(profile)
                if domain is None:
                    continue
                mutated_domains.append(domain)
                credential_snapshots[domain][0].delete_secret()
            # A stale scope must never erase a candidate credential.  This
            # second check also protects injected credential-store factories
            # used by tests from accidentally aliasing two security domains.
            for role in ProviderRole:
                profile = candidate_settings.assigned_profile(role)
                if profile is None or not profile.enabled or profile.auth is ProviderAuth.NONE:
                    continue
                if not self._credential_store_for_profile(profile).has_secret():
                    raise SettingsError("Assigned provider credential was lost during cleanup.")
            # Enable the durable candidate only after every credential mutation
            # and cleanup has succeeded. Until this point a crash leaves the
            # previously written all-disabled document as the recovery state.
            self.settings_repository.save(candidate_document)
            self.provider_router.replace_profiles(
                candidate_settings,
                self.provider_catalog,
                self._credential_store_for_profile,
            )
            router_replaced = True
        except (CredentialStoreError, ProviderProfileError, SettingsError, ValueError) as exc:
            credential_rollback_ok = True
            for domain in reversed(tuple(dict.fromkeys(mutated_domains))):
                store, previous_secret = credential_snapshots[domain]
                try:
                    if previous_secret is None:
                        store.delete_secret()
                    else:
                        store.write_secret(previous_secret)
                except CredentialStoreError:
                    credential_rollback_ok = False
            # Restore enabled settings only after every credential is back in
            # its previous security domain. Otherwise retain the durable
            # all-disabled marker written before the first credential mutation.
            settings_rollback_ok = not disabled_saved
            if disabled_saved and credential_rollback_ok:
                try:
                    self.settings_repository.restore_snapshot(settings_snapshot)
                except SettingsError:
                    settings_rollback_ok = False
                else:
                    settings_rollback_ok = True
            rollback_ok = settings_rollback_ok and credential_rollback_ok
            if rollback_ok:
                self.settings.clear()
                self.settings.update(previous_document)
                self.model_provider_settings = ProviderSettings.from_mapping(
                    previous_document["model_providers"],
                    self.provider_catalog,
                )
                if router_replaced:
                    self.provider_router.replace_profiles(
                        self.model_provider_settings,
                        self.provider_catalog,
                        self._credential_store_for_profile,
                    )
            else:
                fail_closed_settings = ProviderSettings(
                    tuple(
                        replace(profile, enabled=False) for profile in candidate_settings.profiles
                    ),
                    candidate_settings.assignments,
                ).runtime_validated(self.provider_catalog)
                fail_closed_document = deepcopy(candidate_document)
                fail_closed_document["model_providers"] = fail_closed_settings.to_mapping()
                fail_closed_document["provider_enabled"] = False
                fail_closed_document["multimodal"]["enabled"] = False
                fail_closed_document["voice"]["enabled"] = False
                with suppress(SettingsError):
                    self.settings_repository.save(fail_closed_document)
                self.settings.clear()
                self.settings.update(fail_closed_document)
                self.model_provider_settings = fail_closed_settings
                self.provider_router.replace_profiles(
                    fail_closed_settings,
                    self.provider_catalog,
                    self._credential_store_for_profile,
                )
                self._chat_available = False
                self._voice_configured = False
                self.voice_session.stop_session(stop_chat=False)
                self.chat_panel.set_provider_mode("unconfigured")
                self._sync_voice_availability()
            self.logger.warning(
                "Model provider transaction failed error_type=%s "
                "settings_rollback=%s credential_rollback=%s",
                type(exc).__name__,
                settings_rollback_ok,
                credential_rollback_ok,
            )
            if not rollback_ok:
                self.model_settings_window.update_settings(
                    fail_closed_settings,
                    force=True,
                )
            self.model_settings_window.apply_save_result(
                success=False,
                message=(
                    "模型提供商保存失败，原 Profile、凭据和路由已保留。"
                    if rollback_ok
                    else "模型提供商保存或回滚失败；相关 Profile 已失效关闭，请重新配置。"
                ),
            )
            self.memory_jobs.resume()
            self.deep_memory_jobs.resume()
            return

        self.settings.clear()
        self.settings.update(candidate_document)
        self.model_provider_settings = candidate_settings
        self._settings_trusted = True
        voice_source = str(candidate_document["voice"].get("credential_source", ""))
        if voice_source.startswith("profile:") and voice_profile_changed:
            voice_store = self._speech_store_for_source(voice_source)
            if voice_store is None:
                self._voice_configured = False
            else:
                speech_config = self._speech_config_from_settings(candidate_document["voice"])
                speech_client = MiMoSpeechClient(speech_config, voice_store)
                if not self.speech_network.set_services(speech_client, speech_client):
                    self._voice_configured = False
                    self.logger.warning("Voice runtime was still busy after provider transaction")
                else:
                    self._voice_configured = bool(
                        candidate_document["voice"].get("enabled") is True
                        and self._profile_credential_available(
                            candidate_settings.profile(voice_source.removeprefix("profile:"))
                        )
                    )
        self.voice_settings_page.update_reusable_mimo_profiles(
            tuple(
                profile
                for profile in candidate_settings.profiles
                if self._mimo_profile_credential_compatible(profile)
                and profile.enabled
                and self._profile_credential_available(profile)
            ),
            selected_source=voice_source,
        )
        conversation = candidate_settings.assigned_profile(ProviderRole.CONVERSATION)
        self._chat_available = bool(
            conversation
            and conversation.enabled
            and conversation.is_tested(ProviderRole.CONVERSATION)
            and self._profile_credential_available(conversation)
        )
        self.provider_config = ProviderConfig.from_mapping(candidate_document["provider"])
        if conversation is not None and conversation.enabled:
            self.chat_panel.set_provider_mode(
                "provider",
                provider_name=(
                    f"{conversation.display_name} · "
                    f"{conversation.model_for(ProviderRole.CONVERSATION)}"
                ),
            )
            self.data_service.set_provider_metadata(
                conversation.display_name,
                conversation.model_for(ProviderRole.CONVERSATION),
            )
        else:
            self.chat_panel.set_provider_mode("unconfigured")
            self.data_service.set_provider_metadata(None, None)
        self.multimodal_config = ProviderConfig.from_mapping(
            candidate_document["multimodal"]["provider"]
        )
        if vision_profile is not None and vision_profile.enabled:
            self.data_service.set_multimodal_provider_metadata(
                vision_profile.display_name,
                vision_profile.model_for(ProviderRole.VISION),
            )
        else:
            self.data_service.set_multimodal_provider_metadata(None, None)
        self.model_settings_window.apply_save_result(
            success=True,
            message="模型 Profile、凭据和四任务路由已安全保存。",
        )
        self._sync_voice_availability()
        self._refresh_diagnostics()
        self.memory_jobs.resume()
        self.deep_memory_jobs.resume()

    @staticmethod
    def _provider_credential_domain(profile: ProviderProfile) -> str | None:
        """Return the code-owned security-domain identity without reading a secret."""

        if profile.auth is ProviderAuth.NONE:
            return None
        if profile.credential_slot in {"legacy_chat", "legacy_vision"}:
            return profile.credential_slot
        return (
            f"dynamic:{profile.profile_id}:{profile.protocol.value}:"
            f"{profile.credential_scope_digest}"
        )

    @staticmethod
    def _legacy_mirror_config(
        profile: ProviderProfile,
        role: ProviderRole,
        credential_ref: str,
    ) -> ProviderConfig:
        preset = (
            ProviderPreset.MIMO_PAYG
            if profile.catalog_id == "mimo_payg"
            else ProviderPreset.DEEPSEEK_PAYG
            if profile.catalog_id == "deepseek"
            else ProviderPreset.CUSTOM_OPENAI
        )
        auth = AuthMode.BEARER if profile.auth is ProviderAuth.BEARER else AuthMode.API_KEY
        token_field = (
            TokenLimitField.MAX_COMPLETION_TOKENS
            if profile.catalog_id in {"mimo_payg", "openai"}
            else TokenLimitField.MAX_TOKENS
        )
        return ProviderConfig.for_preset(
            preset,
            display_name=(
                ProviderConfig.for_preset(preset).display_name
                if preset is not ProviderPreset.CUSTOM_OPENAI
                else profile.display_name
            ),
            base_url=(
                ProviderConfig.for_preset(preset).base_url
                if preset is not ProviderPreset.CUSTOM_OPENAI
                else profile.base_url
            ),
            model=profile.model_for(role),
            auth_mode=(
                ProviderConfig.for_preset(preset).auth_mode
                if preset is not ProviderPreset.CUSTOM_OPENAI
                else auth
            ),
            credential_ref=credential_ref,
            connect_timeout_seconds=profile.connect_timeout_seconds,
            request_timeout_seconds=profile.request_timeout_seconds,
            max_output_tokens=min(profile.max_output_tokens, 32_768),
            temperature=profile.temperature,
            top_p=profile.top_p,
            stream_enabled=profile.stream_enabled,
            token_limit_field=token_field,
        )

    @classmethod
    def _legacy_mirror_config_or_none(
        cls,
        profile: ProviderProfile | None,
        role: ProviderRole,
        credential_ref: str,
    ) -> ProviderConfig | None:
        """Mirror only contracts the v9 compatibility schema can represent."""

        if (
            profile is None
            or profile.protocol.value != "openai_chat_completions"
            or not profile.base_url.startswith("https://")
            or profile.auth not in {ProviderAuth.BEARER, ProviderAuth.API_KEY}
        ):
            return None
        try:
            return cls._legacy_mirror_config(profile, role, credential_ref)
        except ValueError:
            return None

    @staticmethod
    def _mimo_credential_compatible(config: ProviderConfig) -> bool:
        return (
            config.preset is ProviderPreset.MIMO_PAYG
            and config.base_url == "https://api.xiaomimimo.com/v1"
            and config.auth_mode is AuthMode.API_KEY
        )

    @classmethod
    def _can_reuse_mimo_credential(
        cls,
        text_config: ProviderConfig,
        multimodal_config: ProviderConfig,
    ) -> bool:
        return bool(
            cls._mimo_credential_compatible(text_config)
            and cls._mimo_credential_compatible(multimodal_config)
            and text_config.credential_scope == multimodal_config.credential_scope
        )

    def _speech_store_for_source(self, source: str) -> CredentialStore | None:
        if source == "independent":
            return self.speech_credential_store
        if source.startswith("profile:"):
            profile = self.model_provider_settings.profile(source.removeprefix("profile:"))
            if (
                profile is None
                or not profile.enabled
                or not self._mimo_profile_credential_compatible(profile)
            ):
                return None
            return self._credential_store_for_profile(profile)
        if source == PROVIDER_CREDENTIAL_REF:
            return (
                self.credential_store
                if self._mimo_credential_compatible(self.provider_config)
                else None
            )
        if source == MULTIMODAL_CREDENTIAL_REF:
            if not self._mimo_credential_compatible(self.multimodal_config):
                return None
            return self._selected_multimodal_credential_store()
        return None

    @staticmethod
    def _mimo_profile_credential_compatible(profile: ProviderProfile) -> bool:
        return bool(
            profile.catalog_id == "mimo_payg"
            and profile.base_url == "https://api.xiaomimimo.com/v1"
            and profile.auth is ProviderAuth.API_KEY
        )

    @staticmethod
    def _speech_config_from_settings(settings: object) -> MiMoSpeechConfig:
        if not isinstance(settings, dict):
            raise ValueError("voice settings are invalid")
        return MiMoSpeechConfig(
            base_url=str(settings["base_url"]),
            asr_model=str(settings["asr_model"]),
            tts_model=str(settings["tts_model"]),
            tts_voice=str(settings["tts_voice"]),
            tts_format=str(settings["tts_format"]),
            connect_timeout_seconds=float(settings["connect_timeout_seconds"]),
            request_timeout_seconds=float(settings["request_timeout_seconds"]),
        ).validated()

    def _selected_multimodal_credential_store(self) -> CredentialStore | None:
        multimodal = self.settings.get("multimodal", {})
        if isinstance(multimodal, dict) and multimodal.get("reuse_mimo_credential") is True:
            return (
                self.credential_store
                if self._can_reuse_mimo_credential(
                    self.provider_config,
                    self.multimodal_config,
                )
                else None
            )
        return self.multimodal_credential_store

    def _set_text_provider(self, provider: ChatProvider) -> bool:
        if not self.conversation.set_provider(self.provider_router):
            return False
        self.provider_router.set_provider(ProviderCapability.TEXT, provider)
        return True

    def _save_multimodal_configuration(
        self,
        config_object: object,
        secret_object: object,
        enabled: bool,
        reuse_mimo_credential: bool,
    ) -> None:
        """Retired P7C compatibility entry; P7G uses Profile routing only."""

        del config_object, secret_object, enabled, reuse_mimo_credential
        self.model_settings_window.apply_save_result(
            success=False,
            message="旧多模态配置入口已停用，请在模型提供商中心编辑视觉任务。",
        )

    def _save_voice_configuration(
        self,
        voice_object: object,
        secret_object: object,
    ) -> None:
        if not isinstance(voice_object, dict):
            self.voice_settings_page.apply_save_result(
                success=False,
                message="语音设置无效。",
            )
            return
        candidate_voice = deepcopy(voice_object)
        candidate = deepcopy(self.settings)
        candidate["voice"] = candidate_voice
        disabled = deepcopy(candidate)
        disabled["voice"]["enabled"] = False
        try:
            # Full-document validation also rejects secret-like fields and
            # unsupported endpoints/models before any credential change.
            validate_settings_document(candidate)
            speech_config = self._speech_config_from_settings(candidate_voice)
        except (InvalidSettingsError, ValueError, KeyError, TypeError):
            self.voice_settings_page.apply_save_result(
                success=False,
                message="语音设置不符合固定的 MiMo PAYG 契约。",
            )
            return

        source = str(candidate_voice.get("credential_source", ""))
        selected_store = self._speech_store_for_source(source)
        enabled = candidate_voice.get("enabled") is True
        if enabled and selected_store is None:
            with suppress(SettingsError):
                self.settings_repository.save(self.settings)
            self.voice_settings_page.apply_save_result(
                success=False,
                message="所选凭据不是相同的 MiMo PAYG 安全域。",
            )
            return
        secret = secret_object if isinstance(secret_object, str) and secret_object else None
        previous_settings = deepcopy(self.settings)
        snapshot = self.settings_repository.capture_snapshot()
        try:
            previous_secret = self.speech_credential_store.read_secret()
        except CredentialStoreError:
            previous_secret = None
        wrote_secret = False
        self.voice_session.stop_session()
        try:
            self.settings_repository.save(disabled)
            if source == "independent" and secret is not None:
                self.speech_credential_store.write_secret(secret)
                wrote_secret = True
            if enabled:
                assert selected_store is not None
                if not selected_store.has_secret():
                    raise CredentialStoreError("speech credential is unavailable")
            runtime_store = selected_store or self.speech_credential_store
            client = MiMoSpeechClient(speech_config, runtime_store)
            if not self.speech_network.set_services(client, client):
                raise SettingsError("speech runtime is busy")
            self.settings_repository.save(candidate)
        except (CredentialStoreError, SettingsError, ValueError) as exc:
            with suppress(SettingsError):
                self.settings_repository.restore_snapshot(snapshot)
            if wrote_secret:
                with suppress(CredentialStoreError):
                    if previous_secret is None:
                        self.speech_credential_store.delete_secret()
                    else:
                        self.speech_credential_store.write_secret(previous_secret)
            self.settings.clear()
            self.settings.update(previous_settings)
            self.logger.warning(
                "Voice configuration save failed error_type=%s",
                type(exc).__name__,
            )
            self.voice_settings_page.apply_save_result(
                success=False,
                message="语音设置保存失败，原配置已保留。",
            )
            return
        self.settings.clear()
        self.settings.update(candidate)
        self._voice_configured = enabled
        self.voice_session.set_devices(
            str(candidate_voice["input_device_id"]),
            str(candidate_voice["output_device_id"]),
        )
        self._sync_voice_availability()
        self.voice_settings_page.apply_save_result(
            success=True,
            message=(
                "逐句语音已启用；采集仍需在聊天面板中显式开始。" if enabled else "逐句语音已停用。"
            ),
        )

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
        """Legacy test/programmatic adapter; the UI is no longer connected here.

        Retaining the proven v9 rollback path keeps pre-P7G recovery fixtures
        meaningful. It updates only the legacy compatibility mirrors; all P7G
        production UI and runtime routing use ``model_providers`` Profiles.
        """

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
        multimodal = self.settings.get("multimodal")
        if (
            isinstance(multimodal, dict)
            and multimodal.get("enabled") is True
            and multimodal.get("reuse_mimo_credential") is True
            and not self._can_reuse_mimo_credential(
                config_object,
                self.multimodal_config,
            )
        ):
            self.model_settings_window.apply_save_result(
                success=False,
                message="多模态正在复用对话 MiMo PAYG 密钥，请先调整多模态配置。",
            )
            return
        voice = self.settings.get("voice")
        if (
            isinstance(voice, dict)
            and voice.get("enabled") is True
            and voice.get("credential_source") == PROVIDER_CREDENTIAL_REF
            and not self._mimo_credential_compatible(config_object)
        ):
            self.model_settings_window.apply_save_result(
                success=False,
                message="语音正在复用对话 MiMo PAYG 密钥，请先调整语音配置。",
            )
            return
        if self._provider_switch_pending:
            self.model_settings_window.apply_save_result(
                success=False,
                message="已有模型配置正在安全保存，请稍候。",
            )
            return
        if self._conversation_switch_pending:
            self.model_settings_window.apply_save_result(
                success=False,
                message="会话正在更新，请稍候再保存模型配置。",
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
        deep_clean = self.deep_memory_jobs.pause(wait_ms=0)
        if deep_clean and self.memory_jobs.pause(wait_ms=0):
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

    def _on_visual_generation_idle(self) -> None:
        if self._pending_foreground_action is not None:
            self._dispatch_pending_foreground_action()

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
        self.deep_memory_jobs.resume()
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
                self.deep_memory_jobs.resume()
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
                if not self._set_text_provider(disabled_provider):
                    raise SettingsError("Provider could not be disabled while saving.")
                provider_switched = True
                if not self.background_generation.set_provider(disabled_provider):
                    raise SettingsError("Background provider could not be disabled while saving.")
                background_provider_switched = True
            if secret is not None:
                credential_write_attempted = True
                self.credential_store.write_secret(secret)
            if not self._mock_chat and not self._set_text_provider(candidate_provider):
                raise SettingsError("Provider could not be switched while saving.")
            if not self._mock_chat and not self.background_generation.set_provider(
                candidate_provider
            ):
                raise SettingsError("Background provider could not be switched while saving.")
            self.settings_repository.save(candidate_settings)
        except (CredentialStoreError, SettingsError, ValueError) as exc:
            if provider_switched:
                self._set_text_provider(disabled_provider)
            if background_provider_switched:
                self.background_generation.set_provider(disabled_provider)
            settings_restored, credential_restored = self._restore_provider_transaction(
                settings_snapshot,
                previous_secret,
                restore_settings=disabled_marker_saved,
                restore_credential=credential_write_attempted,
            )
            provider_restored = not provider_switched or self._set_text_provider(previous_provider)
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
                    self._set_text_provider(safe_provider)
                    self.background_generation.set_provider(safe_provider)
                    self._active_chat_provider = safe_provider
                    self.chat_panel.set_provider_mode("unconfigured")
                    self._sync_voice_availability()
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
        # This method is intentionally not connected to the P7G UI. Preserve
        # the legacy rollback fixture semantics without allowing this adapter
        # to replace the active task-level Profile router.
        if not self._mock_chat:
            self.conversation.set_provider(self.provider_router)
            self.background_generation.set_provider(self.provider_router)
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
        self._sync_voice_availability()
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

    def _import_attachment_paths(self, paths_object: object, source_object: object) -> None:
        if self._data_change_state != "idle":
            self.chat_panel.set_status("本地数据操作进行中，暂时不能添加附件。", kind="error")
            return
        try:
            paths = tuple(str(path) for path in paths_object)
            source = AttachmentSource(source_object)
        except (TypeError, ValueError):
            self.chat_panel.set_status("附件来源无效。", kind="error")
            return
        self.attachment_runtime.import_paths(
            paths,
            source=source,
            existing=self.chat_panel.draft_attachments,
        )

    def _import_attachment_image(
        self,
        image_object: object,
        display_name: str,
        source_object: object,
    ) -> None:
        if self._data_change_state != "idle":
            self.chat_panel.set_status("本地数据操作进行中，暂时不能添加附件。", kind="error")
            return
        if not isinstance(image_object, QImage) or image_object.isNull():
            self.chat_panel.set_status("剪贴板图片无效。", kind="error")
            return
        try:
            source = AttachmentSource(source_object)
        except ValueError:
            self.chat_panel.set_status("附件来源无效。", kind="error")
            return
        if self.region_screenshot_encoder.busy or self.attachment_runtime.busy:
            self.chat_panel.set_status("请等当前图片处理结束后再添加。", kind="error")
            return
        self.chat_panel.set_attachment_processing(True)
        self.chat_panel.set_status("正在后台处理剪贴板图片…")
        self.region_screenshot_encoder.submit(image_object, (display_name, source))

    def _on_attachment_imported(self, attachment_object: object) -> None:
        if not isinstance(attachment_object, AttachmentSnapshot):
            self.chat_panel.set_status("附件处理结果无效。", kind="error")
            return
        if self.chat_panel.add_draft_attachment(attachment_object):
            self.chat_panel.set_status("附件已在本地安全处理，可发送。", kind="success")

    def _on_context_attachment_imported(
        self,
        context_object: object,
        attachments_object: object,
    ) -> None:
        if not isinstance(context_object, _SendAction):
            return
        try:
            imported = tuple(attachments_object)
        except TypeError:
            imported = ()
        if len(imported) != 1 or not isinstance(imported[0], AttachmentSnapshot):
            self._on_context_attachment_failed(
                context_object,
                "实时画面处理结果无效。",
            )
            return
        action = replace(
            context_object,
            attachments=(*context_object.attachments, imported[0]),
            visual_sampled=True,
        )
        if not self._queue_send_action(action) and action.input_modality is InputModality.VOICE:
            token = self._pending_voice_token
            self._pending_voice_token = None
            if token is not None:
                self.voice_session.submission_failed(token)

    def _on_context_attachment_failed(
        self,
        context_object: object,
        message: str,
    ) -> None:
        self.chat_panel.set_status(message, kind="error")
        if (
            isinstance(context_object, _SendAction)
            and context_object.input_modality is InputModality.VOICE
        ):
            token = self._pending_voice_token
            self._pending_voice_token = None
            if token is not None:
                self.voice_session.submission_failed(token)

    def _start_visual_source(self, kind_value: str) -> None:
        if self._privacy_mode:
            self.chat_panel.set_status("请先退出隐私模式。", kind="error")
            return
        vision_profile = self.model_provider_settings.assigned_profile(ProviderRole.VISION)
        if (
            vision_profile is None
            or not vision_profile.enabled
            or not vision_profile.is_tested(ProviderRole.VISION)
            or not self._profile_credential_available(vision_profile)
        ):
            self.chat_panel.set_status(
                "请先在设置中配置并启用图片与视觉模型。",
                kind="error",
            )
            return
        try:
            kind = VisualSourceKind(kind_value)
        except ValueError:
            self.chat_panel.set_status("视觉来源无效。", kind="error")
            return
        visual = self.settings["visual"]
        source_id = str(
            visual[
                {
                    VisualSourceKind.SCREEN: "screen_id",
                    VisualSourceKind.WINDOW: "window_id",
                    VisualSourceKind.CAMERA: "camera_id",
                }[kind]
            ]
        )
        if kind is VisualSourceKind.WINDOW and not source_id:
            self.chat_panel.set_status("请先在视觉设置中选择窗口。", kind="error")
            return
        self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        if not self.visual_sources.start(kind, source_id):
            self.chat_panel.set_status("视觉来源无法启动。", kind="error")

    def _stop_visual_source(self) -> None:
        self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        self.visual_sources.stop()

    def _on_visual_source_failed(self, message: str) -> None:
        self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        self.chat_panel.set_status(message, kind="error")

    def _request_region_screenshot(self) -> None:
        if self._privacy_mode:
            self.chat_panel.set_status("请先退出隐私模式再截图。", kind="error")
            return
        if (
            self._data_change_state != "idle"
            or self.attachment_runtime.busy
            or self.region_screenshot_encoder.busy
            or self.conversation.is_active
        ):
            self.chat_panel.set_status("请等当前处理结束后再截图。", kind="error")
            return
        from amadeus_desktop.ui.region_screenshot import RegionScreenshotOverlay

        self.hide_chat()
        overlay = RegionScreenshotOverlay()
        self._region_screenshot_overlay = overlay
        overlay.captured.connect(self._on_region_screenshot_captured)
        overlay.cancelled.connect(self._on_region_screenshot_cancelled)
        overlay.destroyed.connect(lambda: setattr(self, "_region_screenshot_overlay", None))
        QTimer.singleShot(150, overlay, overlay.begin)

    def _on_region_screenshot_captured(self, payload_object: object) -> None:
        self.show_chat()
        if not isinstance(payload_object, QImage) or payload_object.isNull():
            self.chat_panel.set_status("截图结果无效。", kind="error")
            return
        self.chat_panel.set_status("正在后台处理区域截图…")
        self.chat_panel.set_attachment_processing(True)
        self.region_screenshot_encoder.submit(
            payload_object,
            ("region-screenshot.png", AttachmentSource.SCREENSHOT),
        )

    def _on_region_screenshot_encoded(self, result_object: object) -> None:
        try:
            display_name, source_object, payload_object = tuple(result_object)
        except (TypeError, ValueError):
            self.chat_panel.set_attachment_processing(False)
            self.chat_panel.set_status("截图处理结果无效。", kind="error")
            return
        try:
            source = AttachmentSource(source_object)
        except ValueError:
            source = None
        if (
            not isinstance(display_name, str)
            or not isinstance(payload_object, bytes)
            or source is None
        ):
            self.chat_panel.set_attachment_processing(False)
            self.chat_panel.set_status("截图处理结果无效。", kind="error")
            return
        self.chat_panel.set_attachment_processing(False)
        self.attachment_runtime.import_bytes(
            payload_object,
            display_name=display_name,
            source=source,
            existing=self.chat_panel.draft_attachments,
        )

    def _on_attachment_image_encode_failed(self, message: str) -> None:
        self.chat_panel.set_attachment_processing(False)
        self.chat_panel.set_status(message, kind="error")

    def _on_region_screenshot_cancelled(self) -> None:
        self.show_chat()
        self.chat_panel.set_status("已取消区域截图。")

    def _set_privacy_mode(self, enabled: bool) -> None:
        self._privacy_mode = bool(enabled)
        self.chat_panel.set_privacy_mode(self._privacy_mode)
        if not self._privacy_mode:
            return
        self.proactive_interactions.cancel_ai_generation(wait_ms=0)
        self.visual_sources.privacy_stop()
        self.region_screenshot_encoder.cancel()
        self.chat_panel.set_attachment_processing(False)
        self.attachment_runtime.cancel_all()
        overlay = self._region_screenshot_overlay
        if overlay is not None:
            overlay.close()
            self._region_screenshot_overlay = None
        self.voice_session.stop_session()
        self._pending_voice_token = None
        if self._active_visual_turn_id is not None:
            self.conversation.stop()
        self.chat_panel.set_status("隐私模式已停止并清空实时采集。", kind="success")

    def _save_visual_configuration(self, visual_object: object) -> None:
        if not isinstance(visual_object, dict):
            self.visual_settings_page.apply_save_result(
                success=False,
                message="视觉设置无效。",
            )
            return
        candidate = deepcopy(self.settings)
        candidate["visual"] = deepcopy(visual_object)
        try:
            self.settings_repository.save(candidate)
        except SettingsError as exc:
            self.logger.warning(
                "Visual settings save failed error_type=%s",
                type(exc).__name__,
            )
            self.visual_settings_page.apply_save_result(
                success=False,
                message="视觉设置保存失败。",
            )
            return
        self._stop_visual_source()
        self.settings.clear()
        self.settings.update(candidate)
        index = self.chat_panel.visual_source_combo.findData(
            str(candidate["visual"]["preferred_source"])
        )
        if index >= 0:
            self.chat_panel.visual_source_combo.setCurrentIndex(index)
        self.visual_settings_page.apply_save_result(
            success=True,
            message="视觉设置已保存；本次启动不会自动开始采集。",
        )

    def _set_hands_free_session(self, enabled: bool) -> None:
        if enabled:
            if not (
                not self._privacy_mode
                and self._voice_configured
                and self._chat_available
                and self._data_initialized
                and self._data_writable
                and self._data_change_state == "idle"
                and self.settings["voice"]["hands_free_enabled"] is True
            ):
                self.chat_panel.set_hands_free_checked(False)
                self.chat_panel.set_voice_status("语音尚未安全配置。", True)
                return
            if not self.voice_session.start_hands_free():
                self.chat_panel.set_hands_free_checked(False)
        else:
            self.voice_session.stop_session()

    def _press_to_talk(self) -> None:
        if not (
            not self._privacy_mode
            and self._voice_configured
            and self._chat_available
            and self._data_initialized
            and self._data_writable
            and self._data_change_state == "idle"
        ):
            self.chat_panel.set_voice_status("语音尚未安全配置或当前不可用。", True)
            return
        self.voice_session.press_to_talk()

    def _on_voice_transcript(self, transcript: str, token_object: object) -> None:
        if not isinstance(token_object, SpeechToken):
            return
        self._pending_voice_token = token_object
        if not self._send_chat_message(transcript, (), InputModality.VOICE):
            self._pending_voice_token = None
            self.voice_session.submission_failed(token_object)

    def _on_voice_chat_chunk(
        self,
        _request_id: str,
        turn_id: str,
        chunk: str,
    ) -> None:
        self.voice_session.on_chat_chunk(turn_id, chunk)

    def _on_voice_chat_finished(
        self,
        _request_id: str,
        turn_object: object,
        state_object: object,
    ) -> None:
        turn_id = getattr(turn_object, "turn_id", None)
        if not isinstance(turn_id, str):
            return
        try:
            state = ConversationState(state_object)
        except (TypeError, ValueError):
            return
        self.voice_session.on_chat_finished(turn_id, state)

    def _stop_voice_chat(self) -> None:
        action = self._pending_foreground_action
        if (
            action is not None
            and action[0] == "send"
            and isinstance(action[1], _SendAction)
            and action[1].input_modality is InputModality.VOICE
        ):
            self._pending_foreground_action = None
            self._foreground_lane_timer.stop()
            self.chat_panel.set_foreground_preparing(False)
            self._set_foreground_lane_active(False)
        self._pending_voice_token = None
        self.conversation.stop()

    def _sync_voice_availability(self) -> None:
        available = bool(
            self._voice_configured
            and self._chat_available
            and self._data_initialized
            and self._data_writable
            and not self._exiting
            and self._data_change_state == "idle"
        )
        voice_settings = self.settings.get("voice", {})
        hands_free_available = bool(
            isinstance(voice_settings, dict) and voice_settings.get("hands_free_enabled") is True
        )
        self.chat_panel.set_voice_available(
            available,
            hands_free_available=hands_free_available,
        )
        if not available and self.voice_session.state.value != "off":
            self.voice_session.stop_session()

    def _send_chat_message(
        self,
        text: str,
        attachments_object: object = (),
        input_modality: InputModality = InputModality.TEXT,
    ) -> bool:
        try:
            attachments = tuple(attachments_object)
            if any(not isinstance(item, AttachmentSnapshot) for item in attachments):
                raise TypeError
            input_modality = InputModality(input_modality)
        except (TypeError, ValueError):
            self.chat_panel.set_status("附件或输入模态无效，消息未发送。", kind="error")
            return False
        action = _SendAction(text, attachments, input_modality)
        return self._queue_send_action(action)

    def _queue_send_action(self, action: _SendAction) -> bool:
        if self._data_change_state != "idle":
            self.chat_panel.set_status("本地数据操作进行中，消息未发送。", kind="error")
            return False
        if self._provider_switch_pending:
            self.chat_panel.set_status("对话模型切换中，请稍候。", kind="error")
            return False
        if not self._chat_available:
            self.chat_panel.set_status("请先配置并测试对话模型。", kind="error")
            return False
        if not self._data_initialized:
            self._pending_initial_message = action
            self.chat_panel.set_status("正在初始化本地聊天数据，稍后会自动发送。")
            return True
        if not self._data_writable:
            self.chat_panel.set_status("本地数据当前无法安全写入，消息未发送。", kind="error")
            return False
        if self._conversation_switch_pending:
            self.chat_panel.set_status("会话切换中，请稍候。", kind="error")
            return False
        if self.visual_sources.active and not action.visual_sampled:
            frame = self.visual_sources.latest()
            if frame is None:
                self.chat_panel.set_status(
                    "实时视觉尚未产生可用画面，请稍候后重试。",
                    kind="error",
                )
                return False
            if len(action.attachments) >= 5:
                self.chat_panel.set_status(
                    "当前消息已含 5 个附件，无法再加入实时画面。",
                    kind="error",
                )
                return False
            source = AttachmentSource(frame.source_kind.value)
            accepted = self.attachment_runtime.import_bytes(
                frame.png_bytes,
                display_name=f"{frame.source_kind.value}-latest.png",
                source=source,
                existing=action.attachments,
                context=action,
            )
            if accepted:
                self.chat_panel.set_status("正在固定本轮最新画面…")
            return accepted
        return self._begin_foreground_action("send", action)

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

    def _begin_foreground_action(self, kind: str, payload: object) -> bool:
        """Give visible chat exclusive provider priority without blocking Qt."""

        if self._pending_foreground_action is not None:
            self.chat_panel.set_status("正在为前台对话让出模型资源，请稍候。")
            return False
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
            and not self.visual_background_generation.is_running
        ):
            self._dispatch_pending_foreground_action()
        return True

    def _dispatch_pending_foreground_action(self) -> None:
        action = self._pending_foreground_action
        if action is None:
            return
        if self.background_generation.is_running or self.visual_background_generation.is_running:
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
            if isinstance(payload, _SendAction) and payload.input_modality is InputModality.VOICE:
                token = self._pending_voice_token
                self._pending_voice_token = None
                if token is not None:
                    self.voice_session.submission_failed(token)
            self.chat_panel.set_foreground_preparing(False)
            self._set_foreground_lane_active(False)
            self.chat_panel.set_status("当前状态已变化，消息未发送。", kind="error")
            return

        started = time.perf_counter()
        focus_decision = self._focus_decision_for_action(kind, payload)
        self._active_focus_decision = focus_decision
        first_chunk_timeout_ms = (
            self._focused_first_chunk_timeout_ms if focus_decision.active else None
        )
        if kind == "send":
            if not isinstance(payload, _SendAction):
                self.chat_panel.set_foreground_preparing(False)
                self._set_foreground_lane_active(False)
                return
            turn = self.conversation.send_message(
                payload.text,
                first_chunk_timeout_ms=first_chunk_timeout_ms,
                attachments=payload.attachments,
                input_modality=payload.input_modality,
            )
            if turn is not None:
                self._turn_started_at[turn.turn_id] = started
                if payload.visual_sampled:
                    self._active_visual_turn_id = turn.turn_id
                if payload.input_modality is InputModality.VOICE:
                    token = self._pending_voice_token
                    self._pending_voice_token = None
                    if token is None or not self.voice_session.bind_chat_turn(
                        token,
                        turn.turn_id,
                    ):
                        self.voice_session.stop_session()
                return
        elif (
            kind == "retry"
            and isinstance(payload, str)
            and self.conversation.retry(
                payload,
                first_chunk_timeout_ms=first_chunk_timeout_ms,
            )
        ):
            self._turn_started_at[payload] = started
            return

        self._active_focus_decision = FocusModeDecision()
        self.chat_panel.set_foreground_preparing(False)
        self._set_foreground_lane_active(False)
        message = "当前无法重试这轮对话。" if kind == "retry" else "消息未能发送，请重试。"
        self.chat_panel.set_status(message, kind="error")
        if isinstance(payload, _SendAction) and payload.input_modality is InputModality.VOICE:
            token = self._pending_voice_token
            self._pending_voice_token = None
            if token is not None:
                self.voice_session.submission_failed(token)

    def _on_foreground_lane_timeout(self) -> None:
        if self._pending_foreground_action is None:
            return
        kind, payload = self._pending_foreground_action
        self._pending_foreground_action = None
        self.chat_panel.set_foreground_preparing(False)
        self._set_foreground_lane_active(False)
        self.chat_panel.set_status("后台生成未能及时停止，消息未发送，请重试。", kind="error")
        if (
            kind == "send"
            and isinstance(payload, _SendAction)
            and payload.input_modality is InputModality.VOICE
        ):
            token = self._pending_voice_token
            self._pending_voice_token = None
            if token is not None:
                self.voice_session.submission_failed(token)

    def _set_foreground_lane_active(self, active: bool) -> None:
        if self._background_jobs_enabled:
            self.deep_memory_jobs.set_foreground_active(active)
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

        turn_id = getattr(turn, "turn_id", "unknown")
        if str(turn_id) == self._active_visual_turn_id:
            self._active_visual_turn_id = None
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
        finalized_metadata = self.data_service.pop_finalized_provider_metadata(str(turn_id))
        if finalized_metadata is not None:
            provider_name, model_name = finalized_metadata
        elif self.chat_panel.provider_mode == "mock":
            provider_name = "explicit_mock"
            model_name = "scripted"
        else:
            snapshot = self.provider_router.captured_snapshot(str(request_id))
            if snapshot is not None:
                provider_name = snapshot.provider_name
                model_name = snapshot.model
            else:
                profile = self.model_provider_settings.assigned_profile(ProviderRole.CONVERSATION)
                provider_name = profile.display_name if profile is not None else None
                model_name = (
                    profile.model_for(ProviderRole.CONVERSATION) if profile is not None else None
                )
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

    def _bind_captured_provider_metadata(self, request_id: str, turn_id: str, _chunk: str) -> None:
        """Use the first routed chunk to replace pre-request provider metadata."""

        snapshot = self.provider_router.captured_snapshot(str(request_id))
        if snapshot is not None:
            self.data_service.bind_turn_provider_metadata(
                str(turn_id),
                snapshot.provider_name,
                snapshot.model,
            )

    def _on_conversation_state_changed(self, state: ConversationState) -> None:
        # The runner is also used by opt-in proactive greetings even when the
        # durable memory scheduler is disabled by a test/development injection.
        self._set_foreground_lane_active(state is not ConversationState.IDLE)
        focus_active = self._active_focus_decision.active and state in {
            ConversationState.SENDING,
            ConversationState.WAITING_FIRST_CHUNK,
        }
        self.chat_panel.set_conversation_state(
            state,
            FOCUS_STATUS_TEXT if focus_active else None,
            focus_mode=focus_active,
        )
        animation = self.pet_window.animation
        if state is ConversationState.SENDING:
            animation.clear_transient()
            animation.set_activity("responding", False)
            animation.set_activity("waiting", not focus_active)
            animation.set_activity("thinking", focus_active)
        elif state is ConversationState.WAITING_FIRST_CHUNK:
            animation.set_activity("responding", False)
            animation.set_activity("waiting", not focus_active)
            animation.set_activity("thinking", focus_active)
        elif state is ConversationState.STREAMING:
            animation.set_activity("thinking", False)
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
        if state not in {
            ConversationState.SENDING,
            ConversationState.WAITING_FIRST_CHUNK,
        }:
            self._active_focus_decision = FocusModeDecision()

    def _clear_conversation_activities(self) -> None:
        self.pet_window.animation.set_activity("thinking", False)
        self.pet_window.animation.set_activity("waiting", False)
        self.pet_window.animation.set_activity("responding", False)

    def _focus_decision_for_action(self, kind: str, payload: object) -> FocusModeDecision:
        if kind == "send" and isinstance(payload, _SendAction):
            return classify_focus_mode(payload.text)
        if kind == "retry" and isinstance(payload, str):
            for turn in self.conversation.turns:
                if turn.turn_id == payload:
                    return classify_focus_mode(turn.user_message.content)
        return FocusModeDecision()

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
        visual_generation_clean = self.visual_background_generation.shutdown(wait_ms=slice_ms(0.05))
        voice_clean = self.voice_session.shutdown(wait_ms=slice_ms(0.10))
        self.visual_sources.shutdown()
        self.region_screenshot_encoder.shutdown()
        overlay = self._region_screenshot_overlay
        if overlay is not None:
            overlay.close()
            self._region_screenshot_overlay = None
        attachment_clean = self.attachment_runtime.shutdown(wait_ms=slice_ms(0.05))
        deep_background_clean = self.deep_memory_jobs.shutdown(wait_ms=0)
        background_clean = self.memory_jobs.shutdown(wait_ms=slice_ms(0.15))
        # Start cancellation for the connection test before waiting on conversation cleanup.
        self.model_settings_window.cancel_test()
        conversation_clean = self.conversation.shutdown(wait_ms=slice_ms(0.20))
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
        if not attachment_clean:
            self.logger.error("Attachment preprocessing did not stop within the deadline")
        if not voice_clean:
            self.logger.error("Voice workers did not stop within the shutdown deadline")
        if not visual_generation_clean:
            self.logger.error("Active visual worker did not stop within the shutdown deadline")
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
            and visual_generation_clean
            and voice_clean
            and attachment_clean
            and deep_background_clean
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
