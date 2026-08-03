"""Non-activating, short-lived proactive greeting bubble beside the pet."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from amadeus_desktop.proactive import GREETING_BUBBLE_TIMEOUT_MS


class GreetingBubble(QWidget):
    clicked = Signal()
    dismissed = Signal()

    def __init__(self, *, always_on_top: bool = True) -> None:
        self._always_on_top = bool(always_on_top)
        super().__init__(None, self._window_flags())
        self.setObjectName("greetingBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "#greetingBubble { background: #f8fafc; border: 1px solid #94a3b8; "
            "border-radius: 12px; } #greetingText { color: #0f172a; font-size: 14px; }"
        )
        self.label = QLabel()
        self.label.setObjectName("greetingText")
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.label.setMaximumWidth(280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(self.label)
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
        self.move(
            _bubble_position(
                pet_geometry,
                self.size().width(),
                self.size().height(),
                work_areas,
            )
        )
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
