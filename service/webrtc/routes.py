from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import logging
import json
import os
from typing import Optional
from pydantic import BaseModel

router = APIRouter()

# Global refs initialized from server.py
stream = None
rtc_configuration = None
handle_config_update = None


class InputData(BaseModel):
    webrtc_id: str
    llm_api_key: Optional[str] = None
    whisper_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    whisper_base_url: Optional[str] = None
    whisper_model: Optional[str] = None
    ai_model: Optional[str] = None
    voice_output_language: Optional[str] = "ja"
    text_output_language: Optional[str] = "zh"
    system_prompt: Optional[str] = None
    user_name: Optional[str] = "用户"


class BuiltinServiceRequest(BaseModel):
    webrtc_id: str
    ai_model: Optional[str] = None
    voice_output_language: Optional[str] = "ja"
    text_output_language: Optional[str] = "zh"
    system_prompt: Optional[str] = None
    user_name: Optional[str] = "用户"


user_configs: dict[str, InputData] = {}


def init_router(stream_obj, rtc_config=None, config_handler=None):
    global stream, rtc_configuration, handle_config_update
    stream = stream_obj
    rtc_configuration = rtc_config
    handle_config_update = config_handler
    logging.info("Router initialized")


def get_user_config(webrtc_id: str) -> Optional[InputData]:
    return user_configs.get(webrtc_id)


@router.get("/reset/{webrtc_id}")
async def reset(webrtc_id: str):
    from server import user_sessions, get_user_session
    if webrtc_id in user_sessions:
        user_sessions[webrtc_id]["messages"] = [user_sessions[webrtc_id]["messages"][0]]
    else:
        get_user_session(webrtc_id)
    return {"status": "success"}


@router.get("/webrtc/ice-config")
async def get_ice_config():
    if rtc_configuration:
        return rtc_configuration
    return {"iceServers": []}


@router.get("/events")
async def events(webrtc_id: str):
    async def output_stream():
        try:
            async for output in stream.output_stream(webrtc_id):
                if output.args and len(output.args) > 0:
                    json_data = output.args[0]
                    yield f"event: message\ndata: {json_data}\n\n"
        except Exception as e:
            logging.error(f"Event stream error: {e}")
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'data': str(e)})}\n\n"

    return StreamingResponse(
        output_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.post("/input_hook")
async def input_hook(data: InputData):
    user_configs[data.webrtc_id] = data
    if handle_config_update:
        handle_config_update(data.webrtc_id, "config_updated", data)
    stream.set_input(data.webrtc_id, "config_updated", data)
    return {"status": "success"}


@router.post("/use_builtin_service")
async def use_builtin_service(data: BuiltinServiceRequest):
    built_in_config = InputData(
        webrtc_id=data.webrtc_id,
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        whisper_api_key=os.environ.get("WHISPER_API_KEY", ""),
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        whisper_base_url=os.environ.get("WHISPER_BASE_URL", ""),
        whisper_model=os.environ.get("WHISPER_MODEL", "whisper-1"),
        ai_model=data.ai_model or os.environ.get("LLM_MODEL", "gpt-4o"),
        voice_output_language=data.voice_output_language or "ja",
        text_output_language=data.text_output_language or "zh",
        system_prompt=data.system_prompt or os.environ.get("SYSTEM_PROMPT", ""),
        user_name=data.user_name or "用户",
    )
    user_configs[data.webrtc_id] = built_in_config
    if handle_config_update:
        handle_config_update(data.webrtc_id, "config_updated", built_in_config)
    stream.set_input(data.webrtc_id, "config_updated", built_in_config)
    return {"status": "success"}
