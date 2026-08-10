"""Fixed-clock desktop-pet animation state controller."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from amadeus_desktop.pet_models import FrameCoordinate, PetManifest

STATE_PRIORITY = {
    "idle": 0,
    "greeting": 100,
    "jump": 100,
    "thinking": 305,
    "waiting": 300,
    "responding": 310,
    "move_left": 400,
    "move_right": 400,
    "error": 500,
}


class AnimationController(QObject):
    """Resolve concurrent states and advance frames from a monotonic clock."""

    frame_changed = Signal(object)
    state_changed = Signal(str)

    def __init__(self, manifest: PetManifest, *, speed_percent: int = 100) -> None:
        super().__init__()
        self.manifest = manifest
        self._speed_percent = _validated_speed_percent(speed_percent)
        self._active_states: set[str] = set()
        self._transient_state: str | None = None
        self._state = "idle"
        self._animation = manifest.animation("idle")
        self._frame_index = 0
        self._running = False
        self._deadline_ns = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance_from_clock)

    @property
    def state(self) -> str:
        return self._state

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def current_frame(self) -> FrameCoordinate:
        return self._animation.frames[self._frame_index]

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def speed_percent(self) -> int:
        return self._speed_percent

    def set_speed_percent(self, value: int) -> None:
        """Apply a bounded playback multiplier without resetting the active action."""

        normalized = _validated_speed_percent(value)
        if normalized == self._speed_percent:
            return
        self._speed_percent = normalized
        if self._running:
            self._reset_deadline()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.frame_changed.emit(self.current_frame)
        self._reset_deadline()

    def pause(self) -> None:
        self._running = False
        self._timer.stop()

    def set_activity(self, state: str, active: bool) -> None:
        if state == "idle":
            return
        if active:
            self._active_states.add(state)
        else:
            self._active_states.discard(state)
        self._resolve_state()

    def trigger(self, state: str) -> None:
        if state == "idle":
            return
        self._transient_state = state
        self._resolve_state()

    def clear_transient(self) -> None:
        """Cancel only short feedback while preserving ongoing activities."""

        self._transient_state = None
        self._resolve_state()

    def clear(self) -> None:
        self._active_states.clear()
        self._transient_state = None
        self._switch_state("idle")

    def _resolve_state(self) -> None:
        candidates = set(self._active_states)
        if self._transient_state:
            candidates.add(self._transient_state)
        target = max(candidates, key=lambda item: STATE_PRIORITY.get(item, 0), default="idle")
        self._switch_state(target)

    def _switch_state(self, state: str) -> None:
        animation = self.manifest.animation(state)
        resolved_state = animation.name
        if resolved_state == self._state and animation == self._animation:
            return
        self._state = resolved_state
        self._animation = animation
        self._frame_index = 0
        self.state_changed.emit(self._state)
        self.frame_changed.emit(self.current_frame)
        if self._running:
            self._reset_deadline()

    def _period_ns(self) -> int:
        return max(
            1,
            round(1_000_000_000 * 100 / (self._animation.fps * self._speed_percent)),
        )

    def _reset_deadline(self) -> None:
        self._timer.stop()
        self._deadline_ns = time.monotonic_ns() + self._period_ns()
        self._schedule_timer()

    def _schedule_timer(self) -> None:
        if not self._running:
            return
        remaining_ns = max(0, self._deadline_ns - time.monotonic_ns())
        self._timer.start(max(1, math.ceil(remaining_ns / 1_000_000)))

    def _advance_from_clock(self) -> None:
        if not self._running:
            return
        now = time.monotonic_ns()
        period = self._period_ns()
        steps = max(1, 1 + max(0, now - self._deadline_ns) // period)
        self._deadline_ns += steps * period

        for _ in range(int(steps)):
            if self._frame_index + 1 < len(self._animation.frames):
                self._frame_index += 1
            elif self._animation.loop:
                self._frame_index = 0
            else:
                completed = self._state
                fallback = self._animation.fallback
                if self._transient_state == completed:
                    self._transient_state = None
                if self._active_states:
                    self._resolve_state()
                else:
                    self._switch_state(fallback)
                return
        self.frame_changed.emit(self.current_frame)
        self._schedule_timer()


def _validated_speed_percent(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 50 <= value <= 200:
        raise ValueError("animation speed percent must be between 50 and 200")
    return value
