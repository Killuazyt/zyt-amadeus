"""Independent multimodal provider settings with optional safe MiMo credential reuse."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget

from amadeus_desktop.chat_provider import ProviderConnectionTester
from amadeus_desktop.provider_config import ProviderConfig
from amadeus_desktop.ui.model_settings import ModelSettingsWindow


class MultimodalSettingsPage(QWidget):
    """Wrap the proven provider editor while keeping credentials independent."""

    save_requested = Signal(object, object, bool, bool)

    def __init__(
        self,
        config: ProviderConfig,
        *,
        enabled: bool,
        reuse_mimo_credential: bool,
        has_saved_secret: bool,
        has_reusable_secret: bool,
        credential_reader: Callable[[], str | None],
        reusable_credential_reader: Callable[[], str | None],
        tester: ProviderConnectionTester | None = None,
    ) -> None:
        super().__init__()
        self._own_reader = credential_reader
        self._reusable_reader = reusable_credential_reader
        self._has_own_secret = bool(has_saved_secret)
        self._has_reusable_secret = bool(has_reusable_secret)

        self.enabled_check = QCheckBox("启用图片/视觉多模态模型")
        self.enabled_check.setChecked(enabled)
        self.reuse_check = QCheckBox("与相同 MiMo PAYG 安全域复用对话密钥")
        self.reuse_check.setChecked(reuse_mimo_credential)
        notice = QLabel(
            "纯文字仍使用“对话模型”。只有当前消息含图片或最近两轮含视觉上下文时，"
            "才会路由到这里；失败时不会让文字模型猜测画面。"
        )
        notice.setWordWrap(True)
        self.editor = ModelSettingsWindow(
            config,
            has_saved_secret=self._effective_has_secret(),
            credential_reader=self._read_secret,
            tester=tester,
        )
        self.close_button = self.editor.close_button
        self.editor.save_requested.connect(self._relay_save)
        self.reuse_check.toggled.connect(self._on_reuse_changed)
        self.enabled_check.toggled.connect(self.editor._invalidate_test)
        self.disable_button = QPushButton("停用多模态")
        self.disable_button.clicked.connect(self._request_disable)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.enabled_check)
        layout.addWidget(self.reuse_check)
        layout.addWidget(notice)
        layout.addWidget(self.disable_button)
        layout.addWidget(self.editor, 1)

    @property
    def candidate_config(self) -> ProviderConfig:
        return self.editor.candidate_config

    def cancel_test(self) -> None:
        self.editor.cancel_test()

    def shutdown(self, wait_ms: int = 2_000) -> bool:
        return self.editor.shutdown(wait_ms)

    def apply_save_result(self, *, success: bool, message: str) -> None:
        if success:
            self._has_own_secret = self._has_own_secret or bool(self.editor.secret_edit.text())
        self.editor.apply_save_result(success=success, message=message)

    def _effective_has_secret(self) -> bool:
        return self._has_reusable_secret if self.reuse_check.isChecked() else self._has_own_secret

    def _read_secret(self) -> str | None:
        return self._reusable_reader() if self.reuse_check.isChecked() else self._own_reader()

    def _on_reuse_changed(self, _enabled: bool) -> None:
        self.editor._has_saved_secret = self._effective_has_secret()
        self.editor.secret_edit.clear()
        self.editor.secret_edit.setPlaceholderText(
            "留空使用已保存密钥" if self.editor._has_saved_secret else "输入新的 API 密钥"
        )
        self.editor._invalidate_test()

    def _relay_save(self, config: object, secret: object) -> None:
        self.save_requested.emit(
            config,
            secret,
            self.enabled_check.isChecked(),
            self.reuse_check.isChecked(),
        )

    def _request_disable(self) -> None:
        self.enabled_check.setChecked(False)
        try:
            config = self.candidate_config
        except ValueError:
            config = self.editor._current_config
        self.save_requested.emit(config, None, False, self.reuse_check.isChecked())
