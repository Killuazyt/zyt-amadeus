"""Provider settings page with cancellable connection validation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    ProviderConnectionTester,
)
from amadeus_desktop.credential_store import CredentialStoreError
from amadeus_desktop.provider_config import (
    AuthMode,
    ProviderConfig,
    ProviderConfigError,
    ProviderPreset,
    TokenLimitField,
)

_PRESET_LABELS = {
    ProviderPreset.DEEPSEEK_PAYG: "DeepSeek 按量",
    ProviderPreset.MIMO_PAYG: "MiMo 按量",
    ProviderPreset.CUSTOM_OPENAI: "自定义 OpenAI 兼容服务",
}
_AUTH_LABELS = {
    AuthMode.BEARER: "Bearer",
    AuthMode.API_KEY: "api-key",
}
_LIMIT_LABELS = {
    TokenLimitField.MAX_TOKENS: "max_tokens",
    TokenLimitField.MAX_COMPLETION_TOKENS: "max_completion_tokens",
}


class _ConnectionTestWorker(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str, str)
    cancelled = Signal(str)
    done = Signal()

    def __init__(
        self,
        tester: ProviderConnectionTester,
        config: ProviderConfig,
        secret: str,
        fingerprint: str,
        cancellation: CancellationToken,
    ) -> None:
        super().__init__()
        self._tester = tester
        self._config = config
        self._secret = secret
        self._fingerprint = fingerprint
        self._cancellation = cancellation

    @Slot()
    def run(self) -> None:
        try:
            result = asyncio.run(self._tester.test(self._config, self._secret, self._cancellation))
            self.succeeded.emit(self._fingerprint, result)
        except CancellationRequested:
            self.cancelled.emit(self._fingerprint)
        except ChatProviderError as exc:
            self.failed.emit(self._fingerprint, exc.safe_message)
        except Exception:  # noqa: BLE001 - never expose raw provider/credential details
            self.failed.emit(self._fingerprint, "连接测试失败，请检查配置。")
        finally:
            self._secret = ""
            self.done.emit()


class ModelSettingsWindow(QWidget):
    """Reusable provider settings page that can also be shown standalone."""

    save_requested = Signal(object, object)

    def __init__(
        self,
        config: ProviderConfig,
        *,
        has_saved_secret: bool,
        credential_reader: Callable[[], str | None],
        tester: ProviderConnectionTester | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("对话模型设置")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumWidth(420)

        self._credential_reader = credential_reader
        self._tester = tester or ProviderConnectionTester()
        self._current_config = config
        self._has_saved_secret = has_saved_secret
        self._tested_fingerprint: str | None = None
        self._loading = False
        self._test_thread: QThread | None = None
        self._test_worker: _ConnectionTestWorker | None = None
        self._test_cancellation: CancellationToken | None = None
        self._saving = False

        self.preset_combo = QComboBox()
        for preset, label in _PRESET_LABELS.items():
            self.preset_combo.addItem(label, preset.value)
        self.base_url_edit = QLineEdit()
        self.model_edit = QLineEdit()
        self.auth_combo = QComboBox()
        for auth_mode, label in _AUTH_LABELS.items():
            self.auth_combo.addItem(label, auth_mode.value)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setClearButtonEnabled(True)
        self.secret_edit.setAccessibleName("API 密钥")
        self.connect_timeout_spin = QSpinBox()
        self.connect_timeout_spin.setRange(1, 60)
        self.connect_timeout_spin.setSuffix(" 秒")
        self.request_timeout_spin = QSpinBox()
        self.request_timeout_spin.setRange(5, 300)
        self.request_timeout_spin.setSuffix(" 秒")
        self.output_limit_spin = QSpinBox()
        self.output_limit_spin.setRange(1, 32_768)
        self.limit_field_combo = QComboBox()
        for field, label in _LIMIT_LABELS.items():
            self.limit_field_combo.addItem(label, field.value)
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setSingleStep(0.05)
        self.top_p_spin = QDoubleSpinBox()
        self.top_p_spin.setRange(0.0, 1.0)
        self.top_p_spin.setDecimals(2)
        self.top_p_spin.setSingleStep(0.05)
        self.stream_check = QCheckBox("启用流式输出")

        self.form_layout = QFormLayout()
        self.form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form_layout.addRow("预设", self.preset_combo)
        self.form_layout.addRow("Base URL", self.base_url_edit)
        self.form_layout.addRow("模型", self.model_edit)
        self.form_layout.addRow("鉴权方式", self.auth_combo)
        self.form_layout.addRow("API 密钥", self.secret_edit)
        self.form_layout.addRow("连接超时", self.connect_timeout_spin)
        self.form_layout.addRow("总请求超时", self.request_timeout_spin)
        self.form_layout.addRow("输出上限", self.output_limit_spin)
        self.form_layout.addRow("上限字段", self.limit_field_combo)
        self.form_layout.addRow("temperature", self.temperature_spin)
        self.form_layout.addRow("top_p", self.top_p_spin)
        self.form_layout.addRow("", self.stream_check)

        self.privacy_notice = QLabel(
            "密钥只保存到 Windows 凭据管理器，不写入设置、日志或对话。"
            "已保存的密钥不会回显；留空表示继续使用它。"
        )
        self.privacy_notice.setWordWrap(True)
        self.privacy_notice.setObjectName("credentialPrivacyNotice")
        self.privacy_notice.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("modelSettingsStatus")
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Maximum,
        )

        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._start_test)
        self.save_button = QPushButton("保存并启用")
        self.save_button.clicked.connect(self._request_save)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)

        self.scroll_content = QWidget()
        content_layout = QVBoxLayout(self.scroll_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        content_layout.addLayout(self.form_layout)
        content_layout.addWidget(self.privacy_notice)
        content_layout.addStretch(1)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("modelSettingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setWidget(self.scroll_content)

        layout = QVBoxLayout(self)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.base_url_edit.textChanged.connect(self._on_credential_boundary_changed)
        self.auth_combo.currentIndexChanged.connect(self._on_credential_boundary_changed)
        for signal in (
            self.model_edit.textChanged,
            self.secret_edit.textChanged,
            self.connect_timeout_spin.valueChanged,
            self.request_timeout_spin.valueChanged,
            self.output_limit_spin.valueChanged,
            self.limit_field_combo.currentIndexChanged,
            self.temperature_spin.valueChanged,
            self.top_p_spin.valueChanged,
            self.stream_check.toggled,
        ):
            signal.connect(self._invalidate_test)
        self.load_config(config, has_saved_secret=has_saved_secret)

    @property
    def test_running(self) -> bool:
        return self._test_thread is not None

    @property
    def candidate_config(self) -> ProviderConfig:
        try:
            preset = ProviderPreset(self.preset_combo.currentData())
            auth_mode = AuthMode(self.auth_combo.currentData())
            token_limit_field = TokenLimitField(self.limit_field_combo.currentData())
        except (TypeError, ValueError):
            raise ValueError("供应商预设无效。") from None
        config = ProviderConfig(
            preset=preset,
            display_name=ProviderConfig.for_preset(preset).display_name,
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip(),
            auth_mode=auth_mode,
            credential_ref=self._current_config.credential_ref,
            connect_timeout_seconds=self.connect_timeout_spin.value(),
            request_timeout_seconds=self.request_timeout_spin.value(),
            max_output_tokens=self.output_limit_spin.value(),
            temperature=self.temperature_spin.value(),
            top_p=self.top_p_spin.value(),
            stream_enabled=self.stream_check.isChecked(),
            token_limit_field=token_limit_field,
        )
        return config.validated()

    def load_config(self, config: ProviderConfig, *, has_saved_secret: bool) -> None:
        if self.test_running:
            raise RuntimeError("cannot replace configuration during a connection test")
        self._loading = True
        try:
            self._current_config = config
            self._has_saved_secret = has_saved_secret
            self._select_data(self.preset_combo, config.preset)
            self.base_url_edit.setText(config.base_url)
            self.model_edit.setText(config.model)
            self._select_data(self.auth_combo, config.auth_mode)
            self.secret_edit.clear()
            self.secret_edit.setPlaceholderText(
                "留空使用已保存密钥" if has_saved_secret else "输入新的 API 密钥"
            )
            self.connect_timeout_spin.setValue(config.connect_timeout_seconds)
            self.request_timeout_spin.setValue(config.request_timeout_seconds)
            self.output_limit_spin.setValue(config.max_output_tokens)
            self._select_data(self.limit_field_combo, config.token_limit_field)
            self.temperature_spin.setValue(config.temperature)
            self.top_p_spin.setValue(config.top_p)
            self.stream_check.setChecked(config.stream_enabled)
        finally:
            self._loading = False
        self._sync_contract_editability()
        self._tested_fingerprint = None
        self.status_label.setText("修改配置后必须先通过连接测试才能保存。")
        self._sync_buttons()

    def show_and_activate(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def apply_save_result(self, *, success: bool, message: str) -> None:
        self._saving = False
        if success:
            self._current_config = self.candidate_config
            self._has_saved_secret = True
            self.secret_edit.clear()
            self.secret_edit.setPlaceholderText("留空使用已保存密钥")
            self._tested_fingerprint = None
        self.status_label.setText(message)
        self._sync_buttons()

    def require_new_secret(self) -> None:
        """Forbid reuse after a disabled or incomplete credential transaction."""

        self._has_saved_secret = False
        self.secret_edit.clear()
        self.secret_edit.setPlaceholderText("输入新的 API 密钥")
        self._tested_fingerprint = None
        self._sync_buttons()

    def cancel_test(self) -> None:
        if self._test_cancellation is not None:
            self._test_cancellation.cancel()
            self.status_label.setText("正在取消连接测试…")

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        self.cancel_test()
        thread = self._test_thread
        if thread is None:
            return True
        clean = not thread.isRunning() or thread.wait(max(0, wait_ms))
        if clean and self._test_thread is thread:
            self._test_thread = None
            self._test_worker = None
            self._test_cancellation = None
            thread.deleteLater()
        return clean

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        self.cancel_test()
        self.hide()
        event.ignore()

    def reject(self) -> None:
        """Treat Escape like a non-destructive close and cancel background work."""

        self.cancel_test()
        self.hide()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API name
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    @Slot()
    def _on_preset_changed(self) -> None:
        if self._loading:
            return
        try:
            preset = ProviderPreset(self.preset_combo.currentData())
        except (TypeError, ValueError):
            return
        config = ProviderConfig.for_preset(preset)
        self._loading = True
        try:
            self.base_url_edit.setText(config.base_url)
            self.model_edit.setText(config.model)
            self._select_data(self.auth_combo, config.auth_mode)
            self.connect_timeout_spin.setValue(config.connect_timeout_seconds)
            self.request_timeout_spin.setValue(config.request_timeout_seconds)
            self.output_limit_spin.setValue(config.max_output_tokens)
            self._select_data(self.limit_field_combo, config.token_limit_field)
            self.temperature_spin.setValue(config.temperature)
            self.top_p_spin.setValue(config.top_p)
            self.stream_check.setChecked(config.stream_enabled)
        finally:
            self._loading = False
        self.secret_edit.clear()
        self._sync_contract_editability()
        self._invalidate_test()

    @Slot()
    def _on_credential_boundary_changed(self, *_args: object) -> None:
        if self._loading:
            return
        if self.secret_edit.text():
            self.secret_edit.clear()
        self._invalidate_test()

    @Slot()
    def _invalidate_test(self, *_args: object) -> None:
        if self._loading:
            return
        self._tested_fingerprint = None
        if not self.test_running and not self._saving:
            self.status_label.setText("配置已更改，请重新测试连接。")
        self._sync_buttons()

    @Slot()
    def _start_test(self) -> None:
        if self.test_running or self._saving:
            return
        try:
            config = self.candidate_config
            secret = self._effective_secret(config)
            fingerprint = self._fingerprint(config, secret)
        except ProviderConfigError:
            self.status_label.setText("供应商配置不安全或格式无效。")
            self._sync_buttons()
            return
        except ValueError as exc:
            self.status_label.setText(str(exc))
            self._sync_buttons()
            return

        cancellation = CancellationToken()
        thread = QThread(self)
        thread.setObjectName("provider-connection-test")
        worker = _ConnectionTestWorker(
            self._tester,
            config,
            secret,
            fingerprint,
            cancellation,
        )
        worker.moveToThread(thread)
        self._test_thread = thread
        self._test_worker = worker
        self._test_cancellation = cancellation
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_test_succeeded)
        worker.failed.connect(self._on_test_failed)
        worker.cancelled.connect(self._on_test_cancelled)
        worker.done.connect(worker.deleteLater)
        worker.done.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._on_test_finished, Qt.ConnectionType.QueuedConnection)
        self.status_label.setText("正在后台测试连接…")
        self._sync_buttons()
        thread.start()

    @Slot(str, object)
    def _on_test_succeeded(self, fingerprint: str, result: object) -> None:
        if fingerprint != self._current_fingerprint():
            self.status_label.setText("测试完成，但配置已变更，请重新测试。")
            return
        self._tested_fingerprint = fingerprint
        elapsed_ms = getattr(result, "elapsed_ms", None)
        suffix = f"（{elapsed_ms} ms）" if isinstance(elapsed_ms, int) else ""
        self.status_label.setText(f"连接测试通过{suffix}，现在可以保存。")

    @Slot(str, str)
    def _on_test_failed(self, fingerprint: str, message: str) -> None:
        del fingerprint
        self._tested_fingerprint = None
        self.status_label.setText(message)

    @Slot(str)
    def _on_test_cancelled(self, fingerprint: str) -> None:
        del fingerprint
        self._tested_fingerprint = None
        self.status_label.setText("连接测试已取消。")

    @Slot()
    def _on_test_finished(self) -> None:
        thread = self._test_thread
        if thread is None:
            return
        QTimer.singleShot(0, self, lambda: self._finalize_test_thread(thread))

    def _finalize_test_thread(self, thread: QThread) -> None:
        if self._test_thread is not thread:
            return
        thread.wait()
        self._test_thread = None
        self._test_worker = None
        self._test_cancellation = None
        thread.deleteLater()
        self._sync_buttons()

    @Slot()
    def _request_save(self) -> None:
        if self.test_running or self._saving:
            return
        try:
            config = self.candidate_config
            fingerprint = self._current_fingerprint()
        except ProviderConfigError:
            self.status_label.setText("供应商配置不安全或格式无效。")
            return
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        if fingerprint is None or fingerprint != self._tested_fingerprint:
            self.status_label.setText("必须先通过当前配置的连接测试。")
            self._sync_buttons()
            return
        secret: str | None = self.secret_edit.text() or None
        self._saving = True
        self.status_label.setText("正在安全保存配置…")
        self._sync_buttons()
        self.save_requested.emit(config, secret)

    def _current_fingerprint(self) -> str | None:
        try:
            config = self.candidate_config
            secret = self._effective_secret(config)
            return self._fingerprint(config, secret)
        except ValueError:
            return None

    def _effective_secret(self, config: ProviderConfig) -> str:
        secret: object = self.secret_edit.text()
        if not secret:
            if (
                not self._has_saved_secret
                or config.credential_scope != self._current_config.credential_scope
            ):
                raise ValueError("供应商、地址或鉴权方式已变化，请输入对应的新 API 密钥。")
            try:
                secret = self._credential_reader()
            except CredentialStoreError:
                raise ValueError("Windows 凭据管理器不可用，无法读取密钥。") from None
        if not isinstance(secret, str) or not secret:
            raise ValueError("请输入 API 密钥。")
        if secret != secret.strip() or "\x00" in secret:
            raise ValueError("API 密钥格式无效。")
        if secret.casefold().startswith("tp-"):
            raise ValueError("Token Plan 密钥不能用于桌面陪伴对话。")
        return secret

    def _sync_contract_editability(self) -> None:
        try:
            custom = ProviderPreset(self.preset_combo.currentData()) is ProviderPreset.CUSTOM_OPENAI
        except (TypeError, ValueError):
            custom = False
        self.base_url_edit.setEnabled(custom)
        self.auth_combo.setEnabled(custom)
        self.limit_field_combo.setEnabled(custom)

    @staticmethod
    def _fingerprint(config: ProviderConfig, secret: str) -> str:
        payload = json.dumps(config.to_mapping(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256()
        digest.update(payload.encode("utf-8"))
        digest.update(b"\0")
        digest.update(secret.encode("utf-8"))
        return digest.hexdigest()

    def _sync_buttons(self) -> None:
        busy = self.test_running or self._saving
        self.test_button.setEnabled(not busy)
        self.save_button.setEnabled(
            not busy
            and self._tested_fingerprint is not None
            and self._tested_fingerprint == self._current_fingerprint()
        )

    @staticmethod
    def _select_data(combo: QComboBox, value: Any) -> None:
        data = value.value if hasattr(value, "value") else value
        index = combo.findData(data)
        if index < 0:
            raise ValueError(f"Unsupported settings value: {value!r}")
        combo.setCurrentIndex(index)
