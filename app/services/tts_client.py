"""TTS client — talks to a local REST server's /synthesize and /capabilities
endpoints.

The TTS server is optional. If it's down or misconfigured, the app logs
a warning and gracefully disables TTS. The frontend gets the status via
the /api/tts/health endpoint.

STT client code lives in its own module: app.services.stt_client
"""

import base64
import json
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

# base_url for which the "no capabilities doc" synthesis fallback has already
# been warned. Streaming TTS issues one /synthesize per sentence, so the
# fallback warning fires once per server, not once per sentence (plan T4).
# Cleared by invalidate_capabilities(), so a changed situation (engine
# swapped behind the same base_url, server back up) can warn again.
_no_doc_fallback_warned_for: Optional[str] = None


def invalidate_capabilities() -> None:
    """Drop the cached capabilities document (or a cached fetch failure).

    Call after a settings save that may have changed the TTS base_url, or
    after a /synthesize 422 (the cached doc may be stale). Also resets the
    one-per-base_url "no doc" fallback warning (see above).
    """
    global _capabilities_cache, _capabilities_base_url, _no_doc_fallback_warned_for
    _capabilities_cache = None
    _capabilities_base_url = None
    _no_doc_fallback_warned_for = None


def cached_capabilities() -> tuple[Optional[str], Optional[dict]]:
    """Synchronous read of the cache slot: (base_url it holds, doc-or-None).

    The settings save path is a sync endpoint and must stay offline-safe
    (plan T7: no network during a save), so it reads the slot directly
    instead of awaiting get_capabilities() — which would FETCH on a miss.
    """
    return _capabilities_base_url, _capabilities_cache


def doc_supports_reference_audio(doc: Optional[dict]) -> bool:
    """Does this capabilities doc describe a cloning engine?

    `reference_audio: null` marks a non-cloning engine, which cannot
    serve this app's persona TTS (fundamentally reference-audio-based).
    A doc missing the key entirely is treated as non-cloning too: a
    malformed doc should fail loudly with a clear message rather than
    422 at the server. `None` (no doc at all) also returns False — that
    is "no evidence of support", NOT "known non-cloning"; callers that
    must distinguish the two check for a real doc before acting on this.
    """
    return doc is not None and doc.get("reference_audio") is not None


async def fetch_capabilities_url(base_url: str) -> Optional[dict]:
    """GET {base_url}/capabilities — one-shot probe of an explicit URL.

    Deliberately ignores both settings and the capabilities cache: the
    /api/tts/capabilities?base_url= probe (the Servers-modal reconnect
    button) may target a URL the user has NOT saved yet, and a probe must
    never pollute the single cache slot the synthesis path relies on.
    Returns None — with a warning, never an exception — on a non-200 status
    or a non-JSON / non-object body (a down server reads as "unreachable").
    """
    url = f"{base_url}/capabilities"
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
    return await fetch_capabilities_url(settings.tts.base_url)


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


def _numeric_bound(bound: Any) -> Optional[int | float]:
    """A spec's min/max bound, or None when absent or malformed.

    The capabilities doc comes from an external server and is only
    validated as a dict (fetch_capabilities_url); a malformed non-numeric
    bound must not TypeError the settings-save validation (a 500 on
    PUT /api/settings). Skipping it keeps the save alive and leaves the
    server's own 422 as the backstop — the same "skip what we cannot
    judge" policy as unrecognized parameter types, and the same check the
    frontend mirror (ttsParamBoundsErrors) applies. bool is excluded on
    purpose: a JSON true/false is not a bound (same stance as the integer
    type check below).
    """
    if isinstance(bound, bool) or not isinstance(bound, (int, float)):
        return None
    return bound


def _bounds_errors(name: str, value: float, spec: dict) -> List[str]:
    errors = []
    minimum = _numeric_bound(spec.get("min"))
    if minimum is not None and value < minimum:
        errors.append(f"TTS parameter {name!r} must be >= {minimum}, got {value}")
    maximum = _numeric_bound(spec.get("max"))
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


