from dotenv import load_dotenv
import os

load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")

WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")

TTS_API_KEY = os.getenv("TTS_API_KEY", "")
TTS_VOICE_ID = os.getenv("TTS_VOICE_ID", "")

MEM0_API_KEY = os.getenv("MEM0_API_KEY", "")

TIME_LIMIT = int(os.getenv("TIME_LIMIT", "600"))
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "10"))
