"""User-triggered one-shot multi-screen region screenshot overlay."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


class RegionScreenshotOverlay(QWidget):
    captured = Signal(object)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._snapshot = None
        self._origin = QPoint()
        self._current = QPoint()
        self._dragging = False

    def begin(self) -> bool:
        screens = self.screen().virtualSiblings() if self.screen() is not None else []
        if not screens:
            self.cancelled.emit()
            self.close()
            return False
        bounds = QRect()
        for screen in screens:
            bounds = bounds.united(screen.geometry())
        if bounds.isEmpty():
            self.cancelled.emit()
            self.close()
            return False
        from PySide6.QtGui import QImage

        snapshot = QImage(bounds.size(), QImage.Format.Format_RGB888)
        snapshot.fill(QColor("black"))
        painter = QPainter(snapshot)
        try:
            for screen in screens:
                pixmap = screen.grabWindow(0)
                if pixmap.isNull():
                    continue
                target = screen.geometry().translated(-bounds.topLeft())
                painter.drawPixmap(target, pixmap)
        finally:
            painter.end()
        self._snapshot = snapshot
        self.setGeometry(bounds)
        self.showFullScreen() if len(screens) == 1 else self.show()
        self.raise_()
        self.activateWindow()
        return True

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        if self._snapshot is None:
            return
        painter = QPainter(self)
        painter.drawImage(self.rect(), self._snapshot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 105))
        selection = self._selection()
        if not selection.isEmpty():
            painter.drawImage(selection, self._snapshot, selection)
            painter.setPen(QPen(QColor("#22d3ee"), 2))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
        painter.setPen(QColor("white"))
        painter.drawText(20, 30, "拖动选择截图区域；Esc 取消")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() is not Qt.MouseButton.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self._dragging = True
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if not self._dragging:
            return
        self._current = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if not self._dragging or event.button() is not Qt.MouseButton.LeftButton:
            return
        self._current = event.position().toPoint()
        self._dragging = False
        selection = self._selection().intersected(self.rect())
        if selection.width() < 8 or selection.height() < 8 or self._snapshot is None:
            self.update()
            return
        self.captured.emit(self._snapshot.copy(selection))
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def _selection(self) -> QRect:
        return QRect(self._origin, self._current).normalized()
