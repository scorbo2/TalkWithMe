"""STT router — proxy to the local STT server.

Handles speech-to-text transcription requests. The STT server is optional;
the app degrades gracefully if it's unavailable or disabled.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.models import STTRequest, STTResponse
from app.services.stt_client import parse_audio

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stt"])


@router.post("/api/stt", response_model=STTResponse)
async def stt_proxy(req: STTRequest):
    """Proxy a speech-to-text request to the STT server's /parse endpoint.

    Accepts base64-encoded audio and returns the transcribed text.
    """
    settings = get_settings()
    if not settings.stt.enabled:
        return JSONResponse(status_code=503, content={"detail": "STT is disabled in settings"})

    try:
        result = await parse_audio(req.audio_base64)
    except Exception:
        return JSONResponse(status_code=502, content={"detail": "STT server unavailable or returned an error"})

    if not result or "text" not in result:
        return JSONResponse(status_code=502, content={"detail": "STT server returned no text"})

    return result
