"""Bounded off-UI ASR/TTS execution with cancellation and stale-event filtering."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import RLock

from PySide6.QtCore import QObject, Signal

from amadeus_desktop.chat_provider import CancellationRequested, CancellationToken
from amadeus_desktop.speech import (
    SpeechError,
    SpeechSynthesizer,
    SpeechToken,
    SpeechTranscriber,
)


@dataclass(slots=True)
class _TtsTask:
    token: SpeechToken
    sequence: int
    text: str
    cancellation: CancellationToken


class SpeechNetworkRuntime(QObject):
    """Run one ASR and one serialized, capacity-bounded TTS lane."""

    transcribed = Signal(object, str)
    transcription_failed = Signal(object, str)
    synthesized = Signal(object, int, object)
    synthesis_failed = Signal(object, int, str)
    queue_changed = Signal(int)

    def __init__(
        self,
        transcriber: SpeechTranscriber,
        synthesizer: SpeechSynthesizer,
        *,
        tts_capacity: int = 3,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if tts_capacity <= 0:
            raise ValueError("TTS capacity must be positive")
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._tts_capacity = tts_capacity
        self._asr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-asr")
        self._tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-tts")
        self._lock = RLock()
        self._generation = 0
        self._closed = False
        self._asr_future: Future[str] | None = None
        self._asr_token: CancellationToken | None = None
        self._tts_future: Future[bytes] | None = None
        self._tts_active: _TtsTask | None = None
        self._tts_pending: deque[_TtsTask] = deque()

    @property
    def queued_tts(self) -> int:
        with self._lock:
            return len(self._tts_pending) + (1 if self._tts_active is not None else 0)

    @property
    def busy(self) -> bool:
        """Whether an ASR or TTS task still owns the current service clients."""

        with self._lock:
            return bool(
                self._asr_future is not None or self._tts_active is not None or self._tts_pending
            )

    def set_services(
        self,
        transcriber: SpeechTranscriber,
        synthesizer: SpeechSynthesizer,
    ) -> bool:
        with self._lock:
            if (
                self._closed
                or self._asr_future is not None
                or self._tts_active is not None
                or self._tts_pending
            ):
                return False
            self._transcriber = transcriber
            self._synthesizer = synthesizer
            return True

    def transcribe(self, token: SpeechToken, wav_bytes: bytes) -> bool:
        with self._lock:
            if self._closed or self._asr_future is not None:
                return False
            generation = self._generation
            cancellation = CancellationToken()
            future = self._asr_executor.submit(
                lambda: asyncio.run(self._transcriber.transcribe(wav_bytes, cancellation))
            )
            self._asr_future = future
            self._asr_token = cancellation
        future.add_done_callback(lambda completed: self._complete_asr(completed, generation, token))
        return True

    def enqueue_tts(self, token: SpeechToken, sequence: int, text: str) -> bool:
        task = _TtsTask(token, int(sequence), str(text), CancellationToken())
        with self._lock:
            if self._closed:
                return False
            current_count = len(self._tts_pending) + (1 if self._tts_active is not None else 0)
            if current_count >= self._tts_capacity:
                return False
            self._tts_pending.append(task)
            count = len(self._tts_pending) + (1 if self._tts_active is not None else 0)
            should_start = self._tts_active is None
        self.queue_changed.emit(count)
        if should_start:
            self._start_next_tts()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            self._generation += 1
            asr_token = self._asr_token
            asr_future = self._asr_future
            active = self._tts_active
            tts_future = self._tts_future
            pending = tuple(self._tts_pending)
            self._tts_pending.clear()
        if asr_token is not None:
            asr_token.cancel()
        if asr_future is not None:
            asr_future.cancel()
        if active is not None:
            active.cancellation.cancel()
        if tts_future is not None:
            tts_future.cancel()
        for task in pending:
            task.cancellation.cancel()
        self.queue_changed.emit(0)

    def shutdown(self, wait_ms: int = 5_000) -> bool:
        with self._lock:
            self._closed = True
            futures = tuple(
                future for future in (self._asr_future, self._tts_future) if future is not None
            )
        self.cancel_all()
        deadline = time.monotonic() + max(0, wait_ms) / 1_000
        pending = set(futures)
        while pending and time.monotonic() < deadline:
            timeout = max(0.0, min(0.05, deadline - time.monotonic()))
            _done, pending = wait(pending, timeout=timeout)
        self._asr_executor.shutdown(wait=False, cancel_futures=True)
        self._tts_executor.shutdown(wait=False, cancel_futures=True)
        return not pending

    def _complete_asr(
        self,
        future: Future[str],
        generation: int,
        token: SpeechToken,
    ) -> None:
        with self._lock:
            if self._asr_future is future:
                self._asr_future = None
                self._asr_token = None
            relevant = generation == self._generation and not self._closed
        if not relevant or future.cancelled():
            return
        try:
            transcript = future.result()
        except CancellationRequested:
            return
        except SpeechError as exc:
            self.transcription_failed.emit(token, exc.safe_message)
        except Exception:
            self.transcription_failed.emit(token, "语音转写失败，请重试。")
        else:
            self.transcribed.emit(token, transcript)

    def _start_next_tts(self) -> None:
        with self._lock:
            if self._closed or self._tts_active is not None or not self._tts_pending:
                return
            generation = self._generation
            task = self._tts_pending.popleft()
            future = self._tts_executor.submit(
                lambda: asyncio.run(self._synthesizer.synthesize(task.text, task.cancellation))
            )
            self._tts_active = task
            self._tts_future = future
        future.add_done_callback(lambda completed: self._complete_tts(completed, generation, task))

    def _complete_tts(
        self,
        future: Future[bytes],
        generation: int,
        task: _TtsTask,
    ) -> None:
        with self._lock:
            if self._tts_future is future:
                self._tts_future = None
                self._tts_active = None
            relevant = generation == self._generation and not self._closed
            count = len(self._tts_pending)
        if relevant and not future.cancelled():
            try:
                audio = future.result()
            except CancellationRequested:
                pass
            except SpeechError as exc:
                self.synthesis_failed.emit(task.token, task.sequence, exc.safe_message)
            except Exception:
                self.synthesis_failed.emit(task.token, task.sequence, "语音合成失败。")
            else:
                self.synthesized.emit(task.token, task.sequence, audio)
        self.queue_changed.emit(count)
        self._start_next_tts()
