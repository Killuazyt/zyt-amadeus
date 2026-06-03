import asyncio
import functools
import json
import logging
from typing import Any

def run_async(func, *args, **kwargs):
    """Run an async function from sync context."""
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, func(*args, **kwargs)).result()
    return loop.run_until_complete(func(*args, **kwargs))


SYSTEM_PROMPT_TEMPLATE = """你是命运石之门中的牧瀬紅莉栖（Makise Kurisu），通过 Amadeus 系统与用户对话。
你是一个天才少女科学家，性格直率、略带傲娇，喜欢吐槽但内心善良。
不要承认自己是AI或语言模型。保持角色扮演。

当前用户: {user_name}

在每次回复的开头，用三重引号标注你的情绪，可选值：
'''normal''' '''smile''' '''blushing''' '''angry''' '''thinking''' '''sad'''
然后换行开始正式回复。只回复对话内容，不要添加解释说明。

语言要求：
- 语音输出语言: {voice_output_language}
- 文字输出语言: {text_output_language}
{extra_instructions}"""


def generate_sys_prompt(
    voice_output_language: str = 'ja',
    text_output_language: str = 'zh',
    is_same_language: bool = False,
    current_user_name: str = '用户',
    system_prompt: str = '',
    model: str = '',
) -> str:
    if system_prompt:
        base = system_prompt
    else:
        base = "牧瀬紅莉栖，一个天才少女科学家，性格傲娇"

    extra = ""
    if not is_same_language:
        extra = f"- 请用{text_output_language}回复文字内容"

    return SYSTEM_PROMPT_TEMPLATE.format(
        user_name=current_user_name,
        voice_output_language=voice_output_language,
        text_output_language=text_output_language,
        extra_instructions=extra,
    )


def generate_unique_user_id(username: str) -> str:
    import hashlib
    return hashlib.md5(f"amadeus_{username}".encode()).hexdigest()[:12]
