import logging
import io
from typing import AsyncGenerator

async def text_to_speech_stream(
    text: str,
    api_key: str,
    voice_id: str = "",
    model: str = "CosyVoice2-0.5B",
) -> AsyncGenerator[bytes, None]:
    """Stream TTS audio. Yields audio chunks."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url="https://api.siliconflow.cn/v1")

        response = client.audio.speech.create(
            model=model,
            voice=voice_id or "alloy",
            input=text,
            response_format="mp3",
        )

        audio_data = response.content
        chunk_size = 4096
        for i in range(0, len(audio_data), chunk_size):
            yield audio_data[i:i + chunk_size]
    except Exception as e:
        logging.error(f"TTS error: {e}")
        yield b""
