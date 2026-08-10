"""Non-activating, short-lived proactive greeting bubble beside the pet."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from amadeus_desktop.proactive import GREETING_BUBBLE_TIMEOUT_MS

_TAIL_WIDTH = 11
_HORIZONTAL_PADDING = 14
_VERTICAL_PADDING = 10
_CORNER_RADIUS = 12.0
_BUBBLE_FILL = QColor("#f8fafc")
_BUBBLE_BORDER = QColor("#64748b")


class GreetingBubble(QWidget):
    clicked = Signal()
    dismissed = Signal()

    def __init__(self, *, always_on_top: bool = True) -> None:
        self._always_on_top = bool(always_on_top)
        super().__init__(None, self._window_flags())
        self.setObjectName("greetingBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("#greetingText { color: #0f172a; font-size: 14px; }")
        self._tail_side = "left"
        self._tail_center_y = 0.0
        self.label = QLabel()
        self.label.setObjectName("greetingText")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.label.setMaximumWidth(280)
        self._layout = QVBoxLayout(self)
        self._apply_content_margins()
        self._layout.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timeout)
        self._dismissed_emitted = False

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    def set_always_on_top(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._always_on_top:
            return
        visible = self.isVisible()
        position = self.pos()
        self._always_on_top = enabled
        self.setWindowFlags(self._window_flags())
        self.move(position)
        if visible:
            self.show_without_activate()

    def show_message(
        self,
        text: str,
        pet_geometry: QRect,
        work_areas: Sequence[QRect],
        *,
        timeout_ms: int = GREETING_BUBBLE_TIMEOUT_MS,
    ) -> None:
        normalized = str(text).strip()
        if not normalized:
            raise ValueError("greeting text must not be blank")
        if timeout_ms <= 0:
            raise ValueError("greeting timeout must be positive")
        self._timer.stop()
        self._dismissed_emitted = False
        self.label.setText(normalized)
        self.adjustSize()
        position = _bubble_position(
            pet_geometry,
            self.size().width(),
            self.size().height(),
            work_areas,
        )
        bubble_center_x = position.x() + (self.width() / 2)
        tail_side = "left" if bubble_center_x >= pet_geometry.center().x() else "right"
        if tail_side != self._tail_side:
            self._tail_side = tail_side
            self._apply_content_margins()
        self._tail_center_y = float(pet_geometry.center().y() - position.y())
        self.move(position)
        self.update()
        self.show_without_activate()
        self._timer.start(timeout_ms)

    def show_without_activate(self) -> None:
        self.show()
        self.raise_()

    def dismiss(self, *, emit_signal: bool = False) -> None:
        self._timer.stop()
        self.hide()
        if emit_signal and not self._dismissed_emitted:
            self._dismissed_emitted = True
            self.dismissed.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._timer.stop()
        self.hide()
        self.clicked.emit()
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(_BUBBLE_BORDER, 1.25))
        painter.setBrush(_BUBBLE_FILL)
        painter.drawPath(self._bubble_path())

    def _apply_content_margins(self) -> None:
        left = _HORIZONTAL_PADDING + (_TAIL_WIDTH if self._tail_side == "left" else 0)
        right = _HORIZONTAL_PADDING + (_TAIL_WIDTH if self._tail_side == "right" else 0)
        self._layout.setContentsMargins(left, _VERTICAL_PADDING, right, _VERTICAL_PADDING)

    def _bubble_path(self) -> QPainterPath:
        half_border = 0.75
        outer = QRectF(self.rect()).adjusted(
            half_border,
            half_border,
            -half_border,
            -half_border,
        )
        if self._tail_side == "left":
            body = outer.adjusted(_TAIL_WIDTH, 0.0, 0.0, 0.0)
        else:
            body = outer.adjusted(0.0, 0.0, -_TAIL_WIDTH, 0.0)

        radius = min(_CORNER_RADIUS, max(1.0, (body.height() / 2) - 1.0))
        tail_half_height = min(8.0, max(4.0, (body.height() - (2 * radius)) / 2))
        minimum_tail_y = body.top() + radius + tail_half_height
        maximum_tail_y = body.bottom() - radius - tail_half_height
        if minimum_tail_y <= maximum_tail_y:
            tail_y = max(minimum_tail_y, min(self._tail_center_y, maximum_tail_y))
        else:
            tail_y = body.center().y()

        path = QPainterPath()
        path.moveTo(body.left() + radius, body.top())
        path.lineTo(body.right() - radius, body.top())
        path.quadTo(body.right(), body.top(), body.right(), body.top() + radius)
        if self._tail_side == "right":
            path.lineTo(body.right(), tail_y - tail_half_height)
            path.lineTo(outer.right(), tail_y)
            path.lineTo(body.right(), tail_y + tail_half_height)
        path.lineTo(body.right(), body.bottom() - radius)
        path.quadTo(body.right(), body.bottom(), body.right() - radius, body.bottom())
        path.lineTo(body.left() + radius, body.bottom())
        path.quadTo(body.left(), body.bottom(), body.left(), body.bottom() - radius)
        if self._tail_side == "left":
            path.lineTo(body.left(), tail_y + tail_half_height)
            path.lineTo(outer.left(), tail_y)
            path.lineTo(body.left(), tail_y - tail_half_height)
        path.lineTo(body.left(), body.top() + radius)
        path.quadTo(body.left(), body.top(), body.left() + radius, body.top())
        path.closeSubpath()
        return path

    def _timeout(self) -> None:
        self.dismiss(emit_signal=True)

    def _window_flags(self) -> Qt.WindowType:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        return flags


def _bubble_position(
    pet: QRect,
    width: int,
    height: int,
    work_areas: Sequence[QRect],
) -> QPoint:
    if not work_areas:
        return QPoint(pet.right() + 8, pet.top())
    center = pet.center()
    work = min(
        work_areas,
        key=lambda area: abs(area.center().x() - center.x()) + abs(area.center().y() - center.y()),
    )
    gap = 8
    right_x = pet.right() + gap
    left_x = pet.left() - gap - width
    x = right_x if right_x + width <= work.right() + 1 else left_x
    x = max(work.left(), min(x, work.right() - width + 1))
    y = pet.top() + round((pet.height() - height) / 2)
    y = max(work.top(), min(y, work.bottom() - height + 1))
    return QPoint(x, y)
