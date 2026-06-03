"""
Amadeus WebRTC Service
Real-time voice conversation with AI via WebRTC
"""

import fastapi
from fastapi.middleware.cors import CORSMiddleware
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
from fastrtc import ReplyOnPause, Stream, AdditionalOutputs
import logging
import time
import asyncio
import json
import os
import numpy as np
from datetime import timedelta
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv
from contextlib import asynccontextmanager

from utils import run_async, generate_sys_prompt, generate_unique_user_id
from ai import ai_stream, predict_emotion
from stt import transcribe
from tts import text_to_speech_stream
from routes import router, init_router, get_user_config, InputData

load_dotenv()

# --- Default config ---
DEFAULT_LLM_API_KEY = os.getenv("LLM_API_KEY", "")
DEFAULT_LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
DEFAULT_LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
DEFAULT_WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
DEFAULT_WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "")
DEFAULT_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
DEFAULT_TTS_API_KEY = os.getenv("TTS_API_KEY", "")
DEFAULT_TTS_VOICE_ID = os.getenv("TTS_VOICE_ID", "")
DEFAULT_MEM0_API_KEY = os.getenv("MEM0_API_KEY", "")
DEFAULT_TIME_LIMIT = int(os.getenv("TIME_LIMIT", "600"))
DEFAULT_CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "10"))

DEFAULT_VOICE_OUTPUT_LANGUAGE = "ja"
DEFAULT_TEXT_OUTPUT_LANGUAGE = "zh"
DEFAULT_SYSTEM_PROMPT = "牧瀬紅莉栖，一个天才少女科学家，性格傲娇，不喜欢被叫克里斯蒂娜"
DEFAULT_USER_NAME = "用户"

SESSION_TIMEOUT = timedelta(seconds=DEFAULT_TIME_LIMIT)
CLEANUP_INTERVAL = 60

# --- Session state ---
user_sessions: dict[str, dict] = {}
user_sessions_last_active: dict[str, float] = {}
openai_clients: dict[str, OpenAI] = {}

logging.basicConfig(level=logging.INFO)

# ICE / TURN config
rtc_configuration = {
    "iceServers": [
        {"urls": "stun:stun.l.google.com:19302"},
    ]
}


def configure_silero_vad_download():
    """Allow FastRTC's Silero VAD model to load in strict proxy environments."""
    from pathlib import Path
    import urllib.request
    from fastrtc.pause_detection import silero

    original_download = silero.SileroVADModel.download_model
    default_path = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"

    @staticmethod
    def download_model() -> str:
        configured = os.getenv("SILERO_VAD_MODEL_PATH")
        local_path = Path(configured) if configured else default_path

        if local_path.exists():
            return str(local_path)

        try:
            return original_download()
        except Exception as exc:
            logging.warning("Hugging Face VAD download failed, falling back to direct download: %s", exc)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(
                "https://huggingface.co/freddyaboulton/silero-vad/resolve/main/silero_vad.onnx",
                local_path,
            )
            return str(local_path)

    silero.SileroVADModel.download_model = download_model


configure_silero_vad_download()


# --- Session management ---

async def cleanup_expired_sessions():
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            now = time.time()
            expired = [
                wid for wid, last in user_sessions_last_active.items()
                if now - last > SESSION_TIMEOUT.total_seconds()
            ]
            for wid in expired:
                logging.info(f"Cleaning expired session: {wid}")
                user_sessions.pop(wid, None)
                user_sessions_last_active.pop(wid, None)
                openai_clients.pop(wid, None)
        except Exception as e:
            logging.error(f"Cleanup error: {e}")


def get_user_session(webrtc_id: str) -> dict:
    user_sessions_last_active[webrtc_id] = time.time()

    if webrtc_id not in user_sessions:
        config = get_user_config(webrtc_id)
        voice_lang = config.voice_output_language if config and config.voice_output_language else DEFAULT_VOICE_OUTPUT_LANGUAGE
        text_lang = config.text_output_language if config and config.text_output_language else DEFAULT_TEXT_OUTPUT_LANGUAGE
        sys_prompt_text = config.system_prompt if config and config.system_prompt else DEFAULT_SYSTEM_PROMPT
        user_name = config.user_name if config and config.user_name else DEFAULT_USER_NAME

        sys_prompt = generate_sys_prompt(
            voice_output_language=voice_lang,
            text_output_language=text_lang,
            is_same_language=(voice_lang == text_lang),
            current_user_name=user_name,
            system_prompt=sys_prompt_text,
            model=get_user_ai_model(webrtc_id),
        )

        user_sessions[webrtc_id] = {
            "messages": [{"role": "system", "content": sys_prompt}],
            "voice_output_language": voice_lang,
            "text_output_language": text_lang,
            "system_prompt": sys_prompt_text,
            "user_name": user_name,
            "is_same_language": voice_lang == text_lang,
        }

    return user_sessions[webrtc_id]


def get_user_openai_client(webrtc_id: str) -> OpenAI:
    user_sessions_last_active[webrtc_id] = time.time()
    if webrtc_id not in openai_clients:
        config = get_user_config(webrtc_id)
        api_key = config.llm_api_key if config and config.llm_api_key else DEFAULT_LLM_API_KEY
        base_url = config.llm_base_url if config and config.llm_base_url else DEFAULT_LLM_BASE_URL
        openai_clients[webrtc_id] = OpenAI(api_key=api_key, base_url=base_url)
    return openai_clients[webrtc_id]


