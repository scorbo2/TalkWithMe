"""TTS router — proxy to the local TTS server.

Handles health checks, the /capabilities passthrough, and synthesis
requests. The TTS server is optional; the app degrades gracefully if it's
unavailable.

STT routing lives in its own module: app.routers.stt
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_personas, get_settings
from app.models import TTSRequest, TTSHealthResponse
from app.services.tts_client import (
    cached_capabilities,
    check_tts_health,
    doc_supports_reference_audio,
    encode_reference_audio,
    get_capabilities,
    read_transcript,
    synthesize,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tts"])


@router.get("/api/tts/health", response_model=TTSHealthResponse)
async def tts_health():
    """Report TTS availability status to the frontend."""
    settings = get_settings()
    available, server_type = await check_tts_health() if settings.tts.is_active else (False, None)
    return TTSHealthResponse(
        enabled=settings.tts.is_active,
        available=available,
        streaming=settings.tts.streaming,
        server_type=server_type,
    )


@router.get("/api/tts/capabilities")
async def tts_capabilities():
    """Serve the TTS server's /capabilities document (plan T5).

    The document is the payload — no wrapper: it is self-describing and
    the frontend gates on its schema_version itself. 503 when TTS is
    inactive or the server is unreachable / lacks /capabilities (the same
    convention as the STT "inactive" response; no new error machinery).
    Serves the cache when it is warm for the current base_url; otherwise
    fetches once (get_capabilities handles the fetch and negative caching).
    """
    settings = get_settings()
    if not settings.tts.is_active:
        return JSONResponse(
            status_code=503,
            content={"detail": "TTS is not active (disabled or no base_url)"},
        )
    doc = await get_capabilities()
    if doc is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "TTS server is unreachable or has no /capabilities endpoint"},
        )
    return doc


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

    # A non-cloning engine (its capabilities doc says reference_audio: null)
    # cannot serve persona TTS at all — this app's TTS is fundamentally
    # reference-audio-based, so say so before spending a request on a
    # server that cannot do the job. Only the CACHED doc is consulted, and
    # only when it belongs to the current base_url: the synthesis path must
    # never fetch (streaming issues one /synthesize per sentence), and a
    # doc from another server says nothing about this one.
    cached_url, cached_doc = cached_capabilities()
    if (
        cached_doc is not None
        and cached_url == get_settings().tts.base_url
        and not doc_supports_reference_audio(cached_doc)
    ):
        return JSONResponse(
            status_code=503,
            content={"detail": "The connected TTS engine does not support reference-audio voice cloning"},
        )

    # Load reference audio and transcript
    audio_b64 = encode_reference_audio(persona.reference_audio)
    transcript = read_transcript(persona.reference_audio_transcript)

    if not audio_b64 or not transcript:
        return JSONResponse(status_code=503, content={"detail": "TTS reference files unavailable"})

    result = await synthesize(
        text=req.text,
        reference_text=transcript,
        audio_base64=audio_b64,
        language=persona.reference_audio_language,
    )

    if not result:
        return JSONResponse(status_code=502, content={"detail": "TTS server returned no audio"})

    return result
