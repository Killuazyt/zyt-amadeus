from __future__ import annotations

from amadeus_desktop.animation import AnimationController
from amadeus_desktop.pet_assets import builtin_pet_root, validate_package


def make_controller() -> AnimationController:
    return AnimationController(validate_package(builtin_pet_root()).manifest)


def test_priority_and_same_state_do_not_restart(qapp) -> None:
    controller = make_controller()
    controller.set_activity("waiting", True)
    assert controller.state == "waiting"

    controller.set_activity("move_right", True)
    assert controller.state == "move_right"
    controller.set_activity("waiting", True)
    assert controller.state == "move_right"

    controller.set_activity("move_right", False)
    assert controller.state == "waiting"
    controller.set_activity("responding", True)
    assert controller.state == "responding"
    controller.set_activity("responding", False)
    assert controller.state == "waiting"
    controller.set_activity("waiting", False)
    assert controller.state == "idle"


def test_missing_action_falls_back_to_idle(qapp) -> None:
    controller = make_controller()

    controller.set_activity("not-present", True)

    assert controller.state == "idle"


def test_fixed_clock_advances_and_hidden_pause_stops(qapp, qtbot) -> None:
    controller = make_controller()
    frames: list[object] = []
    controller.frame_changed.connect(frames.append)

    controller.start()
    qtbot.waitUntil(lambda: controller.frame_index > 0, timeout=1000)
    controller.pause()
    paused_index = controller.frame_index
    paused_count = len(frames)
    qtbot.wait(220)

    assert controller.frame_index == paused_index
    assert len(frames) == paused_count


def test_transient_action_returns_to_active_state(qapp, qtbot) -> None:
    controller = make_controller()
    controller.set_activity("waiting", True)
    controller.trigger("error")
    controller.start()

    assert controller.state == "error"
    qtbot.waitUntil(lambda: controller.state == "waiting", timeout=2000)


def test_clear_transient_preserves_active_state(qapp) -> None:
    controller = make_controller()
    controller.set_activity("waiting", True)
    controller.trigger("error")

    controller.clear_transient()

    assert controller.state == "waiting"
