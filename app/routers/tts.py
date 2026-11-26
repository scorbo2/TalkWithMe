"""TTS router — proxy to the local TTS server.

Handles health checks, the /capabilities passthrough, and synthesis
requests. The TTS server is optional; the app degrades gracefully if it's
unavailable.

STT routing lives in its own module: app.routers.stt
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.config import clean_base_url, get_personas, get_settings
from app.models import TTSRequest, TTSHealthResponse
from app.services.tts_client import (
    cached_capabilities,
    check_tts_health,
    doc_requires_reference_text,
    doc_supports_reference_audio,
    encode_reference_audio,
    fetch_capabilities_url,
    get_capabilities,
    read_transcript,
    reference_text_refusal,
    synthesize,
    warn_missing_reference_text,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tts"])

# Engine statuses that mean "this will never be voiceable as configured":
# 422 (this text has nothing the engine can speak) and 400 (this persona's
# reference clip has no recognizable speech inside the transcribable window).
# Both are permanent verdicts about app-side configuration, so both are
# forwarded as an app 422 — the frontend's "show the engine's reason, stop
# asking for the rest of this reply" signal. Mapping the clip 400 to 502 kept
# it out of that signal path, and LuxTTS raises it *after* encode_prompt
# (Whisper ASR, inside the synthesis lock), so a client that kept asking paid
# a near-full synthesis slot per sentence, per reply, forever. The engine's
# own sentence carries the fix ("raise prompt_duration"), so the status only
# has to classify permanence, not blame.
ENGINE_REFUSAL_STATUSES = (400, 422)


def _capabilities_for_current_engine() -> Optional[dict]:
    """The cached capabilities doc — but only when it belongs to the current base_url.

    Every engine-axis question this router answers (does the engine clone from
    a reference clip at all? does it demand that clip's transcript?) can only
    be answered from the document of the engine actually configured. Two
    restrictions, both deliberate:

    - The synthesis path may never FETCH here: streaming TTS issues one
      /synthesize per sentence, and each of those must not discover a dead
      /capabilities endpoint all over again. Only the warm slot is read.
    - A document fetched for a DIFFERENT base_url says nothing about this one
      (a stale slot after an engine switch), so a mismatch answers None.

    None means "no evidence", and every check below degrades to permissive on
    it: TalkWithMe must not refuse a synthesis the engine would have answered.
    """
    cached_url, cached_doc = cached_capabilities()
    if cached_doc is None or cached_url != get_settings().tts.base_url:
        return None
    return cached_doc


@router.get("/api/tts/health", response_model=TTSHealthResponse)
async def tts_health():
    """Report TTS availability status to the frontend.

    `reference_text_required` is the engine-axis half of issue #164: it lets
    the sidebar mark a persona whose voice the CONNECTED engine cannot use (it
    demands a transcript the persona does not have) without pretending the
    persona owns that requirement. False covers "not required" and "not known"
    alike; the UI must read it as "no engine-side reason to distrust this
    persona's voice", never as a guarantee that one will speak.

    This is the ONE route where the router may FETCH the capabilities document.
    The synthesis path must never do it (streaming issues one /synthesize per
    sentence, so it reads the warm slot only), but health runs on page load and
    after a settings save — and a settings save deliberately clears the slot, so
    reading the cache here would report False for an engine that does demand a
    transcript and the mute badge would flicker off exactly when the user swaps
    engines and looks at it. The two discovery calls are independent and run
    concurrently, so a dead TTS box still costs one timeout, not two; both
    self-guard on `is_active` (no network at all when TTS is off).
    """
    settings = get_settings()
    (available, server_type), capabilities = await asyncio.gather(
        check_tts_health(), get_capabilities(),
    )
    return TTSHealthResponse(
        enabled=settings.tts.is_active,
        available=available,
        streaming=settings.tts.streaming,
        server_type=server_type,
        reference_text_required=doc_requires_reference_text(capabilities),
    )


@router.get("/api/tts/capabilities")
async def tts_capabilities(base_url: Optional[str] = Query(default=None, alias="base_url")):
    """Serve the TTS server's /capabilities document (plan T5).

    The document is the payload — no wrapper: it is self-describing and
    the frontend gates on its schema_version itself. 503 when TTS is
    inactive or the server is unreachable / lacks /capabilities (the same
    convention as the STT "inactive" response; no new error machinery).
    Serves the cache when it is warm for the current base_url; otherwise
    fetches once (get_capabilities handles the fetch and negative caching).

    Optional ?base_url=<url> probes a SPECIFIC url — the Servers modal's
    reconnect button, where the url may be an unsaved edit. A probe
    deliberately bypasses the is_active gate (the saved config may point
    elsewhere or nowhere) and never touches the cache (one-shot answer; a
    probe must not re-key the slot the synthesis path relies on). A
    scheme-less probe url is a 422 — the user can fix the field in place
    — instead of hanging until the client's connect timeout.
    """
    if base_url is not None:
        probe_url = clean_base_url(base_url)
        if not probe_url:
            return JSONResponse(
                status_code=422,
                content={"detail": "base_url must not be empty"},
            )
        if not (probe_url.startswith("http://") or probe_url.startswith("https://")):
            return JSONResponse(
                status_code=422,
                content={"detail": "base_url must start with http:// or https://"},
            )
        doc = await fetch_capabilities_url(probe_url)
        if doc is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "TTS server is unreachable or has no /capabilities endpoint"},
            )
        return doc

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

    # The persona axis: does this persona have a reference clip at all? A
    # transcript is NOT part of this question — that requirement belongs to the
    # engine and is asked of the capabilities document below (issue #164).
    if not persona.tts_capable:
        return JSONResponse(
            status_code=400,
            content={"detail": "This persona has no reference audio for voice cloning"},
        )

    # A non-cloning engine (its capabilities doc says reference_audio: null)
    # cannot serve persona TTS at all — this app's TTS is fundamentally
    # reference-audio-based, so say so before spending a request on a
    # server that cannot do the job. See _capabilities_for_current_engine() for
    # why only the warm, correctly-keyed doc is consulted.
    engine_doc = _capabilities_for_current_engine()
    if engine_doc is not None and not doc_supports_reference_audio(engine_doc):
        return JSONResponse(
            status_code=503,
            content={"detail": "The connected TTS engine does not support reference-audio voice cloning"},
        )

    # Load reference audio and transcript
    audio_b64 = encode_reference_audio(persona.reference_audio)
    transcript = read_transcript(persona.reference_audio_transcript)

    if not audio_b64:
        return JSONResponse(status_code=503, content={"detail": "TTS reference audio file is unreadable"})

    # The engine axis, answered from the document (warm cache only — synthesize()
    # repeats this check against the doc it has in hand, which is the backstop
    # for a cold slot). Answering 422 rather than 400 is load-bearing: 422 is
    # the frontend's "permanent verdict about this persona's voice" signal, which
    # prints the reason under the reply and stops re-asking for the rest of it.
    # Refusing locally also keeps a doomed request off the engine: a missing
    # required field is a FastAPI validation 422, which the client would read as
    # "stale document" and self-heal-retry once per sentence.
    refusal = reference_text_refusal(persona.name, engine_doc, transcript)
    if refusal:
        warn_missing_reference_text(get_settings().tts.base_url, persona.name)
        return JSONResponse(status_code=422, content={"detail": refusal})

    result = await synthesize(
        text=req.text,
        reference_text=transcript,
        audio_base64=audio_b64,
        language=persona.reference_audio_language,
        persona_name=persona.name,
    )

    if result.succeeded:
        return result.audio

    # Propagate the engine's verdict. Its content pre-flights answer 422 (the
    # text has nothing this engine can speak) and 400 (the persona's reference
    # clip has no recognizable speech inside the transcribable window) with a
    # detail naming what the caller can change. Collapsing every failure into
    # "returned no audio" leaves the user with silence and no lead to follow,
    # and the front end logs nothing more than a status code.
    #
    # Permanent refusals keep 422 (see ENGINE_REFUSAL_STATUSES); everything
    # else is 502: an engine hiccup, a connection failure, a 500 from an engine
    # too old to pre-flight (tts-serve < 1.5) — all worth asking about again on
    # the next sentence.
    if result.detail == "TTS is not active (disabled or no base_url)":
        return JSONResponse(status_code=503, content={"detail": result.reason()})

    status = 422 if result.status in ENGINE_REFUSAL_STATUSES else 502
    return JSONResponse(status_code=status, content={"detail": result.reason()})
