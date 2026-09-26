"""STT client — talks to an OpenAI-compatible STT server.

Sends raw audio as a multipart form POST to /v1/audio/transcriptions.
The STT server is optional. If it's down or misconfigured, the app logs
a warning and returns None to the caller.
"""

import logging
from typing import Optional

import httpx

from app.config import get_settings
from app.services.mime import mime_to_extension

logger = logging.getLogger(__name__)


async def check_stt_health() -> bool:
    """Return True if the STT server is reachable.

    Accepts both 200 (endpoint exists) and 404 (server is up but lacks /health).
    Any other outcome — connection error, timeout, 5xx — means the server is down.
    """
    settings = get_settings()
    if not settings.stt.is_active:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.stt.base_url}/health")
            return resp.status_code in (200, 404)
    except Exception:
        return False


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/webm") -> Optional[dict]:
    """Call the STT server's /v1/audio/transcriptions endpoint.

    Sends raw audio as multipart form data with response_format=json.
    Returns dict with keys: text, language, language_probability.
    Returns None on any failure.
    """
    settings = get_settings()
    if not settings.stt.is_active:
        logger.warning("STT transcribe skipped: feature not active (no base_url or disabled)")
        return None
    if not audio_bytes:
        logger.warning("STT transcribe skipped: no audio data provided")
        return None
    url = f"{settings.stt.base_url}/v1/audio/transcriptions"

    # Derive a sensible filename from the MIME type so the STT server
    # can identify the format. E.g. "audio/ogg" -> "audio.ogg".
    # mime_to_extension() is deterministic on purpose — never the OS's
    # mime database (see app/services/mime.py); a blank/None MIME type
    # defaults to webm above rather than falling through to "bin".
    mime_type = (mime_type or "audio/webm").split(";", 1)[0].strip() or "audio/webm"
    ext = mime_to_extension(mime_type)
    files = {
        "file": (f"audio.{ext}", audio_bytes, mime_type),
    }
    data = {
        "response_format": "json",
    }

    try:
        async with httpx.AsyncClient(timeout=settings.stt.timeout) as client:
            resp = await client.post(url, files=files, data=data)
            resp.raise_for_status()
            json_response = resp.json()
            text = json_response.get("text") or "No response received from STT server"
            return {
                "text": text,
                # "language" is optional in the response; default to "en" if absent
                "language": json_response.get("language") or "en",
                # "language_probability" is optional; None if the server doesn't provide it
                "language_probability": json_response.get("language_probability"),
            }
    except httpx.ConnectError as exc:
        logger.error("STT connect error (server unreachable at %s): %s", url, exc)
    except httpx.TimeoutException as exc:
        logger.error("STT timeout after %.0fs: %s", settings.stt.timeout, exc)
    except httpx.HTTPStatusError as exc:
        logger.error("STT HTTP %d from %s: %s", exc.response.status_code, url, exc)
    except Exception as exc:
        logger.warning("STT transcribe failed: %s", exc)
    return None
