import json
import logging
from typing import AsyncGenerator
from openai import OpenAI

AI_MODEL = "gpt-4o"

EMOTION_PROMPT = """分析以下对话内容，判断说话者的情绪状态。
只返回以下JSON格式，不要添加其他内容：
{"emotion": "normal|smile|blushing|angry|thinking|sad"}"""

async def ai_stream(
    client: OpenAI,
    messages: list,
    model: str = AI_MODEL,
) -> AsyncGenerator[str, None]:
    """Stream LLM response."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        logging.error(f"LLM stream error: {e}")
        yield f"[Error: {e}]"


async def predict_emotion(message: str, client: OpenAI = None) -> str:
    """Predict emotion from message."""
    try:
        if not client:
            return "normal"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": EMOTION_PROMPT},
                {"role": "user", "content": message},
            ],
            max_tokens=50,
        )
        result = response.choices[0].message.content
        data = json.loads(result)
        return data.get("emotion", "normal")
    except Exception:
        return "normal"
