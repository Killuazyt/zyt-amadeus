from __future__ import annotations

import struct

from PySide6.QtCore import QObject, Signal

from amadeus_desktop.chat_models import ConversationState
from amadeus_desktop.speech import VAD_FRAME_BYTES, SpeechToken, VoiceState, pcm16_to_wav
from amadeus_desktop.voice_session import VoiceSessionController


class _Capture(QObject):
    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.starts = 0
        self.stops = 0

    def start(self, _device_id: str = "") -> bool:
        self.active = True
        self.starts += 1
        return True

    def stop(self) -> None:
        if self.active:
            self.stops += 1
        self.active = False


class _Network(QObject):
    transcribed = Signal(object, str)
    transcription_failed = Signal(object, str)
    synthesized = Signal(object, int, object)
    synthesis_failed = Signal(object, int, str)
    queue_changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.asr: list[tuple[SpeechToken, bytes]] = []
        self.tts: list[tuple[SpeechToken, int, str]] = []
        self.cancel_count = 0

    def transcribe(self, token: SpeechToken, wav: bytes) -> bool:
        self.asr.append((token, wav))
        return True

    def enqueue_tts(self, token: SpeechToken, sequence: int, text: str) -> bool:
        self.tts.append((token, sequence, text))
        self.queue_changed.emit(1)
        return True

    def cancel_all(self) -> None:
        self.cancel_count += 1
        self.queue_changed.emit(0)

    def shutdown(self, _wait_ms: int) -> bool:
        return True


class _Playback(QObject):
    item_started = Signal(object, int)
    item_finished = Signal(object, int)
    queue_empty = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.queued = 0
        self.items: list[tuple[SpeechToken, int, bytes]] = []
        self.stop_count = 0

    def set_device(self, _device_id: str = "") -> bool:
        return True

    def enqueue(self, token: SpeechToken, sequence: int, wav: bytes) -> bool:
        self.items.append((token, sequence, wav))
        self.queued = 1
        self.active = True
        self.item_started.emit(token, sequence)
        return True

    def finish(self) -> None:
        token, sequence, _wav = self.items[-1]
        self.active = False
        self.queued = 0
        self.item_finished.emit(token, sequence)
        self.queue_empty.emit(token)

    def stop(self) -> None:
        self.stop_count += 1
        self.active = False
        self.queued = 0


