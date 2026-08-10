from __future__ import annotations

import struct

from PySide6.QtMultimedia import QAudioFormat

from amadeus_desktop.audio_runtime import (
    AudioDeviceCatalog,
    WavPlaybackQueue,
    _PcmConverter,
)
from amadeus_desktop.speech import SpeechToken


def test_pcm_converter_downmixes_and_resamples_to_16khz_pcm16() -> None:
    audio_format = QAudioFormat()
    audio_format.setSampleRate(48_000)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    converter = _PcmConverter(audio_format)
    stereo_samples = [8_192, 8_192] * 480
    payload = struct.pack(f"<{len(stereo_samples)}h", *stereo_samples)

    converted = converter.convert(payload)

    assert len(converted) == 160 * 2
    values = struct.unpack("<160h", converted)
    assert all(8_180 <= value <= 8_192 for value in values)


def test_playback_queue_rejects_invalid_wav_before_device_access(qtbot) -> None:
    catalog = AudioDeviceCatalog()
    playback = WavPlaybackQueue(catalog)
    failures: list[str] = []
    playback.failed.connect(failures.append)

    assert not playback.enqueue(SpeechToken(1, 1, 1), 1, b"not-wav")
    assert failures == ["语音服务返回了无效 WAV。"]
    assert playback.queued == 0
