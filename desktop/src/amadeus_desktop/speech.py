"""Provider-neutral low-latency sentence speech primitives and MiMo contracts."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import re
import struct
import wave
from collections import deque
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from amadeus_desktop.chat_provider import CancellationRequested, CancellationToken
from amadeus_desktop.credential_store import CredentialStore

MIMO_SPEECH_BASE_URL = "https://api.xiaomimimo.com/v1"
ASR_MODEL = "mimo-v2.5-asr"
TTS_MODEL = "mimo-v2.5-tts"
TTS_VOICE = "mimo_default"
TTS_FORMAT = "wav"
PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
VAD_FRAME_MS = 20
VAD_FRAME_BYTES = PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH * VAD_FRAME_MS // 1_000
MAX_UTTERANCE_SECONDS = 60
MAX_UTTERANCE_BYTES = PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH * MAX_UTTERANCE_SECONDS
MAX_ASR_RESPONSE_BYTES = 2 * 1024**2
MAX_TTS_RESPONSE_BYTES = 16 * 1024**2
MAX_TRANSCRIPT_CHARS = 64_000
MAX_TTS_TEXT_CHARS = 2_000


class VoiceState(StrEnum):
    OFF = "off"
    LISTENING = "listening"
    CAPTURING = "capturing"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VadEvent(StrEnum):
    NONE = "none"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    MAX_DURATION = "max_duration"


class SpeechErrorCode(StrEnum):
    CREDENTIAL = "credential"
    NETWORK = "network"
    TIMEOUT = "timeout"
    PROVIDER = "provider"
    PROTOCOL = "protocol"
    CANCELLED = "cancelled"
    DEVICE = "device"
    PERMISSION = "permission"


_SAFE_ERRORS = {
    SpeechErrorCode.CREDENTIAL: "MiMo 语音密钥不可用。",
    SpeechErrorCode.NETWORK: "语音服务网络连接失败。",
    SpeechErrorCode.TIMEOUT: "语音服务响应超时。",
    SpeechErrorCode.PROVIDER: "语音服务暂时不可用。",
    SpeechErrorCode.PROTOCOL: "语音服务返回了无效数据。",
    SpeechErrorCode.CANCELLED: "语音操作已取消。",
    SpeechErrorCode.DEVICE: "音频设备不可用或已断开。",
    SpeechErrorCode.PERMISSION: "没有麦克风权限。",
}


class SpeechError(RuntimeError):
    def __init__(self, code: SpeechErrorCode) -> None:
        self.code = SpeechErrorCode(code)
        self.safe_message = _SAFE_ERRORS[self.code]
        super().__init__(self.safe_message)


class SpeechTranscriber(Protocol):
    async def transcribe(self, wav_bytes: bytes, cancellation: CancellationToken) -> str: ...


class SpeechSynthesizer(Protocol):
    async def synthesize(self, text: str, cancellation: CancellationToken) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SpeechToken:
    """Immutable identifiers used to reject every late voice event."""

    epoch: int
    turn_token: int
    speech_id: int


class SpeechEpoch:
    """Single monotonic source of session, turn, and playback identities."""

    def __init__(self) -> None:
        self._epoch = 0
        self._turn_token = 0
        self._speech_id = 0

    @property
    def current(self) -> SpeechToken:
        return SpeechToken(self._epoch, self._turn_token, self._speech_id)

    def start_session(self) -> SpeechToken:
        self._epoch += 1
        self._turn_token = 0
        self._speech_id = 0
        return self.current

    def stop_session(self) -> SpeechToken:
        self._epoch += 1
        self._turn_token = 0
        self._speech_id = 0
        return self.current

    def begin_turn(self) -> SpeechToken:
        self._turn_token += 1
        self._speech_id += 1
        return self.current

    def interrupt_speech(self) -> SpeechToken:
        self._speech_id += 1
        return self.current

    def accepts(self, token: SpeechToken) -> bool:
        return token == self.current


class AdaptiveEnergyVad:
    """Local PCM16 energy VAD with calibration, hysteresis, and bounded duration."""

    def __init__(
        self,
        *,
        calibration_ms: int = 600,
        end_silence_ms: int = 700,
        start_frames: int = 3,
        maximum_seconds: int = MAX_UTTERANCE_SECONDS,
        minimum_threshold: float = 250.0,
        threshold_multiplier: float = 3.0,
    ) -> None:
        if calibration_ms < VAD_FRAME_MS or end_silence_ms < VAD_FRAME_MS:
            raise ValueError("VAD calibration and silence windows must be positive")
        if start_frames <= 0 or maximum_seconds <= 0:
            raise ValueError("VAD frame and duration limits must be positive")
        self._calibration_frames = math.ceil(calibration_ms / VAD_FRAME_MS)
        self._end_silence_frames = math.ceil(end_silence_ms / VAD_FRAME_MS)
        self._start_frames = start_frames
        self._maximum_frames = math.ceil(maximum_seconds * 1_000 / VAD_FRAME_MS)
        self._minimum_threshold = float(minimum_threshold)
        self._threshold_multiplier = float(threshold_multiplier)
        self.reset()

    @property
    def calibrated(self) -> bool:
        return self._calibration_count >= self._calibration_frames

    @property
    def threshold(self) -> float:
        return max(self._minimum_threshold, self._noise_energy * self._threshold_multiplier)

    @property
    def speaking(self) -> bool:
        return self._speaking

    def reset(self, *, keep_calibration: bool = False) -> None:
        if not keep_calibration:
            self._noise_energy = 0.0
            self._calibration_count = 0
        self._speech_candidate_frames = 0
        self._silence_frames = 0
        self._utterance_frames = 0
        self._speaking = False

    def process(self, frame: bytes) -> VadEvent:
        energy = pcm16_rms(frame)
        if not self.calibrated:
            self._calibration_count += 1
            weight = 1.0 / self._calibration_count
            self._noise_energy += (energy - self._noise_energy) * weight
            return VadEvent.NONE

        voiced = energy >= self.threshold
        if not self._speaking:
            if voiced:
                self._speech_candidate_frames += 1
            else:
                self._speech_candidate_frames = 0
                self._noise_energy = self._noise_energy * 0.98 + energy * 0.02
            if self._speech_candidate_frames >= self._start_frames:
                self._speaking = True
                self._utterance_frames = self._speech_candidate_frames
                self._silence_frames = 0
                return VadEvent.SPEECH_START
            return VadEvent.NONE

        self._utterance_frames += 1
        if self._utterance_frames >= self._maximum_frames:
            self.reset(keep_calibration=True)
            return VadEvent.MAX_DURATION
        if voiced:
            self._silence_frames = 0
        else:
            self._silence_frames += 1
            if self._silence_frames >= self._end_silence_frames:
                self.reset(keep_calibration=True)
                return VadEvent.SPEECH_END
        return VadEvent.NONE


class VadUtteranceBuffer:
    """Keep a short pre-roll and return one complete PCM sentence at a time."""

    def __init__(self, vad: AdaptiveEnergyVad | None = None, *, preroll_ms: int = 240) -> None:
        self.vad = vad or AdaptiveEnergyVad()
        self._preroll = deque(maxlen=max(1, math.ceil(preroll_ms / VAD_FRAME_MS)))
        self._frames: list[bytes] = []

    @property
    def capturing(self) -> bool:
        return bool(self._frames)

    def reset(self, *, keep_calibration: bool = False) -> None:
        self.vad.reset(keep_calibration=keep_calibration)
        self._preroll.clear()
        self._frames.clear()

    def process(self, frame: bytes) -> tuple[VadEvent, bytes | None]:
        event = self.vad.process(frame)
        if event is VadEvent.SPEECH_START:
            self._frames = [*self._preroll, frame]
            self._preroll.clear()
            return event, None
        if self._frames:
            self._frames.append(frame)
        else:
            self._preroll.append(frame)
        if event in {VadEvent.SPEECH_END, VadEvent.MAX_DURATION}:
            payload = b"".join(self._frames)
            self._frames.clear()
            self._preroll.clear()
            return event, payload[:MAX_UTTERANCE_BYTES]
        return event, None


class SentenceSegmenter:
    """Incrementally split streamed model text at readable sentence boundaries."""

    _BOUNDARY = re.compile(r".*?(?:[。！？!?；;\n]+|(?<!\d)\.(?:\s+|$))", re.DOTALL)

    def __init__(self, *, maximum_pending_chars: int = 240) -> None:
        self._maximum_pending_chars = max(32, maximum_pending_chars)
        self._buffer = ""

    @property
    def pending(self) -> str:
        return self._buffer

    def feed(self, chunk: str) -> tuple[str, ...]:
        if not isinstance(chunk, str):
            raise TypeError("speech chunks must be strings")
        self._buffer += chunk
        sentences: list[str] = []
        while self._buffer:
            match = self._BOUNDARY.match(self._buffer)
            if match is not None:
                value = match.group(0).strip()
                self._buffer = self._buffer[match.end() :]
                if value:
                    sentences.append(value)
                continue
            if len(self._buffer) >= self._maximum_pending_chars:
                split_at = self._buffer.rfind(" ", 0, self._maximum_pending_chars)
                if split_at < self._maximum_pending_chars // 2:
                    split_at = self._maximum_pending_chars
                value = self._buffer[:split_at].strip()
                self._buffer = self._buffer[split_at:].lstrip()
                if value:
                    sentences.append(value)
                continue
            break
        return tuple(sentences)

    def flush(self) -> tuple[str, ...]:
        value = self._buffer.strip()
        self._buffer = ""
        return (value,) if value else ()

    def clear(self) -> None:
        self._buffer = ""


@dataclass(frozen=True, slots=True)
class MiMoSpeechConfig:
    base_url: str = MIMO_SPEECH_BASE_URL
    asr_model: str = ASR_MODEL
    tts_model: str = TTS_MODEL
    tts_voice: str = TTS_VOICE
    tts_format: str = TTS_FORMAT
    connect_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 90.0

    def validated(self) -> MiMoSpeechConfig:
        if self.base_url.rstrip("/") != MIMO_SPEECH_BASE_URL:
            raise ValueError("only the MiMo PAYG speech endpoint is supported")
        if (
            self.asr_model != ASR_MODEL
            or self.tts_model != TTS_MODEL
            or self.tts_voice != TTS_VOICE
            or self.tts_format != TTS_FORMAT
        ):
            raise ValueError("unsupported MiMo speech contract")
        if self.connect_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("speech timeouts must be positive")
        return self


class MiMoSpeechClient(SpeechTranscriber, SpeechSynthesizer):
    """Strict MiMo PAYG ASR/TTS over the official Chat Completions contracts."""

    def __init__(
        self,
        config: MiMoSpeechConfig,
        credential_store: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config.validated()
        self._credential_store = credential_store
        self._transport = transport

    async def transcribe(self, wav_bytes: bytes, cancellation: CancellationToken) -> str:
        validate_wav_bytes(wav_bytes, maximum_seconds=MAX_UTTERANCE_SECONDS)
        encoded = base64.b64encode(wav_bytes).decode("ascii")
        payload = {
            "model": self.config.asr_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:audio/wav;base64,{encoded}"},
                        }
                    ],
                }
            ],
            "asr_options": {"language": "auto"},
        }
        response = await self._post(payload, cancellation, MAX_ASR_RESPONSE_BYTES)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SpeechError(SpeechErrorCode.PROTOCOL) from exc
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > MAX_TRANSCRIPT_CHARS
        ):
            raise SpeechError(SpeechErrorCode.PROTOCOL)
        return content.strip()

    async def synthesize(self, text: str, cancellation: CancellationToken) -> bytes:
        value = str(text).strip()
        if not value or len(value) > MAX_TTS_TEXT_CHARS:
            raise SpeechError(SpeechErrorCode.PROTOCOL)
        payload = {
            "model": self.config.tts_model,
            "messages": [
                {"role": "user", "content": "请使用自然、清晰的语气朗读。"},
                {"role": "assistant", "content": value},
            ],
            "audio": {"format": self.config.tts_format, "voice": self.config.tts_voice},
        }
        response = await self._post(payload, cancellation, MAX_TTS_RESPONSE_BYTES)
        try:
            encoded = response["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SpeechError(SpeechErrorCode.PROTOCOL) from exc
        if not isinstance(encoded, str) or not encoded:
            raise SpeechError(SpeechErrorCode.PROTOCOL)
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise SpeechError(SpeechErrorCode.PROTOCOL) from exc
        if not audio or len(audio) > MAX_TTS_RESPONSE_BYTES:
            raise SpeechError(SpeechErrorCode.PROTOCOL)
        validate_wav_bytes(audio, maximum_seconds=300)
        return audio

    async def _post(
        self,
        payload: dict[str, object],
        cancellation: CancellationToken,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        cancellation.bind_current_task()
        response: httpx.Response | None = None
        try:
            cancellation.raise_if_cancelled()
            secret = self._read_secret()
            timeout = httpx.Timeout(
                self.config.request_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
            )
            kwargs: dict[str, object] = {
                "timeout": timeout,
                "follow_redirects": False,
                "trust_env": False,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            async with asyncio.timeout(self.config.request_timeout_seconds):
                async with httpx.AsyncClient(**kwargs) as client:
                    request = client.build_request(
                        "POST",
                        f"{self.config.base_url}/chat/completions",
                        headers={
                            "api-key": secret,
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                        },
                        json=payload,
                    )
                    response = await client.send(request, stream=True)
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SpeechError(
                            SpeechErrorCode.CREDENTIAL
                            if response.status_code in {401, 403}
                            else SpeechErrorCode.PROVIDER
                        )
                    encoding = response.headers.get("content-encoding", "").casefold().strip()
                    if encoding not in {"", "identity"}:
                        raise SpeechError(SpeechErrorCode.PROTOCOL)
                    body = await _read_limited(response.aiter_bytes(), maximum_response_bytes)
            cancellation.raise_if_cancelled()
            value = json.loads(body)
            if not isinstance(value, dict):
                raise SpeechError(SpeechErrorCode.PROTOCOL)
            return value
        except CancellationRequested:
            raise
        except asyncio.CancelledError as exc:
            raise CancellationRequested from exc
        except TimeoutError as exc:
            raise SpeechError(SpeechErrorCode.TIMEOUT) from exc
        except httpx.TimeoutException as exc:
            raise SpeechError(SpeechErrorCode.TIMEOUT) from exc
        except (httpx.NetworkError, httpx.ProxyError) as exc:
            raise SpeechError(SpeechErrorCode.NETWORK) from exc
        except SpeechError:
            raise
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise SpeechError(SpeechErrorCode.PROTOCOL) from exc
        except Exception as exc:
            raise SpeechError(SpeechErrorCode.PROTOCOL) from exc
        finally:
            if response is not None:
                with suppress(Exception):
                    await response.aclose()
            cancellation.unbind_current_task()

    def _read_secret(self) -> str:
        try:
            secret = self._credential_store.read_secret()
        except Exception as exc:
            raise SpeechError(SpeechErrorCode.CREDENTIAL) from exc
        if not isinstance(secret, str) or not secret.strip():
            raise SpeechError(SpeechErrorCode.CREDENTIAL)
        secret = secret.strip()
        if secret.casefold().startswith("tp-"):
            raise SpeechError(SpeechErrorCode.CREDENTIAL)
        return secret


def pcm16_rms(frame: bytes) -> float:
    if len(frame) != VAD_FRAME_BYTES:
        raise ValueError(f"PCM frame must contain exactly {VAD_FRAME_BYTES} bytes")
    sample_count = len(frame) // PCM_SAMPLE_WIDTH
    samples = struct.unpack(f"<{sample_count}h", frame)
    return math.sqrt(sum(sample * sample for sample in samples) / sample_count)


def pcm16_to_wav(pcm_bytes: bytes) -> bytes:
    if not pcm_bytes or len(pcm_bytes) % PCM_SAMPLE_WIDTH or len(pcm_bytes) > MAX_UTTERANCE_BYTES:
        raise ValueError("PCM utterance size is invalid")
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(PCM_CHANNELS)
        handle.setsampwidth(PCM_SAMPLE_WIDTH)
        handle.setframerate(PCM_SAMPLE_RATE)
        handle.writeframes(pcm_bytes)
    return output.getvalue()


def validate_wav_bytes(payload: bytes, *, maximum_seconds: int) -> None:
    if not isinstance(payload, bytes) or not payload:
        raise SpeechError(SpeechErrorCode.PROTOCOL)
    try:
        with wave.open(io.BytesIO(payload), "rb") as handle:
            if handle.getnchannels() <= 0 or handle.getsampwidth() not in {1, 2, 3, 4}:
                raise SpeechError(SpeechErrorCode.PROTOCOL)
            if handle.getframerate() <= 0 or handle.getnframes() <= 0:
                raise SpeechError(SpeechErrorCode.PROTOCOL)
            duration = handle.getnframes() / handle.getframerate()
            if duration > maximum_seconds:
                raise SpeechError(SpeechErrorCode.PROTOCOL)
    except SpeechError:
        raise
    except (EOFError, wave.Error) as exc:
        raise SpeechError(SpeechErrorCode.PROTOCOL) from exc


async def _read_limited(chunks: AsyncIterator[bytes], maximum_bytes: int) -> bytes:
    collected = bytearray()
    async for chunk in chunks:
        collected.extend(chunk)
        if len(collected) > maximum_bytes:
            raise SpeechError(SpeechErrorCode.PROTOCOL)
    if not collected:
        raise SpeechError(SpeechErrorCode.PROTOCOL)
    return bytes(collected)
