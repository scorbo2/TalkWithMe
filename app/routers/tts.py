"""TTS/STT router — proxy to the local TTS/STT server.

Handles health checks, synthesis requests, and speech-to-text transcription.
The TTS/STT server is optional; the app degrades gracefully if it's unavailable.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_personas, get_settings
from app.models import TTSRequest, TTSHealthResponse, STTRequest, STTResponse
from app.services.tts_client import check_tts_health, encode_reference_audio, read_transcript, synthesize, parse_audio

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tts"])


@router.get("/api/tts/health", response_model=TTSHealthResponse)
async def tts_health():
    """Report TTS availability status to the frontend."""
    settings = get_settings()
    available = await check_tts_health() if settings.tts.enabled else False
    return TTSHealthResponse(enabled=settings.tts.enabled, available=available)


@router.post("/api/tts")
async def tts_proxy(req: TTSRequest):
    """Proxy a synthesis request to the TTS server.

    Looks up the persona's reference audio and transcript, then calls
    the TTS server's /synthesize endpoint.
    """
    config = get_personas()
    persona = next((p for p in config.personas if p.name == req.persona_name), None)
    if not persona:
        return JSONResponse(status_code=404, content={"detail": f"Persona '{req.persona_name}' not found"})

    if not persona.tts_capable:
        return JSONResponse(status_code=400, content={"detail": "This persona does not have TTS configured"})

    # Load reference audio and transcript
    audio_b64 = encode_reference_audio(persona.reference_audio)
    transcript = read_transcript(persona.reference_audio_transcript)

    if not audio_b64 or not transcript:
        return JSONResponse(status_code=503, content={"detail": "TTS reference files unavailable"})

    result = await synthesize(
        text=req.text,
        prompt_text=transcript,
        audio_base64=audio_b64,
        language=persona.language,
    )

    if not result:
        return JSONResponse(status_code=502, content={"detail": "TTS server returned no audio"})

    return result


@router.post("/api/stt")
async def stt_proxy(req: STTRequest):
    """Proxy a speech-to-text request to the STT server's /parse endpoint.

    Accepts base64-encoded audio and returns the transcribed text.
    """
    try:
        result = await parse_audio(req.audio_base64)
    except Exception:
        return JSONResponse(status_code=502, content={"detail": "STT server unavailable or returned an error"})

    if not result or "text" not in result:
        return JSONResponse(status_code=502, content={"detail": "STT server returned no text"})

    return result
