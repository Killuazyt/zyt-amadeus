import logging
import base64
import json
import numpy as np
import urllib.request
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

        if "xiaomimimo.com" in base_url or model.startswith("mimo-"):
            if not api_key:
                raise ValueError("ASR API key is not configured")

            encoded_audio = base64.b64encode(audio_bytes.getvalue()).decode("utf-8")
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:audio/wav;base64,{encoded_audio}",
                                },
                            },
                        ],
                    },
                ],
                "asr_options": {
                    "language": "auto",
                },
            }
            request = urllib.request.Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "api-key": api_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

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