def _frame(value: int = 500) -> bytes:
    return struct.pack(f"<{VAD_FRAME_BYTES // 2}h", *([value] * (VAD_FRAME_BYTES // 2)))


def _session():
    capture = _Capture()
    network = _Network()
    playback = _Playback()
    session = VoiceSessionController(capture, network, playback)  # type: ignore[arg-type]
    return session, capture, network, playback


def test_ptt_asr_existing_chat_sentence_tts_and_playback(qtbot) -> None:
    session, capture, network, playback = _session()
    transcripts: list[tuple[str, SpeechToken]] = []
    session.transcript_ready.connect(lambda text, token: transcripts.append((text, token)))

    assert session.press_to_talk()
    assert session.state is VoiceState.CAPTURING
    for _ in range(10):
        capture.frame_ready.emit(_frame())
    assert session.release_to_send()
    assert session.state is VoiceState.TRANSCRIBING
    token, wav = network.asr[-1]
    assert wav.startswith(b"RIFF")

    network.transcribed.emit(token, "这是转写")
    assert transcripts == [("这是转写", token)]
    assert session.state is VoiceState.THINKING
    assert session.bind_chat_turn(token, "turn-1")
    session.on_chat_chunk("turn-1", "你好。")
    assert network.tts[-1][2] == "你好。"
    session.on_chat_finished("turn-1", ConversationState.COMPLETED)

    network.synthesized.emit(token, 1, pcm16_to_wav(_frame() * 2))
    network.queue_changed.emit(0)
    assert session.state is VoiceState.SPEAKING
    playback.finish()
    assert session.state is VoiceState.OFF


def test_ptt_barges_in_and_invalidates_late_speech(qtbot) -> None:
    session, capture, network, playback = _session()
    stop_requests: list[bool] = []
    session.stop_chat_requested.connect(lambda: stop_requests.append(True))

    session.press_to_talk()
    for _ in range(10):
        capture.frame_ready.emit(_frame())
    session.release_to_send()
    old_token, _wav = network.asr[-1]
    network.transcribed.emit(old_token, "第一句")
    assert session.bind_chat_turn(old_token, "turn-old")
    session.on_chat_chunk("turn-old", "旧回复。")
    network.synthesized.emit(old_token, 1, pcm16_to_wav(_frame() * 2))
    assert session.state is VoiceState.SPEAKING

    assert session.press_to_talk()
    assert stop_requests == [True]
    assert playback.stop_count > 0
    assert session.state is VoiceState.CAPTURING
    assert session.active_token != old_token

    network.transcribed.emit(old_token, "迟到转写")
    assert session.state is VoiceState.CAPTURING


def test_hands_free_vad_segments_one_sentence_and_stop_clears_capture(qtbot) -> None:
    session, capture, network, _playback = _session()
    assert session.start_hands_free()
    assert session.state is VoiceState.LISTENING

    for _ in range(30):
        capture.frame_ready.emit(_frame(10))
    for _ in range(4):
        capture.frame_ready.emit(_frame(2_000))
    assert session.state is VoiceState.CAPTURING
    for _ in range(35):
        capture.frame_ready.emit(_frame(0))

    assert session.state is VoiceState.TRANSCRIBING
    assert len(network.asr) == 1
    session.stop_session()
    assert session.state is VoiceState.OFF
    assert not capture.active


def test_hands_free_restarts_capture_and_barges_in_while_model_is_thinking(qtbot) -> None:
    session, capture, network, _playback = _session()
    stop_requests: list[bool] = []
    session.stop_chat_requested.connect(lambda: stop_requests.append(True))

    assert session.start_hands_free()
    for _ in range(30):
        capture.frame_ready.emit(_frame(10))
    for _ in range(4):
        capture.frame_ready.emit(_frame(2_000))
    for _ in range(35):
        capture.frame_ready.emit(_frame(0))
    old_token, _wav = network.asr[-1]
    assert not capture.active

    network.transcribed.emit(old_token, "第一句")

    assert session.state is VoiceState.THINKING
    assert capture.active
    for _ in range(3):
        capture.frame_ready.emit(_frame(2_000))

    assert stop_requests == [True]
    assert session.state is VoiceState.CAPTURING
    assert session.active_token != old_token


def test_microphone_disconnect_and_network_timeout_leave_voice_off(qtbot) -> None:
    session, capture, network, _playback = _session()
    statuses: list[tuple[str, bool]] = []
    session.status_changed.connect(lambda message, error: statuses.append((message, error)))

    assert session.press_to_talk()
    capture.failed.emit("麦克风已断开。")
    assert session.state is VoiceState.OFF
    assert not capture.active

    assert session.press_to_talk()
    for _ in range(10):
        capture.frame_ready.emit(_frame())
    assert session.release_to_send()
    token, _wav = network.asr[-1]
    network.transcription_failed.emit(token, "语音服务响应超时。")

    assert session.state is VoiceState.OFF
    assert ("语音服务响应超时。", True) in statuses


def test_late_tts_after_barge_in_never_reaches_playback(qtbot) -> None:
    session, capture, network, playback = _session()
    assert session.press_to_talk()
    for _ in range(10):
        capture.frame_ready.emit(_frame())
    assert session.release_to_send()
    old_token, _wav = network.asr[-1]
    network.transcribed.emit(old_token, "第一轮")
    assert session.bind_chat_turn(old_token, "turn-old")
    session.on_chat_chunk("turn-old", "旧回复。")

    assert session.press_to_talk()
    item_count = len(playback.items)
    network.synthesized.emit(old_token, 1, pcm16_to_wav(_frame() * 2))

    assert len(playback.items) == item_count
    assert session.state is VoiceState.CAPTURING


def test_tts_failure_preserves_completed_text_and_finishes_session(qtbot) -> None:
    session, capture, network, playback = _session()
    statuses: list[tuple[str, bool]] = []
    session.status_changed.connect(lambda message, error: statuses.append((message, error)))
    session.press_to_talk()
    for _ in range(10):
        capture.frame_ready.emit(_frame())
    session.release_to_send()
    token, _wav = network.asr[-1]
    network.transcribed.emit(token, "问题")
    assert session.bind_chat_turn(token, "turn-1")
    session.on_chat_chunk("turn-1", "文字回答。")
    session.on_chat_finished("turn-1", ConversationState.COMPLETED)

    network.synthesis_failed.emit(token, 1, "语音合成失败。")
    network.queue_changed.emit(0)

    assert session.state is VoiceState.OFF
    assert not playback.items
    assert any("文字聊天不受影响" in message for message, _error in statuses)
