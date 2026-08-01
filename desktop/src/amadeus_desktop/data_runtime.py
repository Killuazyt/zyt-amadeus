"""One serialized background data thread and injectable job boundaries for P5A."""

from __future__ import annotations

import contextlib
import itertools
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot


class DataPriority(IntEnum):
    """Lower values run first; foreground chat always outranks maintenance."""

    FOREGROUND = 0
    INTERACTIVE = 10
    BACKGROUND = 20


@runtime_checkable
class BackgroundJobQueue(Protocol):
    """Minimal injectable boundary used by summary and extraction schedulers."""

    def enqueue(self, kind: str, dedupe_key: str, **metadata: object) -> object:
        """Persist one idempotent background job."""


T = TypeVar("T")
DataOperation = Callable[[Any], T]
SuccessCallback = Callable[[object], None]
FailureCallback = Callable[[str], None]


@dataclass(slots=True)
class _Command:
    request_id: str
    operation: DataOperation[object]


class _DataSignalBridge(QObject):
    initialized = Signal(bool, str)
    succeeded = Signal(str, object)
    failed = Signal(str, str)
    stopped = Signal()


class SerialDataThread(QObject):
    """Run every SQLite operation against one owner object on one thread.

    ``resource_factory`` and ``resource_close`` are executed on the worker
    thread.  Callers submit small operations and receive callbacks on the Qt UI
    thread.  Exception text is intentionally never crossed into UI or logs.
    """

    ready_changed = Signal(bool)
    initialization_failed = Signal(str)

    def __init__(
        self,
        resource_factory: Callable[[], object],
        *,
        resource_close: Callable[[object], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._resource_factory = resource_factory
        self._resource_close = resource_close
        self._bridge = _DataSignalBridge(self)
        self._bridge.initialized.connect(self._on_initialized)
        self._bridge.succeeded.connect(self._on_succeeded)
        self._bridge.failed.connect(self._on_failed)
        self._bridge.stopped.connect(self._on_stopped)
        self._commands: queue.PriorityQueue[tuple[int, int, _Command | None]] = (
            queue.PriorityQueue()
        )
        self._counter = itertools.count()
        self._callbacks: dict[str, tuple[SuccessCallback | None, FailureCallback | None]] = {}
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._ready = False
        self._initialization_error: str | None = None
        self._lock = threading.Lock()

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def initialization_error(self) -> str | None:
        return self._initialization_error

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._accepting = True
            self._thread = threading.Thread(
                target=self._run,
                name="amadeus-data",
                daemon=True,
            )
            self._thread.start()

    def submit(
        self,
        operation: DataOperation[T],
        *,
        priority: DataPriority = DataPriority.INTERACTIVE,
        on_success: Callable[[T], None] | None = None,
        on_failure: FailureCallback | None = None,
    ) -> str | None:
        """Queue an operation without ever executing it on the caller thread."""

        with self._lock:
            if not self._accepting or self._thread is None or not self._thread.is_alive():
                return None
            request_id = uuid4().hex
            self._callbacks[request_id] = (
                on_success,  # type: ignore[dict-item]
                on_failure,
            )
            command = _Command(request_id, operation)  # type: ignore[arg-type]
            self._commands.put((int(priority), next(self._counter), command))
            return request_id

    def shutdown(self, wait_ms: int = 5_000) -> bool:
        """Stop accepting work, drain queued writes, close SQLite, and join."""

        if wait_ms < 0:
            raise ValueError("wait_ms must be non-negative")
        with self._lock:
            self._accepting = False
            thread = self._thread
            if thread is None:
                return True
            self._commands.put((100, next(self._counter), None))
        thread.join(wait_ms / 1000)
        return not thread.is_alive()

    def _run(self) -> None:
        resource: object | None = None
        try:
            resource = self._resource_factory()
        except Exception as exc:  # noqa: BLE001 - only the safe type crosses threads
            with self._lock:
                self._accepting = False
            self._bridge.initialized.emit(False, type(exc).__name__)
            self._fail_all(type(exc).__name__)
            self._bridge.stopped.emit()
            return

        self._bridge.initialized.emit(True, "")
        try:
            while True:
                _priority, _sequence, command = self._commands.get()
                if command is None:
                    break
                try:
                    value = command.operation(resource)
                except Exception as exc:  # noqa: BLE001 - redact operation inputs/details
                    self._bridge.failed.emit(command.request_id, type(exc).__name__)
                else:
                    self._bridge.succeeded.emit(command.request_id, value)
        finally:
            with self._lock:
                self._accepting = False
            if resource is not None and self._resource_close is not None:
                with contextlib.suppress(Exception):
                    self._resource_close(resource)
            self._bridge.stopped.emit()

    def _fail_all(self, category: str) -> None:
        while True:
            try:
                _priority, _sequence, command = self._commands.get_nowait()
            except queue.Empty:
                break
            if command is not None:
                self._bridge.failed.emit(command.request_id, category)

    @Slot(bool, str)
    def _on_initialized(self, ready: bool, category: str) -> None:
        self._ready = ready
        self._initialization_error = None if ready else category
        self.ready_changed.emit(ready)
        if not ready:
            self.initialization_failed.emit(category)

    @Slot(str, object)
    def _on_succeeded(self, request_id: str, value: object) -> None:
        callback, _failure = self._callbacks.pop(request_id, (None, None))
        if callback is not None:
            callback(value)

    @Slot(str, str)
    def _on_failed(self, request_id: str, category: str) -> None:
        _success, callback = self._callbacks.pop(request_id, (None, None))
        if callback is not None:
            callback(category)

    @Slot()
    def _on_stopped(self) -> None:
        self._ready = False
        self.ready_changed.emit(False)
