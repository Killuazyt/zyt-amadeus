"""Single interruptible voice state machine layered on the existing text chat."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from amadeus_desktop.audio_runtime import MicrophoneCapture, WavPlaybackQueue
from amadeus_desktop.chat_models import ConversationState
from amadeus_desktop.speech import (
    MAX_UTTERANCE_BYTES,
    SentenceSegmenter,
    SpeechEpoch,
    SpeechToken,
    VadEvent,
    VadUtteranceBuffer,
    VoiceState,
    pcm16_to_wav,
)
from amadeus_desktop.speech_runtime import SpeechNetworkRuntime

_MIN_UTTERANCE_BYTES = 16_000 * 2 // 5  # 200 ms


class VoiceSessionController(QObject):
    """Coordinate PTT/hands-free capture, ASR, existing chat, TTS, and playback."""

    state_changed = Signal(object)
    status_changed = Signal(str, bool)
    transcript_ready = Signal(str, object)
    stop_chat_requested = Signal()
    hands_free_changed = Signal(bool)

    def __init__(
        self,
        capture: MicrophoneCapture,
        network: SpeechNetworkRuntime,
        playback: WavPlaybackQueue,
        *,
        input_device_id: str = "",
        output_device_id: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._capture = capture
        self._network = network
        self._playback = playback
        self._input_device_id = input_device_id
        self._output_device_id = output_device_id
        self._epoch = SpeechEpoch()
        self._state = VoiceState.OFF
        self._hands_free = False
        self._ptt = False
        self._ptt_pcm = bytearray()
        self._vad = VadUtteranceBuffer()
        self._active_token: SpeechToken | None = None
        self._chat_turn_id: str | None = None
        self._segmenter = SentenceSegmenter()
        self._tts_sequence = 0
        self._tts_network_count = 0
        self._chat_complete = False
        self._closed = False

        capture.frame_ready.connect(self._on_frame)
        capture.failed.connect(self._on_capture_failed)
        network.transcribed.connect(self._on_transcribed)
        network.transcription_failed.connect(self._on_transcription_failed)
        network.synthesized.connect(self._on_synthesized)
        network.synthesis_failed.connect(self._on_synthesis_failed)
        network.queue_changed.connect(self._on_network_queue_changed)
        playback.item_started.connect(self._on_playback_started)
        playback.queue_empty.connect(self._on_playback_empty)
        playback.failed.connect(self._on_playback_failed)

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def hands_free(self) -> bool:
        return self._hands_free

    @property
    def active_token(self) -> SpeechToken | None:
        return self._active_token

    def set_devices(self, input_device_id: str, output_device_id: str) -> None:
        self._input_device_id = str(input_device_id)
        self._output_device_id = str(output_device_id)

    def start_hands_free(self) -> bool:
        if self._closed or self._hands_free:
            return False
        self._interrupt_current(emit_chat_stop=True)
        self._epoch.start_session()
        self._hands_free = True
        self._ptt = False
        self._vad.reset()
        self._set_state(VoiceState.LISTENING)
        self.hands_free_changed.emit(True)
        if not self._capture.start(self._input_device_id):
            self.stop_session(stop_chat=False)
            return False
        self.status_changed.emit("免提会话已开启，正在校准环境噪声。", False)
        return True

    def stop_session(self, *, stop_chat: bool = True) -> None:
        if self._state is VoiceState.OFF and not self._hands_free and not self._ptt:
            return
        if stop_chat and self._chat_turn_id is not None:
            self.stop_chat_requested.emit()
        self._epoch.stop_session()
        self._interrupt_current(emit_chat_stop=False)
        self._hands_free = False
        self._ptt = False
        self._capture.stop()
        self._vad.reset()
        self._set_state(VoiceState.OFF)
        self.hands_free_changed.emit(False)
        self.status_changed.emit("语音会话已停止。", False)

    def press_to_talk(self) -> bool:
        if self._closed or self._ptt:
            return False
        was_hands_free = self._hands_free
        if self._state is VoiceState.OFF:
            self._epoch.start_session()
        self._interrupt_current(emit_chat_stop=True)
        self._hands_free = was_hands_free
        self._ptt = True
        self._ptt_pcm.clear()
        self._active_token = self._epoch.begin_turn()
        self._capture.stop()
        self._set_state(VoiceState.CAPTURING)
        if not self._capture.start(self._input_device_id):
            self._ptt = False
            self._resume_idle()
            return False
        self.status_changed.emit("正在录音，松开后发送转写。", False)
        return True

    def release_to_send(self) -> bool:
        if not self._ptt:
            return False
        self._ptt = False
        self._capture.stop()
        pcm = bytes(self._ptt_pcm)
        self._ptt_pcm.clear()
        return self._submit_pcm(pcm)

    def bind_chat_turn(self, token: SpeechToken, turn_id: str) -> bool:
        if not self._accepts(token) or self._state is not VoiceState.THINKING:
            return False
        self._chat_turn_id = str(turn_id)
        return True

    def submission_failed(self, token: SpeechToken) -> None:
        if self._accepts(token):
            self.status_changed.emit("语音转写未能进入当前会话。", True)
            self._resume_idle()

    def on_chat_chunk(self, turn_id: str, chunk: str) -> None:
        if self._chat_turn_id != str(turn_id) or self._active_token is None:
            return
        for sentence in self._segmenter.feed(chunk):
            self._enqueue_sentence(sentence)

    def on_chat_finished(self, turn_id: str, state: ConversationState) -> None:
        if self._chat_turn_id != str(turn_id) or self._active_token is None:
            return
        self._chat_complete = True
        if state is ConversationState.COMPLETED:
            for sentence in self._segmenter.flush():
                self._enqueue_sentence(sentence)
        else:
            self._segmenter.clear()
        if state in {ConversationState.FAILED, ConversationState.STOPPED}:
            self._network.cancel_all()
            self._playback.stop()
        self._maybe_finish_response()

    def shutdown(self, wait_ms: int = 5_000) -> bool:
        self._closed = True
        self.stop_session()
        network_clean = self._network.shutdown(wait_ms)
        self._playback.stop()
        return network_clean

    def _on_frame(self, frame_object: object) -> None:
        if not isinstance(frame_object, bytes):
            return
        if self._ptt:
            remaining = MAX_UTTERANCE_BYTES - len(self._ptt_pcm)
            if remaining > 0:
                self._ptt_pcm.extend(frame_object[:remaining])
            if len(self._ptt_pcm) >= MAX_UTTERANCE_BYTES:
                self.status_changed.emit("单句已达到 60 秒，正在转写。", False)
                self.release_to_send()
            return
        if not self._hands_free or self._state not in {
            VoiceState.LISTENING,
            VoiceState.CAPTURING,
            VoiceState.THINKING,
            VoiceState.SPEAKING,
        }:
            return
        event, pcm = self._vad.process(frame_object)
        if event is VadEvent.SPEECH_START:
            if self._state in {VoiceState.THINKING, VoiceState.SPEAKING}:
                self._interrupt_current(emit_chat_stop=True)
            self._active_token = self._epoch.begin_turn()
            self._set_state(VoiceState.CAPTURING)
            self.status_changed.emit("检测到说话，正在收音。", False)
        elif event in {VadEvent.SPEECH_END, VadEvent.MAX_DURATION} and pcm is not None:
            self._capture.stop()
            self._submit_pcm(pcm)

    def _submit_pcm(self, pcm: bytes) -> bool:
        token = self._active_token
        if token is None or not self._accepts(token):
            self._resume_idle()
            return False
        if len(pcm) < _MIN_UTTERANCE_BYTES:
            self.status_changed.emit("没有检测到足够的语音，请重试。", True)
            self._resume_idle()
            return False
        try:
            wav_bytes = pcm16_to_wav(pcm)
        except ValueError:
            self.status_changed.emit("录音数据无效，请重试。", True)
            self._resume_idle()
            return False
        self._set_state(VoiceState.TRANSCRIBING)
        self.status_changed.emit("正在转写这一句…", False)
        if not self._network.transcribe(token, wav_bytes):
            self.status_changed.emit("已有语音转写正在进行，请稍候。", True)
            self._resume_idle()
            return False
        # Drop the only controller-owned reference immediately after the worker
        # captured its immutable request bytes.
        del wav_bytes
        return True

    def _on_transcribed(self, token_object: object, transcript: str) -> None:
        if not isinstance(token_object, SpeechToken) or not self._accepts(token_object):
            return
        self._chat_turn_id = None
        self._chat_complete = False
        self._segmenter.clear()
        self._tts_sequence = 0
        self._set_state(VoiceState.THINKING)
        self.status_changed.emit("转写完成，正在生成回复。", False)
        self.transcript_ready.emit(transcript, token_object)
        if self._hands_free and self._accepts(token_object) and not self._capture.active:
            self._capture.start(self._input_device_id)

    def _on_transcription_failed(self, token_object: object, message: str) -> None:
        if not isinstance(token_object, SpeechToken) or not self._accepts(token_object):
            return
        self.status_changed.emit(message, True)
        self._resume_idle()

    def _enqueue_sentence(self, sentence: str) -> None:
        token = self._active_token
        if token is None or not self._accepts(token):
            return
        self._tts_sequence += 1
        if not self._network.enqueue_tts(token, self._tts_sequence, sentence):
            self.status_changed.emit("TTS 队列已满，文字回复仍会完整保留。", True)

    def _on_synthesized(
        self,
        token_object: object,
        sequence: int,
        audio_object: object,
    ) -> None:
        if (
            not isinstance(token_object, SpeechToken)
            or not self._accepts(token_object)
            or not isinstance(audio_object, bytes)
        ):
            return
        if self._playback.queued == 0 and not self._playback.set_device(self._output_device_id):
            return
        if not self._playback.enqueue(token_object, sequence, audio_object):
            self.status_changed.emit("播音队列已满，已保留文字回复。", True)

    def _on_synthesis_failed(
        self,
        token_object: object,
        _sequence: int,
        message: str,
    ) -> None:
        if isinstance(token_object, SpeechToken) and self._accepts(token_object):
            self.status_changed.emit(f"{message} 文字聊天不受影响。", True)
            self._maybe_finish_response()

    def _on_network_queue_changed(self, count: int) -> None:
        self._tts_network_count = max(0, int(count))
        self._maybe_finish_response()

    def _on_playback_started(self, token_object: object, _sequence: int) -> None:
        if isinstance(token_object, SpeechToken) and self._accepts(token_object):
            self._set_state(VoiceState.SPEAKING)
            self.status_changed.emit("正在播放回复；按住说话可立即打断。", False)

    def _on_playback_empty(self, token_object: object) -> None:
        if isinstance(token_object, SpeechToken) and self._accepts(token_object):
            if not self._chat_complete:
                self._set_state(VoiceState.THINKING)
            self._maybe_finish_response()

    def _on_capture_failed(self, message: str) -> None:
        self.status_changed.emit(message, True)
        if self._hands_free:
            self.stop_session()
        else:
            self._ptt = False
            self._resume_idle()

    def _on_playback_failed(self, message: str) -> None:
        self.status_changed.emit(f"{message} 文字聊天不受影响。", True)
        self._maybe_finish_response(force=True)

    def _interrupt_current(self, *, emit_chat_stop: bool) -> None:
        if emit_chat_stop and (
            self._chat_turn_id is not None
            or self._state in {VoiceState.THINKING, VoiceState.SPEAKING}
        ):
            self.stop_chat_requested.emit()
        self._epoch.interrupt_speech()
        self._network.cancel_all()
        self._playback.stop()
        self._segmenter.clear()
        self._chat_turn_id = None
        self._chat_complete = False
        self._active_token = None
        self._tts_network_count = 0
        self._tts_sequence = 0
        self._ptt_pcm.clear()

    def _maybe_finish_response(self, *, force: bool = False) -> None:
        if not force and (
            not self._chat_complete or self._tts_network_count > 0 or self._playback.queued > 0
        ):
            return
        self._chat_turn_id = None
        self._active_token = None
        self._segmenter.clear()
        self._resume_idle()

    def _resume_idle(self) -> None:
        self._ptt_pcm.clear()
        self._ptt = False
        if self._hands_free and not self._closed:
            self._vad.reset(keep_calibration=True)
            self._set_state(VoiceState.LISTENING)
            if not self._capture.active:
                self._capture.start(self._input_device_id)
            self.status_changed.emit("免提会话正在聆听。", False)
        else:
            self._capture.stop()
            self._set_state(VoiceState.OFF)

    def _accepts(self, token: SpeechToken) -> bool:
        return token == self._active_token and self._epoch.accepts(token)

    def _set_state(self, state: VoiceState) -> None:
        normalized = VoiceState(state)
        if normalized is self._state:
            return
        self._state = normalized
        self.state_changed.emit(normalized)
