"""QtMultimedia microphone capture, device hot-plug, and bounded WAV playback."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QCoreApplication,
    QIODevice,
    QMicrophonePermission,
    QObject,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtMultimedia import (
    QAudio,
    QAudioDevice,
    QAudioFormat,
    QAudioOutput,
    QAudioSource,
    QMediaDevices,
    QMediaPlayer,
)

from amadeus_desktop.speech import (
    PCM_SAMPLE_RATE,
    VAD_FRAME_BYTES,
    SpeechError,
    SpeechToken,
    validate_wav_bytes,
)


def audio_device_id(device: QAudioDevice) -> str:
    return bytes(device.id()).hex()


@dataclass(frozen=True, slots=True)
class AudioDeviceInfo:
    device_id: str
    description: str
    is_default: bool


class AudioDeviceCatalog(QObject):
    changed = Signal()

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._devices = QMediaDevices(self)
        self._devices.audioInputsChanged.connect(self.changed.emit)
        self._devices.audioOutputsChanged.connect(self.changed.emit)

    def inputs(self) -> tuple[AudioDeviceInfo, ...]:
        default_id = audio_device_id(QMediaDevices.defaultAudioInput())
        return tuple(
            AudioDeviceInfo(
                audio_device_id(device), device.description(), audio_device_id(device) == default_id
            )
            for device in QMediaDevices.audioInputs()
        )

    def outputs(self) -> tuple[AudioDeviceInfo, ...]:
        default_id = audio_device_id(QMediaDevices.defaultAudioOutput())
        return tuple(
            AudioDeviceInfo(
                audio_device_id(device), device.description(), audio_device_id(device) == default_id
            )
            for device in QMediaDevices.audioOutputs()
        )


class MicrophoneCapture(QObject):
    """Emit canonical 16 kHz mono PCM16 20 ms frames without touching disk."""

    frame_ready = Signal(object)
    started = Signal(str)
    stopped = Signal()
    failed = Signal(str)

    def __init__(self, catalog: AudioDeviceCatalog, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._catalog.changed.connect(self._on_devices_changed)
        self._source: QAudioSource | None = None
        self._io: QIODevice | None = None
        self._device_id = ""
        self._converter: _PcmConverter | None = None
        self._frame_buffer = bytearray()
        self._permission_pending = False
        self._pending_device_id = ""

    @property
    def active(self) -> bool:
        return self._source is not None

    @property
    def device_id(self) -> str:
        return self._device_id

    def start(self, device_id: str = "") -> bool:
        if self.active or self._permission_pending:
            return False
        application = QCoreApplication.instance()
        if application is None:
            self.failed.emit("音频运行环境不可用。")
            return False
        permission = QMicrophonePermission()
        status = application.checkPermission(permission)
        if status is Qt.PermissionStatus.Denied:
            self.failed.emit("没有麦克风权限。")
            return False
        if status is Qt.PermissionStatus.Undetermined:
            self._permission_pending = True
            self._pending_device_id = device_id
            application.requestPermission(permission, self, self._permission_result)
            return True
        return self._start_granted(device_id)

    def stop(self) -> None:
        self._permission_pending = False
        self._pending_device_id = ""
        source = self._source
        self._source = None
        self._io = None
        self._converter = None
        self._frame_buffer.clear()
        self._device_id = ""
        if source is not None:
            source.stop()
            source.deleteLater()
            self.stopped.emit()

    def _permission_result(self, permission: QMicrophonePermission) -> None:
        self._permission_pending = False
        application = QCoreApplication.instance()
        status = (
            Qt.PermissionStatus.Denied
            if application is None
            else application.checkPermission(permission)
        )
        device_id = self._pending_device_id
        self._pending_device_id = ""
        if status is not Qt.PermissionStatus.Granted:
            self.failed.emit("没有麦克风权限。")
            return
        self._start_granted(device_id)

    def _start_granted(self, requested_id: str) -> bool:
        device = (
            _select_device(QMediaDevices.audioInputs(), requested_id)
            if requested_id
            else QMediaDevices.defaultAudioInput()
        )
        if device is None or device.isNull():
            self.failed.emit("没有可用的麦克风。")
            return False
        audio_format = device.preferredFormat()
        if not audio_format.isValid() or audio_format.channelCount() <= 0:
            self.failed.emit("麦克风格式不可用。")
            return False
        try:
            converter = _PcmConverter(audio_format)
        except ValueError:
            self.failed.emit("麦克风音频格式暂不支持。")
            return False
        source = QAudioSource(device, audio_format, self)
        source.stateChanged.connect(self._on_state_changed)
        io_device = source.start()
        if io_device is None:
            source.deleteLater()
            self.failed.emit("麦克风无法启动。")
            return False
        self._source = source
        self._io = io_device
        self._device_id = audio_device_id(device)
        self._converter = converter
        self._frame_buffer.clear()
        io_device.readyRead.connect(self._read_available)
        self.started.emit(self._device_id)
        return True

    def _read_available(self) -> None:
        if self._io is None or self._converter is None:
            return
        raw = bytes(self._io.readAll())
        if not raw:
            return
        try:
            canonical = self._converter.convert(raw)
        except ValueError:
            self.failed.emit("麦克风返回了无效音频。")
            self.stop()
            return
        self._frame_buffer.extend(canonical)
        while len(self._frame_buffer) >= VAD_FRAME_BYTES:
            frame = bytes(self._frame_buffer[:VAD_FRAME_BYTES])
            del self._frame_buffer[:VAD_FRAME_BYTES]
            self.frame_ready.emit(frame)

    def _on_state_changed(self, state: QAudio.State) -> None:
        source = self._source
        if source is None or state is not QAudio.State.StoppedState:
            return
        if source.error() is QAudio.Error.NoError:
            return
        self.failed.emit("麦克风不可用或已断开。")
        self.stop()

    def _on_devices_changed(self) -> None:
        if not self.active:
            return
        available = {audio_device_id(device) for device in QMediaDevices.audioInputs()}
        if self._device_id not in available:
            self.failed.emit("麦克风已断开。")
            self.stop()


@dataclass(slots=True)
class _PlaybackItem:
    token: SpeechToken
    sequence: int
    wav_bytes: bytes


class WavPlaybackQueue(QObject):
    """Play validated in-memory WAV responses serially with a hard queue bound."""

    item_started = Signal(object, int)
    item_finished = Signal(object, int)
    queue_empty = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        catalog: AudioDeviceCatalog,
        *,
        capacity: int = 3,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if capacity <= 0:
            raise ValueError("playback capacity must be positive")
        self._catalog = catalog
        self._catalog.changed.connect(self._on_devices_changed)
        self._capacity = capacity
        self._pending: deque[_PlaybackItem] = deque()
        self._current: _PlaybackItem | None = None
        self._buffer: QBuffer | None = None
        self._device_id = ""
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)

    @property
    def active(self) -> bool:
        return self._current is not None

    @property
    def queued(self) -> int:
        return len(self._pending) + (1 if self._current is not None else 0)

    def set_device(self, device_id: str = "") -> bool:
        device = (
            _select_device(QMediaDevices.audioOutputs(), device_id)
            if device_id
            else QMediaDevices.defaultAudioOutput()
        )
        if device is None or device.isNull():
            self.failed.emit("没有可用的音频输出设备。")
            return False
        self._device_id = audio_device_id(device)
        self._audio_output.setDevice(device)
        return True

    def enqueue(self, token: SpeechToken, sequence: int, wav_bytes: bytes) -> bool:
        if self.queued >= self._capacity:
            return False
        try:
            validate_wav_bytes(wav_bytes, maximum_seconds=300)
        except SpeechError:
            self.failed.emit("语音服务返回了无效 WAV。")
            return False
        if not self._device_id and not self.set_device(""):
            return False
        self._pending.append(_PlaybackItem(token, int(sequence), bytes(wav_bytes)))
        if self._current is None:
            self._play_next()
        return True

    def stop(self) -> None:
        self._pending.clear()
        self._player.stop()
        self._release_buffer()
        self._current = None

    def _play_next(self) -> None:
        if self._current is not None or not self._pending:
            return
        item = self._pending.popleft()
        buffer = QBuffer(self)
        buffer.setData(QByteArray(item.wav_bytes))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            buffer.deleteLater()
            self.failed.emit("语音播放缓冲区无法打开。")
            return
        self._current = item
        self._buffer = buffer
        self._player.setSourceDevice(buffer, QUrl("memory:speech.wav"))
        self._player.play()
        self.item_started.emit(item.token, item.sequence)

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status is QMediaPlayer.MediaStatus.EndOfMedia:
            item = self._current
            self._current = None
            self._release_buffer()
            if item is not None:
                self.item_finished.emit(item.token, item.sequence)
            if self._pending:
                self._play_next()
            elif item is not None:
                self.queue_empty.emit(item.token)
        elif status is QMediaPlayer.MediaStatus.InvalidMedia:
            self.failed.emit("语音服务返回的 WAV 无法播放。")
            self.stop()

    def _on_error(self, _error: QMediaPlayer.Error, _message: str) -> None:
        self.failed.emit("语音播放失败。")
        self.stop()

    def _release_buffer(self) -> None:
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
            self._buffer = None

    def _on_devices_changed(self) -> None:
        if not self._device_id:
            return
        available = {audio_device_id(device) for device in QMediaDevices.audioOutputs()}
        if self._device_id not in available:
            self.failed.emit("音频输出设备已断开。")
            self.stop()
            self._device_id = ""


class _PcmConverter:
    def __init__(self, audio_format: QAudioFormat) -> None:
        self._rate = audio_format.sampleRate()
        self._channels = audio_format.channelCount()
        self._format = audio_format.sampleFormat()
        self._pending = bytearray()
        self._bytes_per_sample = audio_format.bytesPerSample()
        if (
            self._rate <= 0
            or self._channels <= 0
            or self._bytes_per_sample <= 0
            or self._format is QAudioFormat.SampleFormat.Unknown
        ):
            raise ValueError("unsupported PCM format")

    def convert(self, payload: bytes) -> bytes:
        self._pending.extend(payload)
        frame_bytes = self._bytes_per_sample * self._channels
        usable = len(self._pending) - len(self._pending) % frame_bytes
        if usable <= 0:
            return b""
        raw = bytes(self._pending[:usable])
        del self._pending[:usable]
        samples = self._decode(raw).reshape((-1, self._channels)).mean(axis=1)
        if self._rate != PCM_SAMPLE_RATE and samples.size > 1:
            output_count = max(1, round(samples.size * PCM_SAMPLE_RATE / self._rate))
            old_positions = np.linspace(0.0, 1.0, samples.size, endpoint=False)
            new_positions = np.linspace(0.0, 1.0, output_count, endpoint=False)
            samples = np.interp(new_positions, old_positions, samples)
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype("<i2").tobytes()

    def _decode(self, payload: bytes) -> np.ndarray:
        if self._format is QAudioFormat.SampleFormat.UInt8:
            return (np.frombuffer(payload, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        if self._format is QAudioFormat.SampleFormat.Int16:
            return np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        if self._format is QAudioFormat.SampleFormat.Int32:
            return np.frombuffer(payload, dtype="<i4").astype(np.float32) / 2147483648.0
        if self._format is QAudioFormat.SampleFormat.Float:
            return np.frombuffer(payload, dtype="<f4").astype(np.float32)
        raise ValueError("unsupported PCM sample format")


def _select_device(devices: list[QAudioDevice], requested_id: str) -> QAudioDevice | None:
    if requested_id:
        for device in devices:
            if audio_device_id(device) == requested_id:
                return device
        return None
    return devices[0] if devices else None
