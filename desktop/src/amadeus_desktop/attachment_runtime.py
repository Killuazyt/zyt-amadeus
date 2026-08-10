"""Bounded off-UI attachment preprocessing with cooperative cancellation."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import RLock

from PySide6.QtCore import QObject, Signal

from amadeus_desktop.attachments import (
    AttachmentBatch,
    AttachmentCancellation,
    AttachmentError,
    AttachmentStore,
)
from amadeus_desktop.chat_models import AttachmentSnapshot, AttachmentSource


class AttachmentImportRuntime(QObject):
    """Serialize attachment work and return only immutable ready snapshots."""

    imported = Signal(object)
    failed = Signal(str)
    context_imported = Signal(object, object)
    context_failed = Signal(object, str)
    busy_changed = Signal(bool)

    def __init__(self, store: AttachmentStore, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachment")
        self._lock = RLock()
        self._generation = 0
        self._futures: set[Future[tuple[AttachmentSnapshot, ...]]] = set()
        self._tokens: dict[Future[tuple[AttachmentSnapshot, ...]], AttachmentCancellation] = {}
        self._contexts: dict[Future[tuple[AttachmentSnapshot, ...]], object | None] = {}
        self._closed = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._futures)

    def import_paths(
        self,
        paths: tuple[str | Path, ...],
        *,
        source: AttachmentSource,
        existing: tuple[AttachmentSnapshot, ...] = (),
        context: object | None = None,
    ) -> bool:
        try:
            AttachmentBatch(existing)
        except AttachmentError as exc:
            self._emit_failure(context, exc.safe_message)
            return False
        if not paths:
            return False
        if len(existing) + len(paths) > 5:
            self._emit_failure(context, "每条消息最多添加 5 个附件。")
            return False

        def operation(token: AttachmentCancellation) -> tuple[AttachmentSnapshot, ...]:
            imported: list[AttachmentSnapshot] = []
            for path in paths:
                imported.append(
                    self._store.import_path(
                        Path(path),
                        source=source,
                        cancellation=token,
                    )
                )
                AttachmentBatch((*existing, *imported))
            return tuple(imported)

        return self._submit(operation, context=context)

    def import_bytes(
        self,
        payload: bytes,
        *,
        display_name: str,
        source: AttachmentSource,
        existing: tuple[AttachmentSnapshot, ...] = (),
        context: object | None = None,
    ) -> bool:
        if len(existing) >= 5:
            self._emit_failure(context, "每条消息最多添加 5 个附件。")
            return False

        def operation(token: AttachmentCancellation) -> tuple[AttachmentSnapshot, ...]:
            attachment = self._store.import_bytes(
                payload,
                display_name=display_name,
                source=source,
                cancellation=token,
            )
            AttachmentBatch((*existing, attachment))
            return (attachment,)

        return self._submit(operation, context=context)

    def cancel_all(self) -> None:
        with self._lock:
            self._generation += 1
            tokens = tuple(self._tokens.values())
            futures = tuple(self._futures)
        for token in tokens:
            token.cancel()
        for future in futures:
            future.cancel()

    def shutdown(self, wait_ms: int = 5_000) -> bool:
        with self._lock:
            self._closed = True
            futures = tuple(self._futures)
        self.cancel_all()
        deadline = time.monotonic() + max(0, wait_ms) / 1_000
        pending = set(futures)
        while pending and time.monotonic() < deadline:
            _done, pending = wait(pending, timeout=min(0.05, deadline - time.monotonic()))
        self._executor.shutdown(wait=False, cancel_futures=True)
        return not pending

    def _submit(self, operation, *, context: object | None = None) -> bool:
        with self._lock:
            if self._closed:
                return False
            generation = self._generation
            token = AttachmentCancellation()

            future = self._executor.submit(operation, token)
            was_busy = bool(self._futures)
            self._futures.add(future)
            self._tokens[future] = token
            self._contexts[future] = context
        if not was_busy:
            self.busy_changed.emit(True)
        future.add_done_callback(lambda completed: self._completed(completed, generation))
        return True

    def _completed(
        self,
        future: Future[tuple[AttachmentSnapshot, ...]],
        generation: int,
    ) -> None:
        with self._lock:
            self._futures.discard(future)
            self._tokens.pop(future, None)
            context = self._contexts.pop(future, None)
            relevant = generation == self._generation and not self._closed
            now_idle = not self._futures
        if relevant:
            try:
                attachments = future.result()
            except AttachmentError as exc:
                self._emit_failure(context, exc.safe_message)
            except Exception:
                self._emit_failure(context, "附件处理失败，请重试。")
            else:
                if context is None:
                    for attachment in attachments:
                        self.imported.emit(attachment)
                else:
                    self.context_imported.emit(context, attachments)
        if now_idle:
            self.busy_changed.emit(False)

    def _emit_failure(self, context: object | None, message: str) -> None:
        if context is None:
            self.failed.emit(message)
        else:
            self.context_failed.emit(context, message)