def get_user_ai_model(webrtc_id: str) -> str:
    config = get_user_config(webrtc_id)
    return config.ai_model if config and config.ai_model else DEFAULT_LLM_MODEL


def get_user_whisper_config(webrtc_id: str) -> dict:
    config = get_user_config(webrtc_id)
    return {
        "api_key": config.whisper_api_key if config and config.whisper_api_key else DEFAULT_WHISPER_API_KEY,
        "base_url": config.whisper_base_url if config and config.whisper_base_url else DEFAULT_WHISPER_BASE_URL,
        "model": config.whisper_model if config and config.whisper_model else DEFAULT_WHISPER_MODEL,
    }


def get_user_tts_config(webrtc_id: str) -> dict:
    config = get_user_config(webrtc_id)
    return {
        "api_key": config.tts_api_key if config and config.tts_api_key else DEFAULT_TTS_API_KEY,
        "voice_id": config.tts_voice_id if config and config.tts_voice_id else DEFAULT_TTS_VOICE_ID,
    }


# --- WebRTC handlers ---

def echo(audio: tuple[int, np.ndarray], message: str, input_data: InputData):
    session = get_user_session(input_data.webrtc_id)
    whisper_config = get_user_whisper_config(input_data.webrtc_id)

    # STT
    prompt = run_async(transcribe, audio, whisper_config["api_key"], whisper_config["base_url"], whisper_config["model"])
    if not prompt:
        return

    logging.info(f"STT: {prompt}")

    # Send transcript to frontend
    yield AdditionalOutputs(json.dumps({"type": "transcript", "data": prompt}))

    # Add to history
    session["messages"].append({"role": "user", "content": prompt})

    # LLM
    client = get_user_openai_client(input_data.webrtc_id)
    model = get_user_ai_model(input_data.webrtc_id)

    full_response = ""
    for chunk in ai_stream(client, session["messages"], model):
        full_response += chunk
        yield AdditionalOutputs(json.dumps({"type": "llm_stream", "data": chunk}))

    # Parse emotion
    emotion = "normal"
    if full_response.startswith("'''"):
        end = full_response.find("'''", 3)
        if end != -1:
            emotion = full_response[3:end]
            full_response = full_response[end + 3:].strip()

    # Send emotion
    yield AdditionalOutputs(json.dumps({"type": "emotion_response", "data": emotion}))

    # Add assistant response
    session["messages"].append({"role": "assistant", "content": full_response})

    # TTS
    tts_config = get_user_tts_config(input_data.webrtc_id)
    for chunk in text_to_speech_stream(full_response, tts_config["api_key"], tts_config["voice_id"]):
        if chunk:
            yield chunk


def startup_handler(webrtc_id: str):
    session = get_user_session(webrtc_id)
    yield AdditionalOutputs(json.dumps({"type": "connected", "data": webrtc_id}))


# --- App setup ---

reply_handler = ReplyOnPause(
    echo,
    startup_fn=startup_handler,
    can_interrupt=True,
)

stream = Stream(
    reply_handler,
    modality="audio",
    rtc_configuration=rtc_configuration,
    mode="send-receive",
    time_limit=DEFAULT_TIME_LIMIT,
    concurrency_limit=DEFAULT_CONCURRENCY_LIMIT,
)


@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    cleanup_task = asyncio.create_task(cleanup_expired_sessions())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        logging.info("Cleanup task cancelled")


app = fastapi.FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def handle_config_update(webrtc_id, message, data):
    if message == "config_updated" and isinstance(data, InputData):
        if webrtc_id in user_sessions:
            session = user_sessions[webrtc_id]
            if data.voice_output_language:
                session["voice_output_language"] = data.voice_output_language
            if data.text_output_language:
                session["text_output_language"] = data.text_output_language
            if data.system_prompt:
                session["system_prompt"] = data.system_prompt
            if data.user_name:
                session["user_name"] = data.user_name
            session["is_same_language"] = session["voice_output_language"] == session["text_output_language"]

            sys_prompt = generate_sys_prompt(
                voice_output_language=session["voice_output_language"],
                text_output_language=session["text_output_language"],
                is_same_language=session["is_same_language"],
                current_user_name=session["user_name"],
                system_prompt=session["system_prompt"],
                model=get_user_ai_model(webrtc_id),
            )
            if session["messages"] and session["messages"][0]["role"] == "system":
                session["messages"][0]["content"] = sys_prompt

        if webrtc_id in openai_clients and (data.llm_api_key or data.llm_base_url):
            api_key = data.llm_api_key or DEFAULT_LLM_API_KEY
            base_url = data.llm_base_url or DEFAULT_LLM_BASE_URL
            openai_clients[webrtc_id] = OpenAI(api_key=api_key, base_url=base_url)


init_router(stream, rtc_configuration, handle_config_update)
stream.mount(app)
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    logging.info("Starting Amadeus WebRTC server on 0.0.0.0:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)
