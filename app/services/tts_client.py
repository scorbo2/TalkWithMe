"""TTS client — talks to a local REST server's /synthesize and /capabilities
endpoints.

The TTS server is optional. If it's down or misconfigured, the app logs
a warning and gracefully disables TTS. The frontend gets the status via
the /api/tts/health endpoint.

STT client code lives in its own module: app.services.stt_client
"""

import base64
import logging
from pathlib import Path
from typing import Any, List, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# /capabilities is a discovery request on a server the user just pointed us
# at; 3 s is enough for a local box and bounds how long a dead endpoint can
# stall startup or the first synthesis.
_CAPABILITIES_TIMEOUT_S = 3.0

# ---------------------------------------------------------------------------
# Capabilities cache (TTS generification, plan T3)
# ---------------------------------------------------------------------------
#
# Single slot: (base_url it was fetched for, doc-or-None). The document is
# static for the server's lifetime and carries no Cache-Control (the tts-serve
# design answers this explicitly), so freshness is a closed set of events:
# warm at startup, invalidate on a settings save, self-heal on a 422.
# A *failure* is cached just like a document (negative cache): streaming TTS
# issues one /synthesize per sentence, and each of those must not retry a
# dead /capabilities endpoint.

_capabilities_cache: Optional[dict] = None
_capabilities_base_url: Optional[str] = None


def invalidate_capabilities() -> None:
    """Drop the cached capabilities document (or a cached fetch failure).

    Call after a settings save that may have changed the TTS base_url, or
    after a /synthesize 422 (the cached doc may be stale).
    """
    global _capabilities_cache, _capabilities_base_url
    _capabilities_cache = None
    _capabilities_base_url = None


def cached_capabilities() -> tuple[Optional[str], Optional[dict]]:
    """Synchronous read of the cache slot: (base_url it holds, doc-or-None).

    The settings save path is a sync endpoint and must stay offline-safe
    (plan T7: no network during a save), so it reads the slot directly
    instead of awaiting get_capabilities() — which would FETCH on a miss.
    """
    return _capabilities_base_url, _capabilities_cache


async def fetch_capabilities() -> Optional[dict]:
    """GET {base_url}/capabilities and return the parsed JSON document.

    Returns None — with a warning, never an exception — on a non-200 status,
    a non-JSON or non-object body, a connection error, or when TTS is
    inactive. Discovery is best-effort: a down TTS server must not break
    startup or synthesis.
    """
    settings = get_settings()
    if not settings.tts.is_active:
        return None
    url = f"{settings.tts.base_url}/capabilities"
    try:
        async with httpx.AsyncClient(timeout=_CAPABILITIES_TIMEOUT_S) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning(
                    "TTS /capabilities request failed: HTTP %d from %s",
                    resp.status_code, url,
                )
                return None
            doc = resp.json()
    except Exception as exc:
        logger.warning("TTS /capabilities fetch failed for %s: %s", url, exc)
        return None
    if not isinstance(doc, dict):
        logger.warning(
            "TTS /capabilities at %s returned a non-object JSON body; ignoring it",
            url,
        )
        return None
    return doc


async def get_capabilities() -> Optional[dict]:
    """Return the capabilities document for the *current* TTS base_url.

    Serves the cache — including a cached *failure* — when the slot was
    populated for exactly this base_url; otherwise fetches and stores the
    result. An inactive TTS returns None without touching the network and
    without disturbing the cache (there is no base_url to key it on).
    """
    global _capabilities_cache, _capabilities_base_url
    settings = get_settings()
    base_url = settings.tts.base_url if settings.tts.is_active else None
    if not base_url:
        return None
    if _capabilities_base_url == base_url:
        return _capabilities_cache
    doc = await fetch_capabilities()
    _capabilities_cache = doc
    _capabilities_base_url = base_url
    return doc


async def ensure_capabilities() -> None:
    """Lifespan hook: warm the capabilities cache and log the engine slug.

    Never raises — startup must survive a down TTS server, a server without
    /capabilities (old pre-ported scripts, unsupported per plan T11), or any
    other fetch failure.
    """
    try:
        doc = await get_capabilities()
    except Exception:
        logger.warning("TTS capabilities warm-up failed", exc_info=True)
        return
    if doc is None:
        return
    logger.info(
        "TTS capabilities cached: engine=%s schema_version=%s (from %s)",
        doc.get("engine"), doc.get("schema_version"), _capabilities_base_url,
    )


