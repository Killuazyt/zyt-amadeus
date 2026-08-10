from __future__ import annotations

import asyncio
import base64
import json
import struct

import httpx
import pytest

from amadeus_desktop.chat_provider import CancellationRequested, CancellationToken
from amadeus_desktop.credential_store import InMemoryCredentialStore
from amadeus_desktop.speech import (
    VAD_FRAME_BYTES,
    AdaptiveEnergyVad,
    MiMoSpeechClient,
    MiMoSpeechConfig,
    SentenceSegmenter,
    SpeechEpoch,
    VadEvent,
    VadUtteranceBuffer,
    pcm16_to_wav,
)


def _frame(sample: int) -> bytes:
    return struct.pack(f"<{VAD_FRAME_BYTES // 2}h", *([sample] * (VAD_FRAME_BYTES // 2)))


def test_adaptive_vad_calibrates_starts_and_ends_after_700_ms() -> None:
    vad = AdaptiveEnergyVad(calibration_ms=600, end_silence_ms=700)
    buffer = VadUtteranceBuffer(vad)

    for _ in range(30):
        assert buffer.process(_frame(20))[0] is VadEvent.NONE
    assert vad.calibrated
    assert buffer.process(_frame(2_000))[0] is VadEvent.NONE
    assert buffer.process(_frame(2_000))[0] is VadEvent.NONE
    assert buffer.process(_frame(2_000))[0] is VadEvent.SPEECH_START
    for _ in range(4):
        buffer.process(_frame(2_000))
    for _ in range(34):
        event, payload = buffer.process(_frame(0))
        assert event is VadEvent.NONE
        assert payload is None
    event, payload = buffer.process(_frame(0))

    assert event is VadEvent.SPEECH_END
    assert payload is not None
    assert len(payload) >= 39 * VAD_FRAME_BYTES


def test_sentence_segmenter_streams_complete_sentences_and_flushes_tail() -> None:
    segmenter = SentenceSegmenter()

    assert segmenter.feed("第一句还没") == ()
    assert segmenter.feed("结束。第二句！尾") == ("第一句还没结束。", "第二句！")
    assert segmenter.flush() == ("尾",)
    assert segmenter.pending == ""


def test_speech_epoch_rejects_late_turn_and_speech_events() -> None:
    epoch = SpeechEpoch()
    epoch.start_session()
    first = epoch.begin_turn()
    assert epoch.accepts(first)

    epoch.interrupt_speech()
    assert not epoch.accepts(first)
    second = epoch.begin_turn()
    assert epoch.accepts(second)
    epoch.stop_session()
    assert not epoch.accepts(second)


def test_mimo_asr_and_tts_use_official_chat_completion_shapes() -> None:
    requests: list[dict[str, object]] = []
    wav = pcm16_to_wav(_frame(500) * 10)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.xiaomimimo.com/v1/chat/completions"
        assert request.headers["api-key"] == "invalid-fake-amadeus-speech-key"
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == "mimo-v2.5-asr":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "合成转写"}}]},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"audio": {"data": base64.b64encode(wav).decode()}}}]},
            headers={"content-type": "application/json"},
        )

    client = MiMoSpeechClient(
        MiMoSpeechConfig(),
        InMemoryCredentialStore("invalid-fake-amadeus-speech-key"),
        transport=httpx.MockTransport(handler),
    )
    transcript = asyncio.run(client.transcribe(wav, CancellationToken()))
    synthesized = asyncio.run(client.synthesize("你好。", CancellationToken()))

    assert transcript == "合成转写"
    assert synthesized == wav
    assert requests[0]["asr_options"] == {"language": "auto"}
    audio_part = requests[0]["messages"][0]["content"][0]
    assert audio_part["type"] == "input_audio"
    assert audio_part["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert requests[1]["audio"] == {"format": "wav", "voice": "mimo_default"}
    assert requests[1]["messages"][1] == {"role": "assistant", "content": "你好。"}


def test_mimo_speech_honors_cancellation_before_network() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = MiMoSpeechClient(
        MiMoSpeechConfig(),
        InMemoryCredentialStore("invalid-fake-amadeus-speech-key"),
        transport=httpx.MockTransport(handler),
    )
    token = CancellationToken()
    token.cancel()

    with pytest.raises(CancellationRequested):
        asyncio.run(client.transcribe(pcm16_to_wav(_frame(1) * 2), token))
    assert not called
