from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from amadeus_desktop.chat_provider import (
    CancellationRequested,
    CancellationToken,
    ChatProviderError,
    ConnectionTestResult,
    ProviderErrorCode,
)
from amadeus_desktop.credential_store import CredentialStoreError
from amadeus_desktop.provider_config import ProviderConfig, ProviderPreset
from amadeus_desktop.ui.model_settings import ModelSettingsWindow


class SuccessfulTester:
    async def test(self, config, secret, cancellation):
        assert secret == "invalid-test-key"
        cancellation.raise_if_cancelled()
        await asyncio.sleep(0)
        return ConnectionTestResult(config.preset, config.model, 7)


class BlockingTester:
    async def test(self, config, secret, cancellation: CancellationToken):
        del config, secret
        cancellation.bind_current_task()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        finally:
            cancellation.unbind_current_task()


class FailingTester:
    async def test(self, config, secret, cancellation):
        del config, secret
        cancellation.raise_if_cancelled()
        raise ChatProviderError(ProviderErrorCode.AUTHENTICATION)


def make_window(qtbot, tester, *, saved_secret: str | None = None):
    window = ModelSettingsWindow(
        ProviderConfig.default(),
        has_saved_secret=saved_secret is not None,
        credential_reader=lambda: saved_secret,
        tester=tester,
    )
    qtbot.addWidget(window)
    return window


def test_password_never_echoes_saved_secret_and_presets_fill_locked_defaults(qtbot) -> None:
    window = make_window(qtbot, SuccessfulTester(), saved_secret="saved-invalid-key")

    assert window.secret_edit.echoMode() is QLineEdit.EchoMode.Password
    assert window.secret_edit.text() == ""
    assert "已保存" in window.secret_edit.placeholderText()
    assert window.model_edit.text() == "deepseek-v4-flash"

    mimo_index = window.preset_combo.findData(ProviderPreset.MIMO_PAYG)
    window.preset_combo.setCurrentIndex(mimo_index)

    assert window.base_url_edit.text() == "https://api.xiaomimimo.com/v1"
    assert window.model_edit.text() == "mimo-v2.5-pro"
    assert window.auth_combo.currentText() == "api-key"
    assert not window.base_url_edit.isEnabled()
    assert not window.auth_combo.isEnabled()
    assert not window.limit_field_combo.isEnabled()

    custom_index = window.preset_combo.findData(ProviderPreset.CUSTOM_OPENAI)
    window.preset_combo.setCurrentIndex(custom_index)
    assert window.base_url_edit.isEnabled()
    assert window.auth_combo.isEnabled()
    assert window.limit_field_combo.isEnabled()


def test_candidate_must_pass_in_background_and_editing_invalidates_result(qtbot) -> None:
    window = make_window(qtbot, SuccessfulTester())
    window.secret_edit.setText("invalid-test-key")

    window.test_button.click()
    qtbot.waitUntil(lambda: not window.test_running, timeout=2_000)

    assert "连接测试通过" in window.status_label.text()
    assert window.save_button.isEnabled()

    window.temperature_spin.setValue(0.8)

    assert not window.save_button.isEnabled()
    assert "请重新测试" in window.status_label.text()


def test_save_emits_only_after_current_candidate_test_passes(qtbot) -> None:
    window = make_window(qtbot, SuccessfulTester())
    saved: list[tuple[object, object]] = []
    window.save_requested.connect(lambda config, secret: saved.append((config, secret)))
    window.secret_edit.setText("invalid-test-key")

    window.save_button.click()
    assert saved == []

    window.test_button.click()
    qtbot.waitUntil(lambda: window.save_button.isEnabled(), timeout=2_000)
    window.save_button.click()

    assert len(saved) == 1
    assert saved[0][0] == window.candidate_config
    assert saved[0][1] == "invalid-test-key"


def test_close_cancels_background_connection_test_and_cleans_thread(qtbot) -> None:
    window = make_window(qtbot, BlockingTester())
    window.secret_edit.setText("invalid-test-key")
    window.show()
    window.test_button.click()
    qtbot.waitUntil(lambda: window.test_running)

    qtbot.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not window.test_running, timeout=2_000)

    assert not window.isVisible()
    assert window.shutdown()


def test_credential_reader_failure_is_safe_and_does_not_start_test(qtbot) -> None:
    def fail_read() -> str | None:
        raise CredentialStoreError("private credential detail")

    window = ModelSettingsWindow(
        ProviderConfig.default(),
        has_saved_secret=True,
        credential_reader=fail_read,
        tester=SuccessfulTester(),
    )
    qtbot.addWidget(window)

    window.test_button.click()

    assert not window.test_running
    assert "Windows 凭据管理器不可用" in window.status_label.text()
    assert "private credential detail" not in window.status_label.text()


def test_failed_key_or_model_test_never_emits_save(qtbot) -> None:
    window = make_window(qtbot, FailingTester())
    saved: list[tuple[object, object]] = []
    window.save_requested.connect(lambda config, secret: saved.append((config, secret)))
    window.secret_edit.setText("invalid-rejected-key")

    window.test_button.click()
    qtbot.waitUntil(lambda: not window.test_running, timeout=2_000)
    window.save_button.click()

    assert saved == []
    assert not window.save_button.isEnabled()
    assert "鉴权失败" in window.status_label.text()


def test_saved_secret_is_never_reused_across_provider_scope(qtbot) -> None:
    reads = 0

    def read_saved_secret() -> str:
        nonlocal reads
        reads += 1
        return "saved-invalid-key"

    window = ModelSettingsWindow(
        ProviderConfig.default(),
        has_saved_secret=True,
        credential_reader=read_saved_secret,
        tester=SuccessfulTester(),
    )
    qtbot.addWidget(window)
    mimo_index = window.preset_combo.findData(ProviderPreset.MIMO_PAYG)
    window.preset_combo.setCurrentIndex(mimo_index)

    window.test_button.click()

    assert not window.test_running
    assert reads == 0
    assert "输入对应的新 API 密钥" in window.status_label.text()


def test_changing_custom_credential_boundary_clears_typed_secret(qtbot) -> None:
    window = make_window(qtbot, SuccessfulTester())
    custom_index = window.preset_combo.findData(ProviderPreset.CUSTOM_OPENAI)
    window.preset_combo.setCurrentIndex(custom_index)
    window.secret_edit.setText("invalid-test-key")

    window.base_url_edit.setText("https://second.example.invalid/v1")

    assert window.secret_edit.text() == ""
    assert not window.save_button.isEnabled()
