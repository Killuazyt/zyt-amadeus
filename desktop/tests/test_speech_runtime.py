from __future__ import annotations

import threading

from amadeus_desktop.chat_provider import CancellationToken
from amadeus_desktop.speech import SpeechToken
from amadeus_desktop.speech_runtime import SpeechNetworkRuntime


class _Transcriber:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[bytes] = []

    async def transcribe(self, wav_bytes: bytes, cancellation: CancellationToken) -> str:
        self.calls.append(wav_bytes)
        if self.gate is not None:
            self.gate.wait(timeout=2)
        cancellation.raise_if_cancelled()
        return "转写完成"


class _Synthesizer:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[str] = []

    async def synthesize(self, text: str, cancellation: CancellationToken) -> bytes:
        self.calls.append(text)
        if self.gate is not None and len(self.calls) == 1:
            self.gate.wait(timeout=2)
        cancellation.raise_if_cancelled()
        return f"wav:{text}".encode()


def test_tts_lane_is_serial_bounded_and_never_drops_an_accepted_sentence(qtbot) -> None:
    gate = threading.Event()
    synthesizer = _Synthesizer(gate)
    runtime = SpeechNetworkRuntime(_Transcriber(), synthesizer, tts_capacity=2)
    token = SpeechToken(1, 1, 1)
    completed: list[tuple[int, bytes]] = []
    runtime.synthesized.connect(
        lambda _token, sequence, wav: completed.append((sequence, bytes(wav)))
    )

    assert runtime.enqueue_tts(token, 1, "第一句。")
    assert runtime.enqueue_tts(token, 2, "第二句。")
    assert not runtime.enqueue_tts(token, 3, "第三句。")
    assert runtime.queued_tts == 2
    gate.set()
    qtbot.waitUntil(lambda: len(completed) == 2)

    assert synthesizer.calls == ["第一句。", "第二句。"]
    assert completed == [(1, "wav:第一句。".encode()), (2, "wav:第二句。".encode())]
    assert runtime.shutdown(2_000)


def test_cancelled_asr_result_is_filtered_by_runtime_generation(qtbot) -> None:
    gate = threading.Event()
    transcriber = _Transcriber(gate)
    runtime = SpeechNetworkRuntime(transcriber, _Synthesizer())
    token = SpeechToken(2, 3, 4)
    completed: list[str] = []
    failed: list[str] = []
    runtime.transcribed.connect(lambda _token, text: completed.append(text))
    runtime.transcription_failed.connect(lambda _token, message: failed.append(message))

    assert runtime.transcribe(token, b"RIFFsynthetic")
    qtbot.waitUntil(lambda: bool(transcriber.calls))
    runtime.cancel_all()
    gate.set()
    qtbot.wait(100)

    assert completed == []
    assert failed == []
    assert runtime.shutdown(2_000)


def test_asr_and_tts_lanes_fail_independently(qtbot) -> None:
    class BrokenTranscriber:
        async def transcribe(self, _wav: bytes, _cancellation: CancellationToken) -> str:
            raise RuntimeError("private failure")

    runtime = SpeechNetworkRuntime(BrokenTranscriber(), _Synthesizer())
    token = SpeechToken(1, 1, 1)
    failures: list[str] = []
    synthesized: list[bytes] = []
    runtime.transcription_failed.connect(lambda _token, message: failures.append(message))
    runtime.synthesized.connect(lambda _token, _sequence, audio: synthesized.append(bytes(audio)))

    assert runtime.transcribe(token, b"RIFFsynthetic")
    assert runtime.enqueue_tts(token, 1, "仍然播音。")
    qtbot.waitUntil(lambda: bool(failures) and bool(synthesized))

    assert failures == ["语音转写失败，请重试。"]
    assert synthesized == ["wav:仍然播音。".encode()]
    assert runtime.shutdown(2_000)