# ---------------------------------------------------------------------------
# Synthesis payload (TTS generification, plan T4 + T6)
# ---------------------------------------------------------------------------
#
# The tts-serve /synthesize request models are extra="forbid": a field the
# engine never advertised is a loud 422 naming the field. The payload is
# therefore built from the capabilities document, never from a fixed
# template — and that is what makes an engine switch safe by construction:
# switching base_url stops sending the previous engine's parameters
# automatically.

# App-managed request fields (plan T4): supplied from the request and the
# persona, never sourced from settings.tts.parameters. A stale hand-edited
# YAML must not be able to override the persona's text, voice, transcript,
# or language.
_APP_MANAGED_PARAMETER_NAMES = ("text", "audio_base64", "reference_text", "language")


def _language_value_is_accepted(spec: Optional[dict], language: str) -> bool:
    """Plan T6 (code-only language policy): will the engine accept this
    persona's two-letter code if we send it?

    No `language` parameter at all (or no code to send) → not sent. A
    parameter with an `enum` accepts only the codes it lists — a code
    outside the enum would 422 the entire synthesis. A free-form parameter
    (no enum) accepts any code. The app NEVER converts codes (confirmed Q3
    policy): a rejected code is dropped, degrading to the server's own
    default, rather than guessed or mapped.
    """
    if spec is None or not language:
        return False
    enum = spec.get("enum")
    return enum is None or language in enum


def _warn_no_capabilities_doc(base_url: str) -> None:
    """Log (once per base_url) that synthesis proceeds without a doc.

    Streaming TTS issues one /synthesize per sentence; a per-sentence
    warning would be spam. The flag is cleared by
    invalidate_capabilities(), so a server that later recovers (and fails
    again) can warn once more.
    """
    global _no_doc_fallback_warned_for
    if _no_doc_fallback_warned_for == base_url:
        return
    _no_doc_fallback_warned_for = base_url
    logger.warning(
        "TTS /capabilities unavailable for %s: sending the core vocabulary "
        "plus ALL configured tts.parameters unfiltered (a stale parameter "
        "will 422 until the self-heal refreshes the document)",
        base_url,
    )


def build_synthesis_payload(
    doc: Optional[dict],
    text: str,
    reference_text: Optional[str],
    audio_base64: Optional[str],
    language: str,
    configured_parameters: Optional[dict],
) -> dict:
    """Build the /synthesize JSON body from a capabilities doc (plan T4).

    Always: `text`. Then each app-managed field — `audio_base64`,
    `reference_text`, `language` — only if the doc advertises it (or no doc
    is available at all, in which case the caller has already logged the
    fallback warning) and a value is present. Then every configured
    `tts.parameters` entry whose name is advertised and whose value is not
    "not set" (None or empty string — an absent key is the universal
    "engine decides" signal). A field the engine doesn't advertise would
    422 under extra="forbid", so it is never sent.

    `configured_parameters` can never override the app-managed fields —
    see _APP_MANAGED_PARAMETER_NAMES.
    """
    payload: dict = {"text": text}
    specs = _advertised_parameter_specs(doc) if doc is not None else None

    def advertised(name: str) -> bool:
        # No doc: we cannot know, so send (best effort; the 422 self-heal
        # in synthesize() is the backstop).
        return specs is None or name in specs

    if audio_base64 and advertised("audio_base64"):
        payload["audio_base64"] = audio_base64
    if reference_text and advertised("reference_text"):
        payload["reference_text"] = reference_text
    if language and advertised("language"):
        if specs is None:
            # No doc: the language code is part of the core vocabulary and
            # goes out unfiltered (the caller has already warned). The T6
            # fit check needs a doc to judge against; without one there is
            # nothing to judge.
            payload["language"] = language
        elif _language_value_is_accepted(specs.get("language"), language):
            payload["language"] = language
        else:
            logger.warning(
                "TTS: persona language %r is not accepted by the engine "
                "(not in its language enum); omitting it so the server can "
                "fall back to its default. TalkWithMe never maps language "
                "codes — the code-only API contract (plan T6) forbids it.",
                language,
            )
    for name, value in (configured_parameters or {}).items():
        if name in _APP_MANAGED_PARAMETER_NAMES:
            continue  # app-managed: never sourced from tts.parameters
        if value is None or value == "":
            continue  # "not set" — let the engine decide
        if advertised(name):
            payload[name] = value
    return payload


