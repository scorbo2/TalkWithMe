"""STT client — talks to an OpenAI-compatible STT server.

Sends raw audio as a multipart form POST to /v1/audio/transcriptions.
The STT server is optional. If it's down or misconfigured, the app logs
a warning and returns None to the caller.
"""

import logging
import mimetypes
from typing import Optional

import httpx

from app.config import STTLanguagePolicy, get_settings

logger = logging.getLogger(__name__)


def _mime_to_extension(mime_type: str) -> str:
    """Derive a file extension from a MIME type, falling back to 'bin'."""
    base = (mime_type or "").split(";", 1)[0].strip()
    if "/" not in base:
        return "bin"
    ext = mimetypes.guess_extension(base)
    if ext and ext.startswith("."):
        return ext[1:]  # strip leading dot
    # Fallback: use the subtype (e.g. "audio/webm" -> "webm")
    subtype = base.split("/", 1)[1]
    return subtype or "bin"


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


async def transcribe_audio(
    audio_bytes: bytes, mime_type: str = "audio/webm", language: Optional[str] = None
) -> Optional[dict]:
    """Call the STT server's /v1/audio/transcriptions endpoint.

    Sends raw audio as multipart form data with response_format=json.
    `language`, when given, is forwarded as the `language` form field,
    telling Whisper to skip auto-detection and transcribe in that language.
    Omitted (None) means "let the backend auto-detect" — the field is left
    off the request entirely, exactly as before this parameter existed.
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
    mime_type = (mime_type or "audio/webm").split(";", 1)[0].strip() or "audio/webm"
    ext = _mime_to_extension(mime_type)
    files = {
        "file": (f"audio.{ext}", audio_bytes, mime_type),
    }
    data = {
        "response_format": "json",
    }
    if language:
        data["language"] = language

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


def _select_primary_fallback_language(
    policy: STTLanguagePolicy, detected_language: str, language_probability: Optional[float]
) -> str:
    """Pick the forced language for the second pass of "primary_fallback" mode.

    The fallback language wins only on a clearly-confident detection of it;
    everything else (a different language, a weak/uncertain detection, or
    no probability reported at all) defaults to the primary language. This
    is deliberate: short language-learner utterances routinely get
    misdetected as an unrelated third language at low confidence (e.g.
    imperfect Italian heard as Portuguese or Latin), and the real question
    the policy answers is "is this clearly the fallback? if not, assume
    the primary" — not "what did Whisper guess?".
    """
    if (
        detected_language == policy.fallback_language
        and language_probability is not None
        and language_probability >= policy.fallback_threshold
    ):
        return policy.fallback_language
    return policy.primary_language


async def transcribe_with_policy(
    audio_bytes: bytes, mime_type: str, policy: STTLanguagePolicy
) -> Optional[dict]:
    """Transcribe audio_bytes according to an STTLanguagePolicy.

    - "auto": a single auto-detecting pass (today's behavior, unchanged).
    - "fixed": a single pass forced to policy.primary_language.
    - "primary_fallback": an auto-detecting first pass to see what
      language was actually spoken, then a forced second pass on the SAME
      audio bytes with either the primary or fallback language selected
      by _select_primary_fallback_language. The second pass exists
      because relabeling the first pass's text would not fix a sentence
      Whisper already decoded in the wrong language.

    Returns None if the underlying transcribe_audio call(s) fail (network
    error, inactive STT, etc.) — no special handling for a backend that
    ignores or rejects the `language` field beyond that.
    """
    if policy.mode == "auto":
        return await transcribe_audio(audio_bytes, mime_type)
    if policy.mode == "fixed":
        return await transcribe_audio(audio_bytes, mime_type, language=policy.primary_language)

    # primary_fallback
    first_pass = await transcribe_audio(audio_bytes, mime_type)
    if first_pass is None:
        return None
    selected_language = _select_primary_fallback_language(
        policy, first_pass["language"], first_pass["language_probability"]
    )
    second_pass = await transcribe_audio(audio_bytes, mime_type, language=selected_language)
    if second_pass is None:
        return None
    # The forced language is authoritative regardless of what the backend
    # echoes back for it.
    second_pass["language"] = selected_language
    return second_pass
