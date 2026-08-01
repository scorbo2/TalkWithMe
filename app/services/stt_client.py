"""STT client — talks to a local REST server's /parse endpoint.

The STT server is optional. If it's down or misconfigured, the app logs
a warning and returns None to the caller.
"""

import logging
from typing import Optional

import httpx

from app.config import get_settings

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


async def parse_audio(audio_base64: str) -> Optional[dict]:
    """Call the STT server's /parse endpoint.

    Returns dict with {"text": str, "language": str} or None on failure.
    """
    settings = get_settings()
    if not settings.stt.is_active:
        logger.warning("STT parse skipped: feature not active (no base_url or disabled)")
        return None
    url = f"{settings.stt.base_url}/parse"

    payload = {"audio_base64": audio_base64}

    try:
        async with httpx.AsyncClient(timeout=settings.stt.timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("STT parse failed: %s", exc)
        return None
