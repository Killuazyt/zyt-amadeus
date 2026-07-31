"""DPI-independent pet placement calculations."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtGui import QScreen

from amadeus_desktop.pet_models import PetPosition

DEFAULT_EDGE_MARGIN = 24


@dataclass(frozen=True, slots=True)
class ScreenGeometry:
    identifier: str
    available: QRect
    primary: bool = False


def screen_identifier(screen: QScreen) -> str:
    """Build the most stable identifier Qt exposes without platform APIs."""

    serial = screen.serialNumber().strip()
    if serial:
        return f"serial:{serial}"
    components = [
        screen.manufacturer().strip(),
        screen.model().strip(),
        screen.name().strip(),
    ]
    return "display:" + "|".join(component for component in components if component)


def screen_geometry(screen: QScreen, *, primary: bool = False) -> ScreenGeometry:
    return ScreenGeometry(screen_identifier(screen), QRect(screen.availableGeometry()), primary)


def clamp_top_left(point: QPoint, size: QSize, available: QRect) -> QPoint:
    max_x = max(available.left(), available.right() - size.width() + 1)
    max_y = max(available.top(), available.bottom() - size.height() + 1)
    return QPoint(
        min(max(point.x(), available.left()), max_x),
        min(max(point.y(), available.top()), max_y),
    )


def default_top_left(size: QSize, available: QRect) -> QPoint:
    desired = QPoint(
        available.right() - size.width() + 1 - DEFAULT_EDGE_MARGIN,
        available.bottom() - size.height() + 1 - DEFAULT_EDGE_MARGIN,
    )
    return clamp_top_left(desired, size, available)


def capture_position(point: QPoint, size: QSize, screen: ScreenGeometry) -> PetPosition:
    point = clamp_top_left(point, size, screen.available)
    travel_x = max(0, screen.available.width() - size.width())
    travel_y = max(0, screen.available.height() - size.height())
    x_ratio = 0.0 if travel_x == 0 else (point.x() - screen.available.left()) / travel_x
    y_ratio = 0.0 if travel_y == 0 else (point.y() - screen.available.top()) / travel_y
    return PetPosition(screen.identifier, x_ratio, y_ratio)


def restore_top_left(
    position: PetPosition | None,
    size: QSize,
    screens: list[ScreenGeometry],
) -> QPoint:
    if not screens:
        return QPoint(0, 0)

    target = (
        next((screen for screen in screens if screen.identifier == position.screen_id), None)
        if position
        else None
    )
    if target is None:
        target = next((screen for screen in screens if screen.primary), screens[0])
    if position is None or target.identifier != position.screen_id:
        return default_top_left(size, target.available)

    travel_x = max(0, target.available.width() - size.width())
    travel_y = max(0, target.available.height() - size.height())
    point = QPoint(
        target.available.left() + round(travel_x * position.x_ratio),
        target.available.top() + round(travel_y * position.y_ratio),
    )
    return clamp_top_left(point, size, target.available)


def nearest_screen(point: QPoint, screens: list[ScreenGeometry]) -> ScreenGeometry:
    if not screens:
        raise ValueError("At least one screen is required.")
    for screen in screens:
        if screen.available.contains(point):
            return screen

    def distance(screen: ScreenGeometry) -> int:
        rect = screen.available
        dx = max(rect.left() - point.x(), 0, point.x() - rect.right())
        dy = max(rect.top() - point.y(), 0, point.y() - rect.bottom())
        return dx + dy

    return min(screens, key=distance)
