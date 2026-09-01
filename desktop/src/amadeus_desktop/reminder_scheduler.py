"""Local, one-shot scheduler for P7I temporal commitments.

The scheduler owns exactly one nearest-deadline timer and one 30-second
calibration timer.  It never creates a timer per reminder and never calls a
network provider.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from amadeus_desktop.local_data_service import LocalDataService
from amadeus_desktop.presence import PresenceProbe
from amadeus_desktop.storage_models import TemporalCommitment, TemporalCommitmentKind
from amadeus_desktop.temporal_commitments import TemporalDueSnapshot

_CALIBRATION_INTERVAL_MS = 30_000


class ReminderScheduler(QObject):
    """Surface due work only after durable state detection succeeds."""

    status_changed = Signal(str)

    def __init__(
        self,
        data: LocalDataService,
        *,
        presence_probe: PresenceProbe,
        notify: Callable[[str, str, object], bool],
        show_followup: Callable[[TemporalCommitment], bool],
        followup_safe: Callable[[], bool],
        clock: Callable[[], datetime],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._data = data
        self._presence_probe = presence_probe
        self._notify = notify
        self._show_followup = show_followup
        self._followup_safe = followup_safe
        self._clock = clock
        self._running = False
        self._scan_pending = False
        self._delivery_in_flight: set[str] = set()
        self._nearest_due_at: datetime | None = None

        self._nearest_timer = QTimer(self)
        self._nearest_timer.setSingleShot(True)
        self._nearest_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._nearest_timer.timeout.connect(self.wake)
        self._calibration_timer = QTimer(self)
        self._calibration_timer.setInterval(_CALIBRATION_INTERVAL_MS)
        self._calibration_timer.timeout.connect(self.wake)

        self._data.temporal_due_scanned.connect(self._on_due_scanned)
        self._data.temporal_commitment_changed.connect(self._on_commitment_changed)
        self._data.operation_failed.connect(self._on_data_operation_failed)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._calibration_timer.start()
        self.wake()

    def stop(self) -> None:
        self._running = False
        self._scan_pending = False
        self._delivery_in_flight.clear()
        self._nearest_due_at = None
        self._nearest_timer.stop()
        self._calibration_timer.stop()

    def wake(self) -> None:
        if not self._running or self._scan_pending:
            return
        self._scan_pending = bool(self._data.scan_due_temporal_commitments(now=self._clock()))
        if not self._scan_pending:
            self.status_changed.emit("storage_unavailable")

    def _on_commitment_changed(self, commitment: object) -> None:
        if isinstance(commitment, TemporalCommitment):
            self._delivery_in_flight.discard(commitment.commitment_id)
        if self._running:
            self.wake()

    def _on_data_operation_failed(self, operation: str, _category: str) -> None:
        if operation != "surface_reminders":
            return
        self._delivery_in_flight.clear()
        if self._running:
            self.wake()

    def _on_due_scanned(self, value: object) -> None:
        self._scan_pending = False
        if not self._running or not isinstance(value, TemporalDueSnapshot):
            return
        self._nearest_due_at = value.next_due_at_utc
        self._reschedule_nearest()
        deliverable = tuple(
            item for item in value.due if item.commitment_id not in self._delivery_in_flight
        )
        if not deliverable:
            return
        if len(deliverable) > 1:
            self._surface_aggregate(deliverable)
            return
        self._surface_one(deliverable[0])

    def _reschedule_nearest(self) -> None:
        self._nearest_timer.stop()
        due_at = self._nearest_due_at
        if due_at is None or not self._running:
            return
        now = self._clock().astimezone(UTC)
        milliseconds = max(0, round((due_at.astimezone(UTC) - now).total_seconds() * 1000))
        # Calibration still catches wall-clock jumps; capping this timer keeps
        # an exact deadline responsive without a long uninterruptible wait.
        self._nearest_timer.start(min(_CALIBRATION_INTERVAL_MS, milliseconds))

    def _surface_aggregate(self, commitments: tuple[TemporalCommitment, ...]) -> None:
        if self._is_locked():
            return
        ids = tuple(item.commitment_id for item in commitments)
        if not self._notify("Amadeus 提醒", f"有 {len(ids)} 个提醒或约定已到期", {"kind": "aggregate"}):
            self.status_changed.emit("notification_unavailable")
            return
        self._delivery_in_flight.update(ids)
        if not self._data.mark_temporal_commitments_surfaced(
            ids,
            reason_code="aggregate_notification",
        ):
            self._delivery_in_flight.difference_update(ids)

    def _surface_one(self, commitment: TemporalCommitment) -> None:
        if commitment.current_version.kind is TemporalCommitmentKind.SCHEDULED_FOLLOWUP:
            self._surface_followup(commitment)
            return
        if self._is_locked():
            return
        version = commitment.current_version
        body = version.content if version.show_content else "有件事到时间了，点击查看"
        if not self._notify(
            "Amadeus 提醒",
            body,
            {"kind": "single", "commitment_id": commitment.commitment_id},
        ):
            self.status_changed.emit("notification_unavailable")
            return
        self._delivery_in_flight.add(commitment.commitment_id)
        if not self._data.mark_temporal_commitments_surfaced(
            (commitment.commitment_id,),
            reason_code="system_notification",
        ):
            self._delivery_in_flight.discard(commitment.commitment_id)

    def _surface_followup(self, commitment: TemporalCommitment) -> None:
        if self._is_locked() or not self._followup_safe():
            return
        if not self._show_followup(commitment):
            self.status_changed.emit("followup_display_unavailable")
            return
        self._delivery_in_flight.add(commitment.commitment_id)
        if not self._data.mark_temporal_commitments_surfaced(
            (commitment.commitment_id,),
            reason_code="followup_bubble",
        ):
            self._delivery_in_flight.discard(commitment.commitment_id)

    def _is_locked(self) -> bool:
        try:
            return bool(self._presence_probe.snapshot().session_locked)
        except Exception:  # pragma: no cover - defensive Windows probe boundary
            return True