# ---------------------------------------------------------------------------
# Settings-save parameter validation (TTS generification, plan T7)
# ---------------------------------------------------------------------------
#
# PUT /api/settings validates the incoming tts.parameters against the cached
# capabilities document — but only when that document belongs to the exact
# base_url being saved (a stale doc after an engine switch would 422 a
# legitimate switch; T4 makes the switch safe anyway). These helpers are
# pure and synchronous: the save path never touches the network.

def _advertised_parameter_specs(doc: dict) -> dict:
    """Map advertised parameter name -> spec entry from a capabilities doc."""
    specs = {}
    for entry in doc.get("parameters") or []:
        if isinstance(entry, dict) and entry.get("name"):
            specs[entry["name"]] = entry
    return specs


def _wrong_type_message(name: str, expected: str, value: Any) -> str:
    return f"TTS parameter {name!r} expects {expected}, got {type(value).__name__}"


def _bounds_errors(name: str, value: float, spec: dict) -> List[str]:
    errors = []
    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None and value < minimum:
        errors.append(f"TTS parameter {name!r} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        errors.append(f"TTS parameter {name!r} must be <= {maximum}, got {value}")
    return errors


def _parameter_value_errors(name: str, value: Any, spec: dict) -> List[str]:
    param_type = spec.get("type")
    if param_type == "boolean":
        if not isinstance(value, bool):
            return [_wrong_type_message(name, "a boolean", value)]
        return []
    if param_type == "integer":
        # bool is an int subclass in Python: JSON true/false must not pass
        # as 1/0 for an integer parameter. An integer-valued float (a
        # hand-edited "num_steps: 10.0" in YAML) is accepted on purpose:
        # tts-serve's pydantic models run in lax mode and coerce it the
        # same way, so rejecting here would 422 a value the server takes.
        if isinstance(value, bool):
            return [_wrong_type_message(name, "an integer", value)]
        if isinstance(value, float) and not value.is_integer():
            return [_wrong_type_message(name, "an integer", value)]
        if not isinstance(value, (int, float)):
            return [_wrong_type_message(name, "an integer", value)]
        return _bounds_errors(name, value, spec)
    if param_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [_wrong_type_message(name, "a number", value)]
        return _bounds_errors(name, value, spec)
    if param_type == "string":
        if not isinstance(value, str):
            return [_wrong_type_message(name, "a string", value)]
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return [
                f"TTS parameter {name!r} must be one of "
                f"{', '.join(repr(v) for v in enum)}, got {value!r}"
            ]
        return []
    # Unrecognized type: skip. Like the frontend's raw-JSON escape hatch
    # (plan T9), the server's own 422 is the backstop.
    return []


def validate_tts_parameters(doc: dict, values: dict) -> Optional[str]:
    """Validate tts.parameters values against a capabilities doc (plan T7).

    Returns None when every value is acceptable, otherwise a single message
    naming every offending parameter (the router turns it into a 422).
    Checks performed: unknown parameter names, JSON-type conformance
    (boolean/integer/number/string), numeric min/max bounds, and enum
    membership for string parameters. None values are treated as "not set"
    and skipped — an absent key is the universal "engine decides" signal.
    """
    specs = _advertised_parameter_specs(doc)
    errors: List[str] = []
    for name, value in values.items():
        if value is None:
            continue
        spec = specs.get(name)
        if spec is None:
            errors.append(f"unknown TTS parameter {name!r}")
            continue
        errors.extend(_parameter_value_errors(name, value, spec))
    return "; ".join(errors) or None


async def check_tts_health() -> tuple[bool, Optional[str]]:
    """Return (is_reachable, server_type) from the TTS server's /health endpoint.

    Accepts both 200 (endpoint exists) and 404 (server is up but lacks /health).
    Any other outcome — connection error, timeout, 5xx — means the server is down.
    server_type is extracted from the optional `serverType` field in the JSON response.
    """
    settings = get_settings()
    if not settings.tts.is_active:
        return False, None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.tts.base_url}/health")
            if resp.status_code not in (200, 404):
                return False, None
            # Extract serverType if the response body is valid JSON
            server_type = None
            try:
                body = resp.json()
                server_type = body.get("serverType")
            except Exception:
                pass  # Non-JSON or empty body is fine — just no server type
            return True, server_type
    except Exception:
        return False, None


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
    }
    # Engine parameters, generically (TTS generification): whatever the user
    # configured under tts.parameters passes through as-is. This milestone
    # does NOT yet filter against the capabilities doc — plan T4 (only send
    # advertised fields) and the prompt_text -> reference_text rename (T11)
    # land with the generic payload builder in the next milestone. Sending
    # the migrated legacy values unfiltered is deliberate: it keeps
    # pre-ported server scripts behaving exactly as before.
    payload.update(settings.tts.parameters)

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
