"""Pure logical-coordinate geometry for the pet-attached chat panel."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QPoint, QRect, QSize

CHAT_PANEL_WIDTH = 380
CHAT_PANEL_HEIGHT = 560
CHAT_PANEL_EDGE_MARGIN = 16
CHAT_PANEL_GAP = 12

PanelSide = Literal["left", "right"]


@dataclass(frozen=True, slots=True)
class ChatPanelPlacement:
    """Resolved panel position and the work area used to calculate it."""

    top_left: QPoint
    side: PanelSide
    work_area: QRect


def compact_panel_size(
    work_area: QRect,
    *,
    preferred_width: int = CHAT_PANEL_WIDTH,
    preferred_height: int = CHAT_PANEL_HEIGHT,
    edge_margin: int = CHAT_PANEL_EDGE_MARGIN,
) -> QSize:
    """Return the compact panel size that fits inside a logical work area.

    Normal displays retain the fixed preferred width. The defensive width
    reduction only applies when the work area itself is narrower than the
    panel plus margins; height always adapts to short work areas.
    """

    if work_area.isEmpty():
        raise ValueError("The chat panel work area must not be empty.")
    if preferred_width <= 0 or preferred_height <= 0:
        raise ValueError("The preferred chat panel size must be positive.")
    if edge_margin < 0:
        raise ValueError("The chat panel edge margin must not be negative.")

    available_width = max(1, work_area.width() - edge_margin * 2)
    available_height = max(1, work_area.height() - edge_margin * 2)
    return QSize(
        min(preferred_width, available_width),
        min(preferred_height, available_height),
    )


def calculate_chat_panel_placement(
    pet_geometry: QRect,
    panel_size: QSize,
    work_areas: Sequence[QRect],
    *,
    gap: int = CHAT_PANEL_GAP,
) -> ChatPanelPlacement:
    """Place the panel beside the pet using logical Qt coordinates.

    The work area containing the pet centre is selected first. If none does,
    the nearest work area is used. A fully fitting right placement wins,
    followed by a fully fitting left placement. If neither side fits, the
    larger side is selected and the result is clamped into the work area.
    Vertically, the panel is bottom-aligned with the pet before clamping.
    """

    if not work_areas:
        raise ValueError("At least one chat panel work area is required.")
    if pet_geometry.isEmpty():
        raise ValueError("The pet geometry must not be empty.")
    if panel_size.width() <= 0 or panel_size.height() <= 0:
        raise ValueError("The chat panel size must be positive.")
    if gap < 0:
        raise ValueError("The chat panel gap must not be negative.")

    work_area = _work_area_for_point(pet_geometry.center(), work_areas)

    right_space = max(0, work_area.right() - pet_geometry.right() - gap)
    left_space = max(0, pet_geometry.left() - work_area.left() - gap)
    right_fits = panel_size.width() <= right_space
    left_fits = panel_size.width() <= left_space

    if right_fits:
        side: PanelSide = "right"
    elif left_fits:
        side = "left"
    else:
        side = "right" if right_space >= left_space else "left"

    if side == "right":
        desired_x = pet_geometry.right() + gap + 1
    else:
        desired_x = pet_geometry.left() - gap - panel_size.width()
    desired_y = pet_geometry.bottom() - panel_size.height() + 1

    max_x = max(work_area.left(), work_area.right() - panel_size.width() + 1)
    max_y = max(work_area.top(), work_area.bottom() - panel_size.height() + 1)
    top_left = QPoint(
        min(max(desired_x, work_area.left()), max_x),
        min(max(desired_y, work_area.top()), max_y),
    )
    return ChatPanelPlacement(top_left, side, QRect(work_area))


def _work_area_for_point(point: QPoint, work_areas: Sequence[QRect]) -> QRect:
    for work_area in work_areas:
        if work_area.contains(point):
            return work_area

    def distance(work_area: QRect) -> tuple[int, int]:
        dx = max(work_area.left() - point.x(), 0, point.x() - work_area.right())
        dy = max(work_area.top() - point.y(), 0, point.y() - work_area.bottom())
        return dx + dy, dx * dx + dy * dy

    return min(work_areas, key=distance)
