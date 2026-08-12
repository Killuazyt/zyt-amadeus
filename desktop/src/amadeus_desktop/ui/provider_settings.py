"""P7G profile CRUD, task assignments and privacy-safe connection tests."""

from __future__ import annotations

import asyncio
import base64
import hmac
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    ProviderConnectionTester,
)
from amadeus_desktop.credential_store import CredentialStore, CredentialStoreError
from amadeus_desktop.provider_catalog import (
    CachePolicy,
    ProviderAuth,
    ProviderCatalog,
    ProviderProtocol,
    ProviderRole,
)
from amadeus_desktop.provider_profiles import (
    MAX_PROVIDER_PROFILES,
    ProviderProfile,
    ProviderProfileError,
    ProviderSettings,
    normalize_provider_base_url,
    request_snapshot,
    transient_credential_fingerprint,
)

_ROLE_LABELS = {
    ProviderRole.CONVERSATION: "主对话 / 纯文字主动问候",
    ProviderRole.SUMMARY: "会话摘要",
    ProviderRole.MEMORY: "记忆提炼 / 证据 / 反思 / 人格",
    ProviderRole.VISION: "图片 / 显式视觉 / 主动视觉",
}
_AUTH_LABELS = {
    ProviderAuth.BEARER: "Bearer",
    ProviderAuth.API_KEY: "api-key",
    ProviderAuth.ANTHROPIC_X_API_KEY: "Anthropic x-api-key",
    ProviderAuth.NONE: "无鉴权（仅本机回环）",
}
_PROTOCOL_LABELS = {
    ProviderProtocol.OPENAI_CHAT_COMPLETIONS: "OpenAI Chat Completions",
    ProviderProtocol.ANTHROPIC_MESSAGES: "Anthropic Messages",
}
_TEST_PIXEL = (
    "data:image/png;base64,"
    + base64.b64encode(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf"
        b"\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    ).decode("ascii")
)


@dataclass(frozen=True, slots=True)
class ProviderSettingsChange:
    settings: ProviderSettings
    secret_updates: Mapping[str, str] = field(repr=False)
    tested_secret_fingerprints: Mapping[str, str] = field(repr=False)
    deleted_profiles: tuple[ProviderProfile, ...]


class _ProfileTestWorker(QObject):
    succeeded = Signal(str, str, str, str, int)
    failed = Signal(str, str, str)
    cancelled = Signal(str, str)
    done = Signal()

    def __init__(
        self,
        tester: ProviderConnectionTester,
        profile: ProviderProfile,
        role: ProviderRole,
        catalog: ProviderCatalog,
        secret: str,
        cancellation: CancellationToken,
    ) -> None:
        super().__init__()
        self._tester = tester
        self._profile = profile
        self._role = role
        self._catalog = catalog
        self._secret = secret
        self._cancellation = cancellation
        self._request_fingerprint = profile.test_fingerprint(role)
        self._credential_fingerprint = transient_credential_fingerprint(secret)

    @Slot()
    def run(self) -> None:
        try:
            result = asyncio.run(
                self._run_test()
            )
            self.succeeded.emit(
                self._profile.profile_id,
                self._role.value,
                self._request_fingerprint,
                self._credential_fingerprint,
                result.elapsed_ms,
            )
        except CancellationRequested:
            self.cancelled.emit(self._profile.profile_id, self._role.value)
        except ChatProviderError as exc:
            self.failed.emit(self._profile.profile_id, self._role.value, exc.safe_message)
        except Exception:
            self.failed.emit(self._profile.profile_id, self._role.value, "连接测试失败。")
        finally:
            self._secret = ""
            self.done.emit()

    async def _run_test(self):
        snapshot = request_snapshot(self._profile, self._role, self._catalog)
        test_profile = getattr(self._tester, "test_profile", None)
        if callable(test_profile):
            return await test_profile(
                snapshot,
                self._secret,
                self._cancellation,
                visual_data_url=_TEST_PIXEL if self._role is ProviderRole.VISION else None,
            )
        # Compatibility for injected P4 test doubles; production always uses
        # ProviderConnectionTester.test_profile and the exact captured snapshot.
        legacy_test = getattr(self._tester, "test", None)
        if not callable(legacy_test) or snapshot.protocol is not ProviderProtocol.OPENAI_CHAT_COMPLETIONS:
            raise RuntimeError("unsupported injected provider tester")
        from amadeus_desktop.provider_config import AuthMode, ProviderConfig, ProviderPreset

        config = ProviderConfig.for_preset(
            ProviderPreset.CUSTOM_OPENAI,
            display_name=snapshot.provider_name,
            base_url=snapshot.base_url,
            model=snapshot.model,
            auth_mode=(
                AuthMode.BEARER if snapshot.auth is ProviderAuth.BEARER else AuthMode.API_KEY
            ),
            max_output_tokens=min(snapshot.max_output_tokens, 32_768),
            temperature=snapshot.temperature,
            top_p=snapshot.top_p,
            stream_enabled=snapshot.stream_enabled,
        )
        return await legacy_test(config, self._secret, self._cancellation)


class ProviderSettingsPage(QWidget):
    """One model-provider center replacing the old text/vision split pages."""

    save_requested = Signal(object)

    def __init__(
        self,
        settings: ProviderSettings,
        catalog: ProviderCatalog,
        *,
        credential_store_factory: Callable[[ProviderProfile], CredentialStore],
        tester: ProviderConnectionTester | None = None,
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self._settings = settings
        self._credential_store_factory = credential_store_factory
        self._tester = tester or ProviderConnectionTester()
        self._profiles = list(settings.profiles)
        self._assignments = dict(settings.assignments)
        self._secret_updates: dict[str, str] = {}
        self._tested_secret_fingerprints: dict[str, str] = {}
        self._deleted_profiles: list[ProviderProfile] = []
        self._loading = False
        self._saving = False
        self._test_thread: QThread | None = None
        self._test_worker: _ProfileTestWorker | None = None
        self._test_cancel: CancellationToken | None = None

        self.profile_list = QListWidget()
        self.profile_list.setAccessibleName("模型提供商 Profile")
        self.add_button = QPushButton("新增")
        self.copy_button = QPushButton("复制")
        self.delete_button = QPushButton("删除")
        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.add_button)
        list_buttons.addWidget(self.copy_button)
        list_buttons.addWidget(self.delete_button)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel(f"Profile（最多 {MAX_PROVIDER_PROFILES} 个）"))
        left_layout.addWidget(self.profile_list, 1)
        left_layout.addLayout(list_buttons)

        self.catalog_combo = QComboBox()
        for entry in catalog.all():
            self.catalog_combo.addItem(entry.display_name, entry.catalog_id)
        self.name_edit = QLineEdit()
        self.enabled_check = QCheckBox("启用此 Profile")
        self.protocol_value = QLabel()
        self.capability_disclosure = QLabel()
        self.capability_disclosure.setWordWrap(True)
        self.endpoint_combo = QComboBox()
        self.endpoint_edit = QLineEdit()
        self.auth_value = QComboBox()
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setClearButtonEnabled(True)
        self.secret_edit.setPlaceholderText("留空复用当前安全域中已保存的密钥")
        self.role_models: dict[ProviderRole, QLineEdit] = {
            role: QLineEdit() for role in ProviderRole
        }
        self.connect_timeout = QSpinBox()
        self.connect_timeout.setRange(1, 60)
        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(5, 300)
        self.first_chunk_timeout = QSpinBox()
        self.first_chunk_timeout.setRange(1, 120)
        self.idle_timeout = QSpinBox()
        self.idle_timeout.setRange(1, 120)
        self.output_limit = QSpinBox()
        self.output_limit.setRange(1, 131_072)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setDecimals(2)
        self.top_p = QDoubleSpinBox()
        self.top_p.setRange(0.01, 1.0)
        self.top_p.setDecimals(2)
        self.stream_check = QCheckBox("流式输出")
        self.cache_check = QCheckBox("显式启用当前 Profile 的提示词缓存")
        self.cache_disclosure = QLabel()
        self.cache_disclosure.setWordWrap(True)
        self.test_role = QComboBox()
        for role, label in _ROLE_LABELS.items():
            self.test_role.addItem(label, role.value)
        self.test_button = QPushButton("测试当前任务模型")
        self.assignments: dict[ProviderRole, QComboBox] = {
            role: QComboBox() for role in ProviderRole
        }
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status_label = self.status
        self.save_button = QPushButton("保存全部 Profile 与任务分配")

        form = QFormLayout()
        form.addRow("目录类型", self.catalog_combo)
        form.addRow("显示名称", self.name_edit)
        form.addRow("状态", self.enabled_check)
        form.addRow("协议", self.protocol_value)
        form.addRow("能力注册", self.capability_disclosure)
        endpoint_row = QWidget()
        endpoint_layout = QVBoxLayout(endpoint_row)
        endpoint_layout.setContentsMargins(0, 0, 0, 0)
        endpoint_layout.addWidget(self.endpoint_combo)
        endpoint_layout.addWidget(self.endpoint_edit)
        form.addRow("Base URL", endpoint_row)
        form.addRow("鉴权", self.auth_value)
        form.addRow("API 密钥", self.secret_edit)
        for role, label in _ROLE_LABELS.items():
            form.addRow(f"{label}模型", self.role_models[role])
        form.addRow("连接超时（秒）", self.connect_timeout)
        form.addRow("总请求超时（秒）", self.request_timeout)
        form.addRow("首包超时建议（秒）", self.first_chunk_timeout)
        form.addRow("流空闲超时（秒）", self.idle_timeout)
        form.addRow("输出上限", self.output_limit)
        form.addRow("temperature", self.temperature)
        form.addRow("top_p", self.top_p)
        form.addRow("", self.stream_check)
        form.addRow("", self.cache_check)
        form.addRow("缓存披露", self.cache_disclosure)
        form.addRow("连接测试任务", self.test_role)
        form.addRow("", self.test_button)
        for role, label in _ROLE_LABELS.items():
            form.addRow(f"{label}分配", self.assignments[role])
        form.addRow("", self.status)
        form.addRow("", self.save_button)

        content = QWidget()
        content.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        catalog_note = QLabel(
            "静态目录不联网更新、不会调用 /models。内置协议/端点/鉴权锁定；模型名可编辑。"
            "文档附件仍由 Amadeus 本地提取为文本，不代表供应商支持原生文件上传。"
            "Ollama、LM Studio 与 vLLM 仅提供回环地址模板；Amadeus 不安装、启动或管理本地模型服务。"
        )
        catalog_note.setWordWrap(True)
        if catalog.degraded:
            catalog_note.setText(
                "内置提供商目录损坏，当前仅保留安全自定义恢复入口；已禁止使用损坏预设。"
            )
        layout.addWidget(catalog_note)
        layout.addWidget(splitter, 1)

        self.profile_list.currentRowChanged.connect(self._on_profile_selected)
        self.add_button.clicked.connect(self._add_profile)
        self.copy_button.clicked.connect(self._copy_profile)
        self.delete_button.clicked.connect(self._delete_profile)
        self.catalog_combo.currentIndexChanged.connect(self._catalog_changed)
        self.endpoint_combo.currentIndexChanged.connect(self._endpoint_choice_changed)
        self.auth_value.currentIndexChanged.connect(self._auth_changed)
        self.test_button.clicked.connect(self._start_test)
        self.test_role.currentIndexChanged.connect(self._test_role_changed)
        self.save_button.clicked.connect(self._save)
        for combo in self.assignments.values():
            combo.currentIndexChanged.connect(self._assignments_changed)
        for widget in (
            self.name_edit,
            self.endpoint_edit,
            self.secret_edit,
            *self.role_models.values(),
        ):
            widget.textChanged.connect(self._editor_changed)
        for widget in (
            self.enabled_check,
            self.stream_check,
            self.cache_check,
        ):
            widget.toggled.connect(self._editor_changed)
        for widget in (
            self.connect_timeout,
            self.request_timeout,
            self.first_chunk_timeout,
            self.idle_timeout,
            self.output_limit,
            self.temperature,
            self.top_p,
        ):
            widget.valueChanged.connect(self._editor_changed)
        self._refresh_lists(select=0)

    @property
    def test_running(self) -> bool:
        return self._test_thread is not None

    def cancel_test(self) -> None:
        if self._test_cancel is not None:
            self._test_cancel.cancel()

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        self.cancel_test()
        thread = self._test_thread
        return thread is None or not thread.isRunning() or thread.wait(max(0, wait_ms))

    def apply_save_result(self, *, success: bool, message: str) -> None:
        self._saving = False
        if success:
            self._settings = ProviderSettings(
                tuple(self._profiles), MappingProxyType(dict(self._assignments))
            )
            self._secret_updates.clear()
            self._tested_secret_fingerprints.clear()
            self._deleted_profiles.clear()
            self.secret_edit.clear()
            self.secret_edit.setPlaceholderText("留空复用当前安全域中已保存的密钥")
        self.status.setText(message)
        self._sync_buttons()

    def update_settings(
        self,
        settings: ProviderSettings,
        *,
        force: bool = False,
    ) -> None:
        """Replace the page snapshot after an external migration/restore."""

        if self.test_running or (self._saving and not force):
            return
        if force:
            self._saving = False
        self._settings = settings
        self._profiles = list(settings.profiles)
        self._assignments = dict(settings.assignments)
        self._secret_updates.clear()
        self._tested_secret_fingerprints.clear()
        self._deleted_profiles.clear()
        self._refresh_lists(select=0)

    def require_new_secret(self) -> None:
        """Compatibility-safe fail-closed marker used by rollback recovery."""

        self._secret_updates.clear()
        self._tested_secret_fingerprints.clear()
        self.secret_edit.clear()
        self.status.setText("请输入对应的新 API 密钥并重新完成连接测试。")

    def _refresh_lists(self, *, select: int | None = None) -> None:
        current = self.profile_list.currentRow() if select is None else select
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for profile in self._profiles:
            roles = [
                role.value for role, profile_id in self._assignments.items()
                if profile_id == profile.profile_id
            ]
            state = "启用" if profile.enabled else "停用"
            tested = sum(
                1
                for role in ProviderRole
                if profile.model_for(role) and profile.is_tested(role)
            )
            configured = sum(1 for role in ProviderRole if profile.model_for(role))
            entry = self.catalog.entries.get(profile.catalog_id)
            cache_state = (
                "缓存：开"
                if profile.cache_enabled
                else "缓存：供应商自动"
                if entry is not None and entry.cache_policy is CachePolicy.UPSTREAM_AUTO
                else "缓存：关"
            )
            item = QListWidgetItem(
                f"{profile.display_name}\n{state} · {profile.protocol.value}"
                f" · 测试 {tested}/{configured} · {cache_state}"
                + (f" · {','.join(roles)}" if roles else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id)
            self.profile_list.addItem(item)
        self.profile_list.blockSignals(False)
        self._refresh_assignment_combos()
        if self._profiles:
            self.profile_list.setCurrentRow(max(0, min(current, len(self._profiles) - 1)))
        else:
            self.profile_list.setCurrentRow(-1)
            self.status.setText("当前没有 Profile；可新增一个，或保存全部任务均未分配的状态。")
            self._sync_buttons()

    def _refresh_assignment_combos(self) -> None:
        for role, combo in self.assignments.items():
            current = self._assignments.get(role)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("未分配", None)
            for profile in self._profiles:
                entry = self.catalog.entries.get(profile.catalog_id)
                if role is ProviderRole.VISION and (
                    entry is None or not entry.capabilities.image_input
                ):
                    if profile.profile_id != current:
                        continue
                    combo.addItem(
                        f"{profile.display_name}（未登记图片能力）",
                        profile.profile_id,
                    )
                    continue
                label = profile.display_name
                if not profile.model_for(role):
                    label = f"{label}（未填写模型）"
                combo.addItem(label, profile.profile_id)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    @Slot(int)
    def _on_profile_selected(self, row: int) -> None:
        if not 0 <= row < len(self._profiles):
            return
        self._load_profile(self._profiles[row])

    def _load_profile(self, profile: ProviderProfile) -> None:
        entry = self.catalog.entries.get(profile.catalog_id)
        if entry is None:
            self._loading = True
            try:
                # Do not leave the combobox pointing at an unrelated recovery
                # entry.  The user must make an explicit catalog choice before
                # this preserved Profile can be converted and saved.
                self.catalog_combo.setCurrentIndex(-1)
                self.name_edit.setText(profile.display_name)
                self.enabled_check.setChecked(False)
                self.enabled_check.setEnabled(False)
                self.protocol_value.setText(_PROTOCOL_LABELS[profile.protocol])
                self.capability_disclosure.setText("目录契约不可用；运行时已失效关闭。")
                self.auth_value.clear()
                self.auth_value.addItem(_AUTH_LABELS[profile.auth], profile.auth.value)
                self.auth_value.setEnabled(False)
                self.endpoint_combo.hide()
                self.endpoint_edit.show()
                self.endpoint_edit.setText(profile.base_url)
                self.endpoint_edit.setEnabled(False)
                self.secret_edit.clear()
                self.secret_edit.setEnabled(False)
                for role, edit in self.role_models.items():
                    edit.setText(profile.model_for(role))
                    edit.setEnabled(False)
                for widget in (
                    self.connect_timeout,
                    self.request_timeout,
                    self.first_chunk_timeout,
                    self.idle_timeout,
                    self.output_limit,
                    self.temperature,
                    self.top_p,
                ):
                    widget.setEnabled(False)
                self.stream_check.setChecked(profile.stream_enabled)
                self.stream_check.setEnabled(False)
                self.cache_check.setChecked(False)
                self.cache_check.setEnabled(False)
                self.cache_disclosure.setText(
                    "目录恢复模式：此 Profile 的内置契约不可验证，已禁止启用和测试；"
                    "配置仍保留，可切换为安全自定义类型后另存。"
                )
            finally:
                self._loading = False
            self.status.setText("提供商目录损坏，此 Profile 已在运行时停用。")
            self._sync_buttons()
            return
        self._loading = True
        try:
            self.catalog_combo.setCurrentIndex(self.catalog_combo.findData(profile.catalog_id))
            self.name_edit.setText(profile.display_name)
            self.enabled_check.setChecked(profile.enabled)
            self.enabled_check.setEnabled(True)
            self.protocol_value.setText(_PROTOCOL_LABELS[profile.protocol])
            self.capability_disclosure.setText(self._capability_text(entry))
            self.auth_value.clear()
            if entry.custom_endpoint:
                allowed_auth = (
                    ProviderAuth.BEARER,
                    ProviderAuth.API_KEY,
                    ProviderAuth.NONE,
                ) if entry.protocol is ProviderProtocol.OPENAI_CHAT_COMPLETIONS else (
                    ProviderAuth.ANTHROPIC_X_API_KEY,
                    ProviderAuth.BEARER,
                    ProviderAuth.NONE,
                )
            else:
                allowed_auth = (entry.auth,)
            for auth in allowed_auth:
                self.auth_value.addItem(_AUTH_LABELS[auth], auth.value)
            auth_index = self.auth_value.findData(profile.auth.value)
            self.auth_value.setCurrentIndex(auth_index if auth_index >= 0 else 0)
            self.auth_value.setEnabled(entry.custom_endpoint)
            self.endpoint_combo.clear()
            for endpoint in entry.endpoints:
                self.endpoint_combo.addItem(endpoint, endpoint)
            self.endpoint_combo.setCurrentIndex(max(0, self.endpoint_combo.findData(profile.base_url)))
            self.endpoint_edit.setText(profile.base_url)
            self.endpoint_combo.setVisible(not entry.custom_endpoint and len(entry.endpoints) > 1)
            self.endpoint_edit.setVisible(entry.custom_endpoint or len(entry.endpoints) <= 1)
            self.endpoint_edit.setEnabled(entry.custom_endpoint)
            self.secret_edit.clear()
            self.secret_edit.setEnabled(profile.auth is not ProviderAuth.NONE)
            for role, edit in self.role_models.items():
                edit.setText(profile.model_for(role))
                edit.setEnabled(
                    role is not ProviderRole.VISION or entry.capabilities.image_input
                )
            self.connect_timeout.setValue(round(profile.connect_timeout_seconds))
            self.request_timeout.setValue(round(profile.request_timeout_seconds))
            self.first_chunk_timeout.setValue(round(profile.first_chunk_timeout_seconds))
            self.idle_timeout.setValue(round(profile.idle_timeout_seconds))
            self.output_limit.setValue(profile.max_output_tokens)
            self.temperature.setValue(profile.temperature)
            self.top_p.setValue(profile.top_p)
            for widget in (
                self.connect_timeout,
                self.request_timeout,
                self.first_chunk_timeout,
                self.idle_timeout,
                self.output_limit,
                self.temperature,
                self.top_p,
            ):
                widget.setEnabled(True)
            self.stream_check.setChecked(profile.stream_enabled)
            self.stream_check.setEnabled(entry.capabilities.streaming)
            controllable_cache = entry.cache_policy in {
                CachePolicy.DASHSCOPE_SESSION,
                CachePolicy.ANTHROPIC_EPHEMERAL,
            }
            self.cache_check.setChecked(profile.cache_enabled if controllable_cache else False)
            self.cache_check.setEnabled(controllable_cache)
            if not controllable_cache and profile.cache_enabled:
                profile = replace(profile, cache_enabled=False).invalidate_tests()
                row = self.profile_list.currentRow()
                if 0 <= row < len(self._profiles):
                    self._profiles[row] = profile
            self.cache_disclosure.setText(self._cache_text(profile, entry.cache_policy))
        finally:
            self._loading = False
        self._sync_test_status(profile)
        self._sync_buttons()

    def _candidate(self, *, invalidate: bool) -> ProviderProfile:
        row = self.profile_list.currentRow()
        if not 0 <= row < len(self._profiles):
            raise ProviderProfileError("请选择一个 Profile。")
        current = self._profiles[row]
        catalog_id = self.catalog_combo.currentData()
        if not isinstance(catalog_id, str) or catalog_id not in self.catalog.entries:
            raise ProviderProfileError(
                "目录契约不可用；请先明确切换为安全自定义类型。"
            )
        entry = self.catalog.entry(catalog_id)
        endpoint = (
            self.endpoint_edit.text().strip()
            if entry.custom_endpoint or entry.local_template or len(entry.endpoints) <= 1
            else str(self.endpoint_combo.currentData())
        )
        candidate = ProviderProfile(
            profile_id=current.profile_id,
            catalog_id=entry.catalog_id,
            display_name=self.name_edit.text().strip(),
            enabled=self.enabled_check.isChecked(),
            protocol=entry.protocol,
            base_url=normalize_provider_base_url(endpoint),
            auth=ProviderAuth(str(self.auth_value.currentData())),
            credential_slot=(
                current.credential_slot
                if current.catalog_id == entry.catalog_id
                and current.protocol is entry.protocol
                and current.base_url == normalize_provider_base_url(endpoint)
                and current.auth is ProviderAuth(str(self.auth_value.currentData()))
                else "dynamic"
            ),
            models=MappingProxyType(
                {role: edit.text().strip() for role, edit in self.role_models.items()}
            ),
            connect_timeout_seconds=self.connect_timeout.value(),
            request_timeout_seconds=self.request_timeout.value(),
            first_chunk_timeout_seconds=self.first_chunk_timeout.value(),
            idle_timeout_seconds=self.idle_timeout.value(),
            max_output_tokens=self.output_limit.value(),
            temperature=self.temperature.value(),
            top_p=self.top_p.value(),
            stream_enabled=self.stream_check.isChecked(),
            cache_enabled=self.cache_check.isChecked(),
            test_fingerprints=current.test_fingerprints,
        ).validated(self.catalog)
        if invalidate:
            changed_roles = tuple(
                role
                for role in ProviderRole
                if candidate.test_fingerprint(role) != current.test_fingerprint(role)
            )
            if changed_roles:
                candidate = candidate.invalidate_tests(*changed_roles)
        return candidate

    @Slot()
    def _editor_changed(self, *_args: object) -> None:
        if self._loading:
            return
        try:
            candidate = self._candidate(invalidate=True)
        except (ProviderProfileError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        row = self.profile_list.currentRow()
        self._profiles[row] = candidate
        try:
            secret_update = self._candidate_secret_update(candidate)
        except (ProviderProfileError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        if secret_update is not None:
            self._secret_updates[candidate.profile_id] = secret_update
            self._tested_secret_fingerprints.pop(candidate.profile_id, None)
            self._profiles[row] = candidate.invalidate_tests()
        elif self.sender() is self.secret_edit:
            # Only an explicit edit of the password field may discard a
            # transient, already-tested replacement. Other editor signals can
            # arrive after the field was intentionally cleared on test success.
            self._secret_updates.pop(candidate.profile_id, None)
            self._tested_secret_fingerprints.pop(candidate.profile_id, None)
        self._sync_test_status(self._profiles[row])
        self._refresh_assignment_combos()
        self._sync_buttons()

    @Slot()
    def _catalog_changed(self, *_args: object) -> None:
        if self._loading:
            return
        entry = self.catalog.entry(str(self.catalog_combo.currentData()))
        row = self.profile_list.currentRow()
        if not 0 <= row < len(self._profiles):
            return
        current = self._profiles[row]
        replacement = ProviderProfile.from_catalog(
            entry,
            profile_id=current.profile_id,
            display_name=entry.display_name,
        )
        self._profiles[row] = replacement
        self._secret_updates.pop(current.profile_id, None)
        self._tested_secret_fingerprints.pop(current.profile_id, None)
        self._load_profile(replacement)
        self._refresh_lists(select=row)

    @Slot()
    def _endpoint_choice_changed(self, *_args: object) -> None:
        if self._loading or not self.endpoint_combo.isVisible():
            return
        self.endpoint_edit.setText(str(self.endpoint_combo.currentData()))
        self._editor_changed()

    @Slot()
    def _auth_changed(self, *_args: object) -> None:
        if self._loading:
            return
        none_selected = self.auth_value.currentData() == ProviderAuth.NONE.value
        self.secret_edit.setEnabled(not none_selected)
        if none_selected:
            self.secret_edit.clear()
        self._editor_changed()

    @Slot()
    def _test_role_changed(self, *_args: object) -> None:
        row = self.profile_list.currentRow()
        if 0 <= row < len(self._profiles):
            self._sync_test_status(self._profiles[row])

    @Slot()
    def _assignments_changed(self, *_args: object) -> None:
        if self._loading:
            return
        for role, combo in self.assignments.items():
            self._assignments[role] = combo.currentData()
        self._refresh_lists(select=self.profile_list.currentRow())
        row = self.profile_list.currentRow()
        if 0 <= row < len(self._profiles):
            entry = self.catalog.entries.get(self._profiles[row].catalog_id)
            if entry is not None:
                self.cache_disclosure.setText(
                    self._cache_text(self._profiles[row], entry.cache_policy)
                )

    @Slot()
    def _add_profile(self) -> None:
        if len(self._profiles) >= MAX_PROVIDER_PROFILES:
            self.status.setText("已达到 32 个 Profile 上限。")
            return
        available = self.catalog.all()
        if not available:
            self.status.setText("提供商目录不可用，无法新增 Profile。")
            return
        entry = available[0]
        self._profiles.append(ProviderProfile.from_catalog(entry))
        self._refresh_lists(select=len(self._profiles) - 1)

    @Slot()
    def _copy_profile(self) -> None:
        row = self.profile_list.currentRow()
        if not 0 <= row < len(self._profiles) or len(self._profiles) >= MAX_PROVIDER_PROFILES:
            return
        source = self._profiles[row]
        duplicate = replace(
            source,
            profile_id=f"profile-{uuid4().hex}",
            display_name=f"{source.display_name} 副本",
            enabled=False,
            credential_slot="dynamic",
            test_fingerprints=MappingProxyType({role: "" for role in ProviderRole}),
        )
        self._profiles.append(duplicate)
        self._refresh_lists(select=len(self._profiles) - 1)
        self.status.setText("已复制 Profile；密钥和连接测试未复制。")

    @Slot()
    def _delete_profile(self) -> None:
        row = self.profile_list.currentRow()
        if not 0 <= row < len(self._profiles):
            return
        profile = self._profiles[row]
        if profile.profile_id in self._assignments.values():
            self.status.setText("该 Profile 仍被任务分配使用，请先重新分配或取消。")
            return
        answer = QMessageBox.question(
            self,
            "删除模型 Profile",
            f"删除“{profile.display_name}”及其 Amadeus 专属凭据？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        persisted = self._settings.profile(profile.profile_id)
        if persisted is not None:
            self._deleted_profiles.append(persisted)
        self._secret_updates.pop(profile.profile_id, None)
        self._tested_secret_fingerprints.pop(profile.profile_id, None)
        self._profiles.pop(row)
        self._refresh_lists(select=max(0, row - 1))

    @Slot()
    def _start_test(self) -> None:
        if self.test_running or self._saving:
            return
        try:
            profile = self._candidate(invalidate=False)
            role = ProviderRole(str(self.test_role.currentData()))
            if not profile.model_for(role):
                raise ProviderProfileError("当前任务没有填写模型名。")
            entry = self.catalog.entry(profile.catalog_id)
            if role is ProviderRole.VISION and not entry.capabilities.image_input:
                raise ProviderProfileError("该目录类型未登记图片能力。")
            secret = self._effective_secret(profile)
        except (ProviderProfileError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        cancellation = CancellationToken()
        thread = QThread(self)
        worker = _ProfileTestWorker(
            self._tester, profile, role, self.catalog, secret, cancellation
        )
        worker.moveToThread(thread)
        self._test_thread = thread
        self._test_worker = worker
        self._test_cancel = cancellation
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._test_succeeded)
        worker.failed.connect(self._test_failed)
        worker.cancelled.connect(self._test_cancelled)
        worker.done.connect(worker.deleteLater)
        worker.done.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._test_finished)
        self.status.setText("正在后台执行无私人数据的最小连接测试…")
        self._sync_buttons()
        thread.start()

    @Slot(str, str, str, str, int)
    def _test_succeeded(
        self,
        profile_id: str,
        role_value: str,
        request_fingerprint: str,
        credential_fingerprint: str,
        elapsed_ms: int,
    ) -> None:
        role = ProviderRole(role_value)
        accepted = False
        for index, profile in enumerate(self._profiles):
            if profile.profile_id == profile_id:
                try:
                    current_secret = self._effective_secret(profile)
                    if not hmac.compare_digest(
                        profile.test_fingerprint(role), request_fingerprint
                    ) or not hmac.compare_digest(
                        transient_credential_fingerprint(current_secret),
                        credential_fingerprint,
                    ):
                        self.status.setText(
                            "测试期间配置或密钥已改变；本次结果已丢弃，请重新测试。"
                        )
                        return
                    self._profiles[index] = profile.mark_tested(role)
                    if profile.profile_id in self._secret_updates:
                        self._tested_secret_fingerprints[profile.profile_id] = (
                            credential_fingerprint
                        )
                    else:
                        self._tested_secret_fingerprints.pop(profile.profile_id, None)
                    accepted = True
                except (CredentialStoreError, ProviderProfileError):
                    self.status.setText(
                        "连接已返回，但当前配置、凭据或本机绑定无法复核；"
                        "Profile 未标记为已测试。"
                    )
                    return
                break
        if not accepted:
            self.status.setText("测试对应的 Profile 已不存在；本次结果已丢弃。")
            return
        # Keep a newly tested credential only in the transient transaction map;
        # clear the password widget without letting its signal discard that map.
        if (
            0 <= self.profile_list.currentRow() < len(self._profiles)
            and self._profiles[self.profile_list.currentRow()].profile_id == profile_id
        ):
            self._loading = True
            try:
                self.secret_edit.clear()
            finally:
                self._loading = False
            self.secret_edit.setPlaceholderText("新密钥已通过测试，等待保存")
        self.status.setText(f"{_ROLE_LABELS[role]}连接测试通过（{elapsed_ms} ms）。")

    @Slot(str, str, str)
    def _test_failed(self, profile_id: str, role_value: str, message: str) -> None:
        del profile_id, role_value
        self.status.setText(message)

    @Slot(str, str)
    def _test_cancelled(self, profile_id: str, role_value: str) -> None:
        del profile_id, role_value
        self.status.setText("连接测试已取消。")

    @Slot()
    def _test_finished(self) -> None:
        thread = self._test_thread
        if thread is None:
            return
        thread.wait()
        thread.deleteLater()
        self._test_thread = None
        self._test_worker = None
        self._test_cancel = None
        row = self.profile_list.currentRow()
        if 0 <= row < len(self._profiles):
            self._sync_test_status(self._profiles[row], preserve=True)
        self._sync_buttons()

    def _effective_secret(self, profile: ProviderProfile) -> str:
        if profile.auth is ProviderAuth.NONE:
            return ""
        candidate = self._secret_updates.get(profile.profile_id, "")
        if candidate:
            if candidate.casefold().startswith("tp-") or "\x00" in candidate:
                raise ProviderProfileError("密钥格式无效或属于禁止的 Token Plan。")
            return candidate
        try:
            secret = self._credential_store_factory(profile).read_secret()
        except CredentialStoreError:
            raise ProviderProfileError("Windows 凭据管理器不可用。") from None
        if not secret:
            raise ProviderProfileError("请输入此 Profile 安全域对应的 API 密钥。")
        return secret

    def _candidate_secret_update(self, profile: ProviderProfile) -> str | None:
        raw = self.secret_edit.text()
        if not raw:
            return None
        secret = raw.strip()
        if (
            not secret
            or secret != raw
            or "\x00" in secret
            or secret.casefold().startswith("tp-")
        ):
            raise ProviderProfileError("密钥格式无效或属于禁止的 Token Plan。")
        return secret

    @Slot()
    def _save(self) -> None:
        if self.test_running or self._saving:
            return
        try:
            row = self.profile_list.currentRow()
            if row >= 0:
                candidate = self._candidate(invalidate=True)
                secret_update = self._candidate_secret_update(candidate)
                if secret_update is not None:
                    self._secret_updates[candidate.profile_id] = secret_update
                    self._tested_secret_fingerprints.pop(candidate.profile_id, None)
                    candidate = candidate.invalidate_tests()
                self._profiles[row] = candidate
            settings = ProviderSettings(
                tuple(self._profiles), MappingProxyType(dict(self._assignments))
            ).validated(self.catalog).require_assigned_tests()
            for role, profile_id in self._assignments.items():
                if profile_id is None:
                    continue
                profile = settings.profile(profile_id)
                if profile is None or not profile.enabled:
                    raise ProviderProfileError(
                        f"{_ROLE_LABELS[role]}分配的 Profile 尚未启用。"
                    )
                if profile.auth is not ProviderAuth.NONE:
                    self._effective_secret(profile)
            assigned_secret_updates = set(self._secret_updates).intersection(
                profile_id
                for profile_id in self._assignments.values()
                if profile_id is not None
            )
            if not assigned_secret_updates.issubset(
                self._tested_secret_fingerprints
            ):
                raise ProviderProfileError(
                    "新密钥必须先完成当前被分配模型的连接测试。"
                )
        except (ProviderProfileError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self._saving = True
        self._sync_buttons()
        self.status.setText("正在以可回滚事务保存 Profile、凭据和任务路由…")
        change = ProviderSettingsChange(
            settings,
            MappingProxyType(deepcopy(self._secret_updates)),
            MappingProxyType(deepcopy(self._tested_secret_fingerprints)),
            tuple(self._deleted_profiles),
        )
        self.save_requested.emit(change)

    def _sync_test_status(self, profile: ProviderProfile, *, preserve: bool = False) -> None:
        role = ProviderRole(str(self.test_role.currentData()))
        if preserve and self.status.text():
            return
        self.status.setText(
            "当前任务模型已通过连接测试。"
            if profile.is_tested(role)
            else "当前配置或密钥变更后必须重新测试被分配的模型。"
        )

    def _sync_buttons(self) -> None:
        busy = self._saving or self.test_running
        row = self.profile_list.currentRow()
        known_profile = bool(
            0 <= row < len(self._profiles)
            and self._profiles[row].catalog_id in self.catalog.entries
        )
        self.test_button.setEnabled(not busy and known_profile)
        self.save_button.setEnabled(not busy)
        self.add_button.setEnabled(
            not busy and bool(self.catalog.entries) and len(self._profiles) < MAX_PROVIDER_PROFILES
        )
        self.copy_button.setEnabled(
            not busy
            and known_profile
            and len(self._profiles) < MAX_PROVIDER_PROFILES
        )
        self.delete_button.setEnabled(not busy and bool(self._profiles))

    def _cache_text(self, profile: ProviderProfile, policy: CachePolicy) -> str:
        roles = [
            _ROLE_LABELS[role]
            for role, profile_id in self._assignments.items()
            if profile_id == profile.profile_id
        ]
        data = "、".join(roles) if roles else "当前未分配任务"
        if policy is CachePolicy.DASHSCOPE_SESSION:
            behavior = "开启后仅发送白名单 x-dashscope-session-cache: enable。"
        elif policy is CachePolicy.ANTHROPIC_EPHEMERAL:
            behavior = "开启后仅在系统块添加 cache_control: ephemeral。"
        elif policy is CachePolicy.UPSTREAM_AUTO:
            behavior = "供应商可能自动缓存；Amadeus 无法关闭或伪装控制，仅在此披露。"
        else:
            behavior = "Amadeus 不发送任何缓存控制字段。"
        return f"承载数据类别：{data}。{behavior}"

    @staticmethod
    def _capability_text(entry) -> str:
        capabilities = entry.capabilities
        return (
            f"流式：{'是' if capabilities.streaming else '否'}；"
            f"图片：{'是' if capabilities.image_input else '否'}；"
            "原生文件：否（文档由 Amadeus 本地抽取文本）；"
            f"推理：{'可关闭' if capabilities.reasoning_can_disable else '可能存在但无独立开关' if capabilities.reasoning else '未登记'}；"
            f"建议上下文 {capabilities.suggested_context_tokens}，"
            f"建议输出 {capabilities.suggested_output_tokens}；"
            f"首包/空闲建议 {capabilities.first_chunk_timeout_seconds}/"
            f"{capabilities.idle_timeout_seconds} 秒；模型名允许编辑。"
            + (
                "视觉槽默认留空，只有实际模型支持图片时才应填写并完成视觉测试。"
                if capabilities.image_input and not entry.model_for(ProviderRole.VISION)
                else ""
            )
        )
