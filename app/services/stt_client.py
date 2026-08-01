"""STT client — talks to an OpenAI-compatible STT server.

Sends raw audio as a multipart form POST to /v1/audio/transcriptions.
The STT server is optional. If it's down or misconfigured, the app logs
a warning and returns None to the caller.
"""

import logging
import mimetypes
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _mime_to_extension(mime_type: str) -> str:
    """Derive a file extension from a MIME type, falling back to 'bin'."""
    ext = mimetypes.guess_extension(mime_type)
    if ext and ext.startswith("."):
        return ext[1:]  # strip leading dot
    # Fallback: use the subtype (e.g. "audio/webm" -> "webm")
    return mime_type.split("/")[-1]


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
    # can identify the format. E.g. "audio/ogg" -> "audio.ogg"
    ext = _mime_to_extension(mime_type)
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
            return {
                "text": json_response.get("text", ""),
                # "language" is optional in the response; default to "en" if absent
                "language": json_response.get("language", "en"),
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
