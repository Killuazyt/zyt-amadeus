"""Transparent, non-activating desktop-pet window."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QBitmap,
    QHideEvent,
    QImage,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QRegion,
    QShowEvent,
)
from PySide6.QtWidgets import QApplication, QWidget

from amadeus_desktop.animation import AnimationController
from amadeus_desktop.pet_assets import LoadedPetAsset
from amadeus_desktop.pet_models import FrameCoordinate

MIN_SCALE_PERCENT = 50
MAX_SCALE_PERCENT = 200


class PetWindow(QWidget):
    """Render sprite frames and own click-versus-drag input semantics."""

    clicked = Signal()
    drag_started = Signal()
    drag_direction_changed = Signal(str)
    drag_finished = Signal(object)
    position_changed = Signal(object)
    visibility_changed = Signal(bool)

    def __init__(
        self,
        asset: LoadedPetAsset,
        *,
        scale_percent: int = 100,
        animation_speed_percent: int = 100,
        always_on_top: bool = True,
    ) -> None:
        super().__init__()
        self.asset = asset
        self.animation = AnimationController(
            asset.manifest,
            speed_percent=animation_speed_percent,
        )
        self._always_on_top = bool(always_on_top)
        self.scale_percent = max(MIN_SCALE_PERCENT, min(scale_percent, MAX_SCALE_PERCENT))
        self._sheet = QImage(str(asset.spritesheet_path))
        if self._sheet.isNull():
            raise ValueError("The validated pet spritesheet could not be loaded.")
        self._frame_cache: dict[tuple[FrameCoordinate, int], tuple[QPixmap, QRegion]] = {}
        self._current_pixmap = QPixmap()
        self._press_global: QPoint | None = None
        self._press_window_position: QPoint | None = None
        self._last_global: QPoint | None = None
        self._dragging = False
        self._drag_direction: str | None = None

        self.setWindowFlags(self._window_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(False)
        self.setWindowTitle(asset.manifest.display_name)

        self.animation.frame_changed.connect(self._set_frame)
        self._resize_for_scale()
        self._set_frame(self.animation.current_frame)

    @property
    def is_dragging(self) -> bool:
        return self._dragging

    @property
    def current_frame(self) -> FrameCoordinate:
        return self.animation.current_frame

    def show_without_activate(self) -> None:
        self.show()
        self.raise_()

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

    def set_animation_speed_percent(self, value: int) -> None:
        self.animation.set_speed_percent(value)

    def replace_asset(self, asset: LoadedPetAsset) -> None:
        """Replace a validated sprite asset while preserving placement and hot settings."""

        was_running = self.animation.is_running
        speed_percent = self.animation.speed_percent
        previous = self.geometry()
        self.animation.pause()
        self.animation.frame_changed.disconnect(self._set_frame)
        self.asset = asset
        self._sheet = QImage(str(asset.spritesheet_path))
        if self._sheet.isNull():
            raise ValueError("The validated pet spritesheet could not be loaded.")
        self.animation.deleteLater()
        self.animation = AnimationController(asset.manifest, speed_percent=speed_percent)
        self.animation.frame_changed.connect(self._set_frame)
        self._frame_cache.clear()
        self.setWindowTitle(asset.manifest.display_name)
        self._resize_for_scale()
        self._set_frame(self.animation.current_frame)
        self.move(
            round(previous.center().x() - self.width() / 2),
            previous.bottom() - self.height() + 1,
        )
        if was_running:
            self.animation.start()

    def set_scale_percent(self, value: int) -> None:
        value = max(MIN_SCALE_PERCENT, min(int(value), MAX_SCALE_PERCENT))
        if value == self.scale_percent:
            return
        previous = self.geometry()
        self.scale_percent = value
        self._frame_cache.clear()
        self._resize_for_scale()
        self._set_frame(self.animation.current_frame)
        self.move(
            round(previous.center().x() - self.width() / 2),
            previous.bottom() - self.height() + 1,
        )

    def _resize_for_scale(self) -> None:
        spec = self.asset.manifest.spritesheet
        width = max(1, round(spec.frame_width * self.scale_percent / 100))
        height = max(1, round(spec.frame_height * self.scale_percent / 100))
        self.setFixedSize(width, height)

    def _window_flags(self) -> Qt.WindowType:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        return flags

    def _source_frame(self, frame: FrameCoordinate) -> QImage:
        spec = self.asset.manifest.spritesheet
        return self._sheet.copy(
            frame.column * spec.frame_width,
            frame.row * spec.frame_height,
            spec.frame_width,
            spec.frame_height,
        )

    def _rendered_frame(self, frame: FrameCoordinate) -> tuple[QPixmap, QRegion]:
        key = (frame, self.scale_percent)
        cached = self._frame_cache.get(key)
        if cached is not None:
            return cached
        scaled = self._source_frame(frame).scaled(
            self.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        pixmap = QPixmap.fromImage(scaled)
        region = self._alpha_region(scaled)
        rendered = (pixmap, region)
        self._frame_cache[key] = rendered
        return rendered

    def _alpha_region(self, image: QImage) -> QRegion:
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        bits = rgba.constBits()
        thresholded = QImage(rgba.size(), QImage.Format.Format_ARGB32)
        thresholded.fill(Qt.GlobalColor.transparent)
        threshold = self.asset.manifest.spritesheet.alpha_threshold
        for y in range(rgba.height()):
            row_start = y * rgba.bytesPerLine()
            for x in range(rgba.width()):
                if bits[row_start + x * 4 + 3] >= threshold:
                    thresholded.setPixel(x, y, 0xFF000000)
        region = QRegion(QBitmap.fromImage(thresholded.createAlphaMask()))
        padding = max(
            0,
            round(self.asset.manifest.spritesheet.hit_padding * self.scale_percent / 100),
        )
        if padding:
            original = QRegion(region)
            for dx in range(-padding, padding + 1):
                for dy in range(-padding, padding + 1):
                    region |= original.translated(dx, dy)
        return region.intersected(QRegion(QRect(QPoint(0, 0), self.size())))

    def _set_frame(self, frame: FrameCoordinate) -> None:
        self._current_pixmap, region = self._rendered_frame(frame)
        self.setMask(region)
        self.update()

    def _set_drag_direction(self, direction: str | None) -> None:
        if direction == self._drag_direction:
            return
        if self._drag_direction:
            self.animation.set_activity(self._drag_direction, False)
        self._drag_direction = direction
        if direction:
            self.animation.set_activity(direction, True)
            self.drag_direction_changed.emit(direction)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API name
        del event
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawPixmap(0, 0, self._current_pixmap)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API name
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._press_global = event.globalPosition().toPoint()
        self._last_global = self._press_global
        self._press_window_position = self.pos()
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API name
        if self._press_global is None or self._press_window_position is None:
            event.ignore()
            return
        current = event.globalPosition().toPoint()
        total_delta = current - self._press_global
        if not self._dragging:
            threshold = QApplication.startDragDistance()
            if total_delta.manhattanLength() <= threshold:
                event.accept()
                return
            self._dragging = True
            self.drag_started.emit()
        self.move(self._press_window_position + total_delta)
        if self._last_global is not None:
            horizontal = current.x() - self._last_global.x()
            if horizontal < 0:
                self._set_drag_direction("move_left")
            elif horizontal > 0:
                self._set_drag_direction("move_right")
        self._last_global = current
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API name
        if event.button() != Qt.MouseButton.LeftButton or self._press_global is None:
            event.ignore()
            return
        if self._dragging:
            self._set_drag_direction(None)
            self.drag_finished.emit(self.pos())
        else:
            self.clicked.emit()
        self._press_global = None
        self._press_window_position = None
        self._last_global = None
        self._dragging = False
        event.accept()

    def moveEvent(self, event: QMoveEvent) -> None:  # noqa: N802 - Qt API name
        super().moveEvent(event)
        self.position_changed.emit(self.pos())

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API name
        super().showEvent(event)
        self.animation.start()
        self.visibility_changed.emit(True)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API name
        super().hideEvent(event)
        self.animation.pause()
        self.visibility_changed.emit(False)
