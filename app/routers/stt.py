"""STT router — proxy to an OpenAI-compatible STT server.

Handles speech-to-text transcription requests. The STT server is optional;
the app degrades gracefully if it's unavailable or disabled.
"""

import base64
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.models import STTRequest, STTResponse, STTHealthResponse
from app.services.stt_client import check_stt_health, transcribe_audio

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stt"])


@router.get("/api/stt/health", response_model=STTHealthResponse)
async def stt_health():
    """Report STT availability status to the frontend."""
    settings = get_settings()
    available = await check_stt_health() if settings.stt.is_active else False
    return STTHealthResponse(
        enabled=settings.stt.is_active,
        available=available,
    )


@router.post("/api/stt", response_model=STTResponse)
async def stt_proxy(req: STTRequest):
    """Proxy a speech-to-text request to the STT server.

    Accepts base64-encoded audio from the frontend, decodes it, and forwards
    as multipart form data to the OpenAI-compatible /v1/audio/transcriptions endpoint.
    """
    settings = get_settings()
    if not settings.stt.is_active:
        return JSONResponse(status_code=503, content={"detail": "STT is disabled in settings"})

    try:
        audio_bytes = base64.b64decode(req.audio_base64)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid audio data"})

    try:
        result = await transcribe_audio(audio_bytes, mime_type=req.audio_mime_type)
    except Exception:
        return JSONResponse(status_code=502, content={"detail": "Unable to process STT data"})

    if not result or not result.get("text"):
        return JSONResponse(status_code=502, content={"detail": "Unable to process STT data"})

    return result
