"""Audio device and MiMo speech settings without secret persistence in JSON."""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.audio_runtime import AudioDeviceCatalog, AudioDeviceInfo
from amadeus_desktop.provider_profiles import ProviderProfile


class VoiceSettingsPage(QWidget):
    save_requested = Signal(object, object)

    def __init__(
        self,
        settings: dict[str, object],
        catalog: AudioDeviceCatalog,
        *,
        has_own_secret: bool,
        text_credential_reusable: bool,
        multimodal_credential_reusable: bool,
        reusable_mimo_profiles: tuple[ProviderProfile, ...] = (),
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._has_own_secret = bool(has_own_secret)
        self._settings = deepcopy(settings)
        self.enabled_check = QCheckBox("启用 MiMo 逐句语音")
        self.enabled_check.setChecked(bool(settings["enabled"]))
        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        self.hands_free_check = QCheckBox("允许在聊天面板中显式开启免提 VAD")
        self.hands_free_check.setChecked(bool(settings["hands_free_enabled"]))
        self.credential_source = QComboBox()
        self.credential_source.addItem("独立 MiMo 语音密钥", "independent")
        del text_credential_reusable, multimodal_credential_reusable
        for profile in reusable_mimo_profiles:
            self.credential_source.addItem(
                f"复用 MiMo PAYG Profile：{profile.display_name}",
                f"profile:{profile.profile_id}",
            )
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setClearButtonEnabled(True)
        self.secret_edit.setPlaceholderText(
            "留空使用已保存密钥" if has_own_secret else "输入独立 MiMo PAYG API 密钥"
        )
        self.connect_timeout = QSpinBox()
        self.connect_timeout.setRange(1, 60)
        self.connect_timeout.setSuffix(" 秒")
        self.connect_timeout.setValue(int(settings["connect_timeout_seconds"]))
        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(5, 300)
        self.request_timeout.setSuffix(" 秒")
        self.request_timeout.setValue(int(settings["request_timeout_seconds"]))
        self.contract = QLabel(
            "固定契约：mimo-v2.5-asr → 现有可取消流式文字聊天 → "
            "mimo-v2.5-tts / mimo_default / WAV。原始麦克风音频不写入历史。"
        )
        self.contract.setWordWrap(True)
        self.privacy = QLabel(
            "即使允许免提，麦克风和播音也不会自动启动；密钥只进入 Windows 凭据管理器。"
        )
        self.privacy.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.save_button = QPushButton("保存语音设置")
        self.save_button.clicked.connect(self._save)

        form = QFormLayout()
        form.addRow("麦克风", self.input_combo)
        form.addRow("输出设备", self.output_combo)
        form.addRow("凭据来源", self.credential_source)
        form.addRow("独立 API 密钥", self.secret_edit)
        form.addRow("连接超时", self.connect_timeout)
        form.addRow("请求超时", self.request_timeout)
        layout = QVBoxLayout(self)
        layout.addWidget(self.enabled_check)
        layout.addLayout(form)
        layout.addWidget(self.hands_free_check)
        layout.addWidget(self.contract)
        layout.addWidget(self.privacy)
        layout.addWidget(self.status)
        layout.addWidget(self.save_button)
        layout.addStretch(1)

        self._catalog.changed.connect(self._reload_devices)
        self.credential_source.currentIndexChanged.connect(self._sync_secret_enabled)
        self._reload_devices()
        self._select_data(self.credential_source, str(settings["credential_source"]))
        self._sync_secret_enabled()

    def update_reusable_mimo_profiles(
        self,
        profiles: tuple[ProviderProfile, ...],
        *,
        selected_source: str | None = None,
    ) -> None:
        """Refresh profile-backed voice choices after one provider transaction."""

        selected = selected_source or str(self.credential_source.currentData() or "independent")
        self.credential_source.blockSignals(True)
        self.credential_source.clear()
        self.credential_source.addItem("独立 MiMo 语音密钥", "independent")
        for profile in profiles:
            self.credential_source.addItem(
                f"复用 MiMo PAYG Profile：{profile.display_name}",
                f"profile:{profile.profile_id}",
            )
        if not self._select_data(self.credential_source, selected):
            self.credential_source.setCurrentIndex(0)
        self.credential_source.blockSignals(False)
        self._sync_secret_enabled()

    def apply_save_result(self, *, success: bool, message: str) -> None:
        self.save_button.setEnabled(True)
        if success and self.credential_source.currentData() == "independent":
            self._has_own_secret = self._has_own_secret or bool(self.secret_edit.text())
            self.secret_edit.clear()
            self.secret_edit.setPlaceholderText("留空使用已保存密钥")
        self.status.setText(message)

    def set_runtime_hands_free(self, enabled: bool) -> None:
        # The setting is only a user preference; this method never starts capture.
        self.hands_free_check.setChecked(bool(enabled))

    def _reload_devices(self) -> None:
        input_id = str(self.input_combo.currentData() or self._settings["input_device_id"])
        output_id = str(self.output_combo.currentData() or self._settings["output_device_id"])
        self._fill_devices(self.input_combo, self._catalog.inputs(), input_id, "系统默认麦克风")
        self._fill_devices(
            self.output_combo,
            self._catalog.outputs(),
            output_id,
            "系统默认输出",
        )

    def _save(self) -> None:
        source = str(self.credential_source.currentData())
        settings = {
            "enabled": self.enabled_check.isChecked(),
            "input_device_id": str(self.input_combo.currentData() or ""),
            "output_device_id": str(self.output_combo.currentData() or ""),
            "hands_free_enabled": self.hands_free_check.isChecked(),
            "base_url": "https://api.xiaomimimo.com/v1",
            "asr_model": "mimo-v2.5-asr",
            "tts_model": "mimo-v2.5-tts",
            "tts_voice": "mimo_default",
            "tts_format": "wav",
            "connect_timeout_seconds": self.connect_timeout.value(),
            "request_timeout_seconds": self.request_timeout.value(),
            "credential_ref": "windows-credential-manager:amadeus-mimo-speech",
            "credential_source": source,
        }
        if (
            settings["enabled"]
            and source == "independent"
            and not self.secret_edit.text()
            and not self._has_own_secret
        ):
            self.status.setText("请输入独立 MiMo PAYG API 密钥。")
            return
        self.save_button.setEnabled(False)
        self.save_requested.emit(settings, self.secret_edit.text().strip() or None)

    def _sync_secret_enabled(self) -> None:
        own = self.credential_source.currentData() == "independent"
        self.secret_edit.setEnabled(own)
        if not own:
            self.secret_edit.clear()

    @staticmethod
    def _fill_devices(
        combo: QComboBox,
        devices: tuple[AudioDeviceInfo, ...],
        selected: str,
        default_label: str,
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(default_label, "")
        for device in devices:
            suffix = "（默认）" if device.is_default else ""
            combo.addItem(f"{device.description}{suffix}", device.device_id)
        if selected and not VoiceSettingsPage._select_data(combo, selected):
            combo.addItem("已保存设备（当前不可用）", selected)
            VoiceSettingsPage._select_data(combo, selected)
        combo.blockSignals(False)

    @staticmethod
    def _select_data(combo: QComboBox, value: str) -> bool:
        index = combo.findData(value)
        if index < 0:
            return False
        combo.setCurrentIndex(index)
        return True
