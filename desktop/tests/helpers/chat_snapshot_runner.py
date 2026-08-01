"""Capture a synthetic P3 chat-panel screenshot for DPI visual checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from amadeus_desktop.ui.chat_panel import ChatPanel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    application = QApplication([])
    panel = ChatPanel()
    user_bubble = panel.append_message(
        "synthetic-user",
        "user",
        "中文 English mixed\n\n连续换行与超长内容 " + "测试" * 40,
        status="completed",
    )
    assistant_bubble = panel.append_message(
        "synthetic-assistant",
        "assistant",
        "LongTokenWithoutSpaces" * 25,
        status="streaming",
    )
    screen = application.primaryScreen()
    if screen is not None:
        panel.resize_for_work_area(screen.availableGeometry())
    panel.show()
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    pixmap = panel.grab()
    saved = pixmap.save(str(args.output), "PNG")
    print(
        json.dumps(
            {
                "qt_scale_factor": os.environ.get("QT_SCALE_FACTOR"),
                "panel_size": [panel.width(), panel.height()],
                "snapshot_size": [pixmap.width(), pixmap.height()],
                "device_pixel_ratio": pixmap.devicePixelRatio(),
                "viewport_width": panel.scroll_area.viewport().width(),
                "bubble_widths": [user_bubble.width(), assistant_bubble.width()],
                "text_widths": [
                    user_bubble.text_view.width(),
                    assistant_bubble.text_view.width(),
                ],
                "horizontal_scroll_maxima": [
                    user_bubble.text_view.horizontalScrollBar().maximum(),
                    assistant_bubble.text_view.horizontalScrollBar().maximum(),
                ],
                "saved": saved,
            },
            ensure_ascii=False,
        )
    )
    panel.hide()
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
