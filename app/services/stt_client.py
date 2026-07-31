"""STT client — talks to a local REST server's /parse endpoint.

The STT server is optional. If it's down or misconfigured, the app logs
a warning and returns None to the caller.
"""

import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


async def parse_audio(audio_base64: str) -> Optional[dict]:
    """Call the STT server's /parse endpoint.

    Returns dict with {"text": str, "language": str} or None on failure.
    """
    settings = get_settings()
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
