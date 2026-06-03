import logging
import base64
import json
import urllib.request
from typing import AsyncGenerator

async def text_to_speech_stream(
    text: str,
    api_key: str,
    voice_id: str = "",
    model: str = "mimo-v2.5-tts",
    base_url: str = "https://token-plan-cn.xiaomimimo.com/v1",
) -> AsyncGenerator[bytes, None]:
    """Stream TTS audio. Yields audio chunks."""
    try:
        if not api_key:
            raise ValueError("TTS API key is not configured")

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "用符合牧瀬紅莉栖人设的自然女声朗读，语气直率，略带傲娇但不夸张。",
                },
                {
                    "role": "assistant",
                    "content": text,
                },
            ],
            "audio": {
                "format": "wav",
                "voice": voice_id or "冰糖",
            },
        }

        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "api-key": api_key,
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))

        audio_base64 = data["choices"][0]["message"]["audio"]["data"]
        audio_data = base64.b64decode(audio_base64)
        chunk_size = 4096
        for i in range(0, len(audio_data), chunk_size):
            yield audio_data[i:i + chunk_size]
    except Exception as e:
        logging.error(f"TTS error: {e}")
        yield b""