def _response_detail(response: httpx.Response) -> str:
    """Human-readable body of an error response; a FastAPI 422 body yields
    the JSON of its `detail` (it names the offending field — the diagnostic
    that matters)."""
    try:
        body = response.json()
    except Exception:
        return response.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return json.dumps(body["detail"])
    return json.dumps(body)


async def _post_synthesis(url: str, payload: dict, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=payload)


async def synthesize(
    text: str,
    reference_text: str,
    audio_base64: str,
    language: str = "en",
) -> Optional[dict]:
    """Call the TTS server's /synthesize endpoint with a doc-driven payload.

    Returns the server's raw response dict (engine extras pass through; the
    frontend only reads `audio_base64`) or None on failure.

    The payload is built from the cached capabilities document (plan T4):
    only fields the engine advertises are sent. If no document is available
    (server without /capabilities, unreachable, ...), the core vocabulary
    plus all configured parameters are sent unfiltered, with one warning
    per base_url.

    A 422 from /synthesize triggers the self-heal (plan T3): the cached
    document may have gone stale (e.g. the engine behind base_url was
    replaced), so the cache is invalidated, the document refetched, the
    payload rebuilt, and the request retried EXACTLY ONCE. The server's 422
    detail is logged at warning in either case — it names the offending
    field.
    """
    settings = get_settings()
    if not settings.tts.is_active:
        logger.warning("TTS synthesis skipped: feature not active (no base_url or disabled)")
        return None
    base_url = settings.tts.base_url
    url = f"{base_url}/synthesize"

    def payload_for(doc: Optional[dict]) -> dict:
        return build_synthesis_payload(
            doc, text, reference_text, audio_base64, language,
            settings.tts.parameters,
        )

    try:
        doc = await get_capabilities()
        if doc is None:
            _warn_no_capabilities_doc(base_url)
        elif not doc_supports_reference_audio(doc):
            # A non-cloning engine (reference_audio: null) would accept a
            # text-only payload and answer in its DEFAULT voice — a
            # "success" with the wrong voice for the persona. Fail loudly
            # before spending the request; the doc is already in hand, so
            # this check costs no fetch. (The router 503s on the WARM
            # cache; this is the backstop for the first call after a
            # cache invalidation, when the cache is still cold.)
            logger.warning(
                "TTS engine at %s does not support reference-audio voice "
                "cloning (capabilities: reference_audio=null); persona TTS "
                "is unavailable",
                base_url,
            )
            return None
        response = await _post_synthesis(url, payload_for(doc), settings.tts.timeout)
        if response.status_code == 422:
            logger.warning(
                "TTS /synthesize rejected the payload (422) from %s: %s",
                url, _response_detail(response),
            )
            # Self-heal: the cached doc may be stale — drop it, refetch,
            # rebuild, and retry exactly once.
            invalidate_capabilities()
            doc = await get_capabilities()
            if doc is None:
                _warn_no_capabilities_doc(base_url)
            response = await _post_synthesis(url, payload_for(doc), settings.tts.timeout)
            if response.status_code == 422:
                logger.warning(
                    "TTS /synthesize rejected the payload (422) again from %s "
                    "after refetching the capabilities document: %s",
                    url, _response_detail(response),
                )
        if response.status_code != 200:
            logger.warning("TTS synthesis failed: HTTP %d from %s", response.status_code, url)
            return None
        return response.json()
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
