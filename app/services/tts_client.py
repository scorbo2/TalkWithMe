"""TTS client — talks to a local REST server's /synthesize endpoint.

The TTS server is optional. If it's down or misconfigured, the app logs
a warning and gracefully disables TTS. The frontend gets the status via
the /api/tts/health endpoint.

STT client code lives in its own module: app.services.stt_client
"""

import base64
import logging
from pathlib import Path
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def check_tts_health() -> bool:
    """Return True if the TTS server is reachable.

    Accepts both 200 (endpoint exists) and 404 (server is up but lacks /health).
    Any other outcome — connection error, timeout, 5xx — means the server is down.
    """
    settings = get_settings()
    if not settings.tts.is_active:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.tts.base_url}/health")
            return resp.status_code in (200, 404)
    except Exception:
        return False


async def synthesize(
    text: str,
    prompt_text: str,
    audio_base64: str,
    language: str = "en",
) -> Optional[dict]:
    """Call the TTS server's /synthesize endpoint.

    Returns dict with {"audio_base64": str, "sample_rate": int} or None on failure.
    """
    settings = get_settings()
    if not settings.tts.is_active:
        logger.warning("TTS synthesis skipped: feature not active (no base_url or disabled)")
        return None
    url = f"{settings.tts.base_url}/synthesize"

    payload = {
        "text": text,
        "prompt_text": prompt_text,
        "audio_base64": audio_base64,
        "language": language,
        "num_steps": settings.tts.num_steps,
        "guidance_scale": settings.tts.guidance_scale,
        "seed": settings.tts.seed,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.tts.timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("TTS synthesis failed: %s", exc)
        return None


def encode_reference_audio(audio_path: Optional[str]) -> Optional[str]:
    """Read a WAV file and return its base64-encoded contents.

    Returns None if the path is None or the file doesn't exist.
    """
    if not audio_path:
        return None
    path = Path(audio_path)
    if not path.exists():
        logger.warning("Reference audio file not found: %s", audio_path)
        return None
    return base64.b64encode(path.read_bytes()).decode("ascii")


def read_transcript(transcript_path: Optional[str]) -> Optional[str]:
    """Read the reference audio transcript file.

    Returns None if the path is None or the file doesn't exist.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.exists():
        logger.warning("Transcript file not found: %s", transcript_path)
        return None
    return path.read_text(encoding="utf-8").strip()
