"""Generate the deterministic CC0 generic fallback/test spritesheet."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen

FRAME_WIDTH = 96
FRAME_HEIGHT = 112
COLUMNS = 8
ROWS = 9


def draw_robot(
    painter: QPainter,
    column: int,
    row: int,
) -> None:
    center_x = column * FRAME_WIDTH + FRAME_WIDTH / 2
    top = row * FRAME_HEIGHT
    phase = column * math.pi / 3
    bob = round(math.sin(phase) * 2)
    offset_x = 0
    offset_y = bob
    accent = QColor("#22d3ee")
    expression = "normal"

    if row == 1:
        offset_x = column % 3 - 1
    elif row == 2:
        offset_x = 1 - column % 3
    elif row == 4:
        offset_y = -round(12 * math.sin(column * math.pi / 4))
    elif row == 5:
        accent = QColor("#fb7185")
        expression = "sad"
    elif row == 7:
        offset_y = 1

    cx = center_x + offset_x
    cy = top + 56 + offset_y
    outline = QPen(QColor("#0f172a"), 3)
    outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(outline)

    # Antenna and ear lights.
    painter.drawLine(QPointF(cx, cy - 39), QPointF(cx + 7, cy - 49))
    painter.setBrush(accent)
    painter.drawEllipse(QPointF(cx + 8, cy - 50), 3, 3)
    painter.drawEllipse(QPointF(cx - 29, cy - 21), 4, 7)
    painter.drawEllipse(QPointF(cx + 29, cy - 21), 4, 7)

    # Head and body.
    painter.setBrush(QColor("#e2e8f0"))
    painter.drawRoundedRect(QRectF(cx - 28, cy - 39, 56, 40), 13, 13)
    painter.setBrush(QColor("#334155"))
    painter.drawRoundedRect(QRectF(cx - 21, cy + 2, 42, 42), 10, 10)
    painter.setBrush(QColor("#0f172a"))
    painter.drawRoundedRect(QRectF(cx - 19, cy - 29, 38, 20), 7, 7)

    # Eyes communicate error; other rows blink deterministically.
    painter.setPen(QPen(accent, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    if expression == "sad":
        painter.drawLine(QPointF(cx - 11, cy - 18), QPointF(cx - 6, cy - 21))
        painter.drawLine(QPointF(cx + 6, cy - 21), QPointF(cx + 11, cy - 18))
    elif column % 6 == 1 and row in {0, 7, 8}:
        painter.drawLine(QPointF(cx - 12, cy - 18), QPointF(cx - 6, cy - 18))
        painter.drawLine(QPointF(cx + 6, cy - 18), QPointF(cx + 12, cy - 18))
    else:
        painter.drawPoint(QPointF(cx - 9, cy - 19))
        painter.drawPoint(QPointF(cx + 9, cy - 19))

    # Arms express the non-movement states.
    painter.setPen(outline)
    left_hand = QPointF(cx - 32, cy + 23)
    right_hand = QPointF(cx + 32, cy + 23)
    if row == 3:
        right_hand = QPointF(cx + 31 + (column % 2) * 4, cy - 18 - (column % 2) * 4)
    elif row == 5:
        left_hand = QPointF(cx - 13, cy + 36)
        right_hand = QPointF(cx + 13, cy + 36)
    elif row == 6:
        left_hand = QPointF(cx - 34 - column % 2 * 3, cy + 11)
        right_hand = QPointF(cx + 34 + (column + 1) % 2 * 3, cy + 11)
    elif row == 7:
        right_hand = QPointF(cx + 15, cy - 5)
    elif row == 8:
        right_hand = QPointF(cx + 18, cy - 9)
    painter.drawLine(QPointF(cx - 19, cy + 12), left_hand)
    painter.drawLine(QPointF(cx + 19, cy + 12), right_hand)
    painter.setBrush(QColor("#e2e8f0"))
    painter.drawEllipse(left_hand, 4, 4)
    painter.drawEllipse(right_hand, 4, 4)

    # Moving rows use alternating legs.
    leg_shift = 6 if row in {1, 2} and column % 2 else 0
    painter.drawLine(QPointF(cx - 11, cy + 42), QPointF(cx - 15 - leg_shift, cy + 51))
    painter.drawLine(QPointF(cx + 11, cy + 42), QPointF(cx + 15 + leg_shift, cy + 51))

    painter.setPen(QColor("#67e8f9"))
    font = QFont("Segoe UI", 17, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(QRectF(cx - 18, cy + 8, 36, 26), Qt.AlignmentFlag.AlignCenter, "A")

    if row == 7:
        painter.setPen(QPen(QColor("#fbbf24"), 3))
        for dot in range(3):
            painter.drawPoint(QPointF(cx - 8 + dot * 8, top + 8 + (column % 2)))
    elif row == 8:
        painter.setPen(QColor("#c084fc"))
        painter.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        painter.drawText(QRectF(cx + 22, top + 3, 26, 28), Qt.AlignmentFlag.AlignCenter, "?")


def main() -> int:
    _application = QGuiApplication.instance() or QGuiApplication([])
    output = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).parents[1]
        / "src"
        / "amadeus_desktop"
        / "resources"
        / "app_icon"
        / "spritesheet.png"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(FRAME_WIDTH * COLUMNS, FRAME_HEIGHT * ROWS, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for row in range(ROWS):
        frame_count = (6, 8, 8, 4, 5, 8, 6, 6, 6)[row]
        for column in range(frame_count):
            draw_robot(painter, column, row)
    painter.end()
    if not image.save(str(output), "PNG"):
        raise RuntimeError(f"Could not write {output}")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
