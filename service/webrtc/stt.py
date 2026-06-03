import logging
import numpy as np
from openai import OpenAI

async def transcribe(
    audio: tuple[int, np.ndarray],
    api_key: str,
    base_url: str = "",
    model: str = "whisper-1",
) -> str:
    """Transcribe audio using Whisper API."""
    try:
        sample_rate, audio_data = audio

        # Convert to WAV bytes
        import io
        import wave

        audio_bytes = io.BytesIO()
        with wave.open(audio_bytes, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data.tobytes())

        audio_bytes.seek(0)
        audio_bytes.name = "audio.wav"

        client = OpenAI(api_key=api_key, base_url=base_url or None)

        response = client.audio.transcriptions.create(
            model=model,
            file=audio_bytes,
            language="zh",
        )

        return response.text.strip()
    except Exception as e:
        logging.error(f"STT error: {e}")
        return ""
