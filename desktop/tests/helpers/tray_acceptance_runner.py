"""Interactive Windows tray and attached-chat acceptance runner."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from amadeus_desktop.chat_models import ConversationState, MessageStatus
from amadeus_desktop.chat_provider import ScriptedChatProvider, ScriptedScenario
from amadeus_desktop.controller import ApplicationController
from amadeus_desktop.logging_config import close_logger, configure_logging
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.settings import SettingsRepository
from amadeus_desktop.single_instance import SingleInstance

_REQUIRED_BOOLEAN_CHECKS = (
    "system_tray_available",
    "tray_visible",
    "pet_initially_visible",
    "chat_initially_hidden",
    "chat_visible_after_pet_click",
    "chat_input_focused",
    "mock_turn_completed",
    "stop_action_passed",
    "failed_turn_preserved_partial",
    "retry_reused_turn_and_messages",
    "retry_completed",
    "normal_chat_panel_snapshot",
    "chat_panel_snapshot",
    "pet_hidden_after_tray_toggle",
    "chat_hidden_with_pet",
    "pet_visible_after_double_click",
    "chat_remains_hidden_after_double_click",
    "conversation_worker_clean",
    "conversation_timers_clean",
    "graceful_exit",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-app-data", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--visible-ms", type=int, default=1_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    application = QApplication([])
    application.setApplicationName("Amadeus P3 acceptance")
    application.setQuitOnLastWindowClosed(False)

    instance = SingleInstance(f"amadeus-tray-acceptance-{uuid4().hex}")
    if not instance.acquire():
        return 2

    paths = AppPaths.for_current_user(args.local_app_data)
    paths.initialize()
    repository = SettingsRepository(paths.settings_file)
    settings = repository.load_or_create()
    logger = configure_logging(paths.log_file, logger_name="amadeus.tray.acceptance")

    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    provider = ScriptedChatProvider()
    controller = ApplicationController(
        application,
        instance,
        logger,
        paths=paths,
        settings_repository=repository,
        settings=settings,
        tray_available=tray_available,
        chat_provider=provider,
        status_message="P3 Windows 托盘、桌宠与本地模拟聊天验收正在运行。",
    )

    result: dict[str, object] = {
        "system_tray_available": tray_available,
        "tray_visible": controller.tray is not None and controller.tray.is_visible,
        "pet_initially_visible": controller.pet_window.isVisible(),
        "chat_initially_hidden": not controller.chat_panel.isVisible(),
        "chat_visible_after_pet_click": False,
        "chat_input_focused": False,
        "chat_open_latency_ms": None,
        "mock_turn_completed": False,
        "stop_action_passed": False,
        "failed_turn_preserved_partial": False,
        "retry_reused_turn_and_messages": False,
        "retry_completed": False,
        "normal_chat_panel_snapshot": False,
        "chat_panel_snapshot": False,
        "pet_hidden_after_tray_toggle": False,
        "chat_hidden_with_pet": False,
        "pet_visible_after_double_click": False,
        "chat_remains_hidden_after_double_click": False,
        "conversation_worker_clean": False,
        "conversation_timers_clean": False,
        "acceptance_timed_out": False,
        "graceful_exit": False,
    }
    opened_at = 0.0

    def open_chat() -> None:
        nonlocal opened_at
        opened_at = time.perf_counter()
        controller.pet_window.clicked.emit()
        QTimer.singleShot(50, controller.chat_panel, record_open)

    def record_open() -> None:
        result["chat_visible_after_pet_click"] = controller.chat_panel.isVisible()
        result["chat_input_focused"] = controller.chat_panel.input.hasFocus()
        result["chat_open_latency_ms"] = (time.perf_counter() - opened_at) * 1_000
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(str(os.getpid()), encoding="utf-8")
        controller.chat_panel.send_requested.emit("P3 本地模拟验收消息")
        QTimer.singleShot(25, controller.chat_panel, wait_for_turn)

    def wait_for_turn() -> None:
        if controller.conversation.state is not ConversationState.IDLE:
            QTimer.singleShot(25, controller.chat_panel, wait_for_turn)
            return
        turns = controller.conversation.turns
        result["mock_turn_completed"] = bool(
            turns and turns[-1].assistant_message.status is MessageStatus.COMPLETED
        )
        normal_snapshot = args.result_file.with_name("chat-panel-normal.png")
        result["normal_chat_panel_snapshot"] = controller.chat_panel.grab().save(
            str(normal_snapshot), "PNG"
        )
        provider.scenario = ScriptedScenario.NEVER
        controller.chat_panel.send_requested.emit("P3 可见停止验收消息")
        QTimer.singleShot(25, controller.chat_panel, wait_to_stop)

    def wait_to_stop() -> None:
        if controller.conversation.state is not ConversationState.WAITING_FIRST_CHUNK:
            QTimer.singleShot(25, controller.chat_panel, wait_to_stop)
            return
        controller.chat_panel.action_button.click()
        QTimer.singleShot(25, controller.chat_panel, wait_for_stopped)

    def wait_for_stopped() -> None:
        if controller.conversation.state is not ConversationState.IDLE:
            QTimer.singleShot(25, controller.chat_panel, wait_for_stopped)
            return
        stopped = controller.conversation.turns[-1]
        result["stop_action_passed"] = (
            stopped.assistant_message.status is MessageStatus.STOPPED
            and not controller.conversation.has_running_worker
            and not controller.conversation.has_active_timers
        )
        provider.scenario = ScriptedScenario.PARTIAL_ERROR
        controller.chat_panel.send_requested.emit("P3 可见失败重试验收消息")
        QTimer.singleShot(25, controller.chat_panel, wait_for_failure)

    def wait_for_failure() -> None:
        if controller.conversation.state is not ConversationState.IDLE:
            QTimer.singleShot(25, controller.chat_panel, wait_for_failure)
            return
        failed = controller.conversation.turns[-1]
        if failed.assistant_message.status is not MessageStatus.FAILED:
            QTimer.singleShot(25, controller.chat_panel, wait_for_failure)
            return
        original_ids = (
            failed.turn_id,
            failed.user_message.message_id,
            failed.assistant_message.message_id,
        )
        result["failed_turn_preserved_partial"] = bool(failed.assistant_message.content)
        provider.scenario = ScriptedScenario.NORMAL
        bubble = controller.chat_panel.message_widget(failed.assistant_message.message_id)
        if bubble is None or not bubble.retry_button.isEnabled():
            controller.request_exit()
            return
        bubble.retry_button.click()
        QTimer.singleShot(
            25,
            controller.chat_panel,
            lambda: wait_for_retry(original_ids),
        )

    def wait_for_retry(original_ids: tuple[str, str, str]) -> None:
        if controller.conversation.state is not ConversationState.IDLE:
            QTimer.singleShot(
                25,
                controller.chat_panel,
                lambda: wait_for_retry(original_ids),
            )
            return
        retried = controller.conversation.turns[-1]
        current_ids = (
            retried.turn_id,
            retried.user_message.message_id,
            retried.assistant_message.message_id,
        )
        result["retry_reused_turn_and_messages"] = current_ids == original_ids
        result["retry_completed"] = (
            retried.attempt == 2 and retried.assistant_message.status is MessageStatus.COMPLETED
        )
        record_corner_placements()
        snapshot_path = args.result_file.with_name("chat-panel.png")
        result["chat_panel_snapshot"] = controller.chat_panel.grab().save(str(snapshot_path), "PNG")
        QTimer.singleShot(max(0, args.visible_ms), hide_from_tray)

    def record_corner_placements() -> None:
        screen = application.primaryScreen()
        if screen is None:
            result["screen"] = None
            result["four_corner_placements"] = []
            return
        work_area = screen.availableGeometry()
        result["screen"] = {
            "name": screen.name(),
            "device_pixel_ratio": screen.devicePixelRatio(),
            "logical_dpi": screen.logicalDotsPerInch(),
            "available_geometry": _rect_document(work_area),
        }
        original_position = controller.pet_window.pos()
        corners = {
            "top_left": (work_area.left(), work_area.top()),
            "top_right": (
                work_area.right() - controller.pet_window.width() + 1,
                work_area.top(),
            ),
            "bottom_left": (
                work_area.left(),
                work_area.bottom() - controller.pet_window.height() + 1,
            ),
            "bottom_right": (
                work_area.right() - controller.pet_window.width() + 1,
                work_area.bottom() - controller.pet_window.height() + 1,
            ),
        }
        placements: list[dict[str, object]] = []
        for name, (x, y) in corners.items():
            controller.pet_window.move(x, y)
            application.processEvents()
            controller._reposition_chat_panel(force=True)
            pet_geometry = controller.pet_window.geometry()
            panel_geometry = controller.chat_panel.geometry()
            side = "left" if panel_geometry.right() < pet_geometry.left() else "right"
            placements.append(
                {
                    "corner": name,
                    "side": side,
                    "pet_geometry": _rect_document(pet_geometry),
                    "panel_geometry": _rect_document(panel_geometry),
                    "panel_fully_visible": work_area.contains(panel_geometry),
                }
            )
        result["four_corner_placements"] = placements
        controller.pet_window.move(original_position)
        controller._reposition_chat_panel(force=True)

    def hide_from_tray() -> None:
        if controller.tray is not None:
            controller.tray.toggle_action.trigger()
        else:
            controller.toggle_pet()
        result["pet_hidden_after_tray_toggle"] = not controller.pet_window.isVisible()
        result["chat_hidden_with_pet"] = not controller.chat_panel.isVisible()
        QTimer.singleShot(150, controller.chat_panel, simulate_double_click)

    def simulate_double_click() -> None:
        if controller.tray is not None:
            controller.tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        else:
            controller.show_pet()
        result["pet_visible_after_double_click"] = controller.pet_window.isVisible()
        result["chat_remains_hidden_after_double_click"] = not controller.chat_panel.isVisible()
        QTimer.singleShot(150, controller.chat_panel, controller.request_exit)

    def acceptance_timeout() -> None:
        result["acceptance_timed_out"] = True
        controller.request_exit()

    QTimer.singleShot(200, controller.chat_panel, open_chat)
    QTimer.singleShot(10_000, controller.chat_panel, acceptance_timeout)
    exit_code = application.exec()
    controller._cleanup()
    result["conversation_worker_clean"] = not controller.conversation.has_running_worker
    result["conversation_timers_clean"] = not controller.conversation.has_active_timers
    result["graceful_exit"] = exit_code == 0
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    args.result_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    close_logger(logger)
    if exit_code != 0:
        return exit_code
    return 0 if _acceptance_passed(result) else 1


def _rect_document(rect) -> list[int]:
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def _acceptance_passed(result: dict[str, object]) -> bool:
    if result.get("acceptance_timed_out") is not False:
        return False
    if any(result.get(name) is not True for name in _REQUIRED_BOOLEAN_CHECKS):
        return False
    latency = result.get("chat_open_latency_ms")
    if not isinstance(latency, int | float) or isinstance(latency, bool) or latency > 200:
        return False
    placements = result.get("four_corner_placements")
    return bool(
        isinstance(placements, list)
        and len(placements) == 4
        and all(
            isinstance(placement, dict) and placement.get("panel_fully_visible") is True
            for placement in placements
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
