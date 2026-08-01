from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from amadeus_desktop.pet_assets import PetAssetService
from amadeus_desktop.ui.pet_window import PetWindow


def make_window(tmp_path) -> PetWindow:
    asset = PetAssetService(tmp_path / "pets").load_builtin()
    return PetWindow(asset)


def send_mouse(
    window: PetWindow,
    event_type: QEvent.Type,
    global_point: QPoint,
    button: Qt.MouseButton,
    buttons: Qt.MouseButton,
) -> None:
    local = global_point - window.pos()
    event = QMouseEvent(
        event_type,
        QPointF(local),
        QPointF(global_point),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    if event_type == QEvent.Type.MouseButtonPress:
        window.mousePressEvent(event)
    elif event_type == QEvent.Type.MouseMove:
        window.mouseMoveEvent(event)
    else:
        window.mouseReleaseEvent(event)


def test_window_flags_and_alpha_hit_region(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)

    flags = window.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.Tool
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert window.mask().contains(QPoint(window.width() // 2, round(window.height() * 0.65)))
    assert not window.mask().contains(QPoint(0, 0))


def test_show_starts_and_hide_pauses_animation(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    assert window.animation.is_running is False

    window.show()
    qtbot.waitUntil(window.isVisible)
    assert window.animation.is_running is True

    window.hide()
    assert window.animation.is_running is False


def test_scale_preserves_bottom_and_is_clamped(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    window.move(300, 400)
    old_bottom = window.geometry().bottom()

    window.set_scale_percent(150)

    assert window.scale_percent == 150
    assert window.geometry().bottom() == old_bottom
    window.set_scale_percent(999)
    assert window.scale_percent == 200


def test_click_and_drag_signals_are_exclusive_for_one_hundred_sequences(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    window.move(100, 100)
    clicks: list[bool] = []
    starts: list[bool] = []
    finishes: list[QPoint] = []
    directions: list[str] = []
    window.clicked.connect(lambda: clicks.append(True))
    window.drag_started.connect(lambda: starts.append(True))
    window.drag_finished.connect(finishes.append)
    window.drag_direction_changed.connect(directions.append)

    for _ in range(50):
        point = window.pos() + window.rect().center()
        send_mouse(
            window,
            QEvent.Type.MouseButtonPress,
            point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        send_mouse(
            window,
            QEvent.Type.MouseButtonRelease,
            point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )

    for index in range(50):
        start = window.pos() + window.rect().center()
        direction = 1 if index % 2 == 0 else -1
        end = start + QPoint(direction * (qapp.startDragDistance() + 20), 3)
        send_mouse(
            window,
            QEvent.Type.MouseButtonPress,
            start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        send_mouse(
            window,
            QEvent.Type.MouseMove,
            end,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        send_mouse(
            window,
            QEvent.Type.MouseButtonRelease,
            end,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )

    assert len(clicks) == 50
    assert len(starts) == 50
    assert len(finishes) == 50
    assert "move_left" in directions
    assert "move_right" in directions


def test_drag_keeps_original_mouse_anchor(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    window.move(200, 300)
    start_position = QPoint(window.pos())
    press = window.pos() + QPoint(30, 40)
    end = press + QPoint(75, -20)

    send_mouse(
        window,
        QEvent.Type.MouseButtonPress,
        press,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    send_mouse(
        window,
        QEvent.Type.MouseMove,
        end,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
    )

    assert window.pos() == start_position + QPoint(75, -20)


def test_click_only_emits_intent_and_does_not_choose_feedback(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    clicks: list[bool] = []
    window.clicked.connect(lambda: clicks.append(True))
    point = window.pos() + window.rect().center()

    send_mouse(
        window,
        QEvent.Type.MouseButtonPress,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )
    send_mouse(
        window,
        QEvent.Type.MouseButtonRelease,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )

    assert clicks == [True]
    assert window.animation.state == "idle"


def test_position_signal_follows_programmatic_moves(qapp, qtbot, tmp_path) -> None:
    window = make_window(tmp_path)
    qtbot.addWidget(window)
    positions: list[QPoint] = []
    window.position_changed.connect(positions.append)
    window.show()
    qtbot.waitUntil(window.isVisible)

    window.move(240, 180)

    qtbot.waitUntil(lambda: bool(positions) and positions[-1] == QPoint(240, 180))
