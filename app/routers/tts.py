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
    INACTIVE_DETAIL,
    SynthesisOutcome,
    SynthesisResult,
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
# forwarded as an app 422 AND both carry a `refusal` class in the body (see
# _voice_verdict_refusal). Mapping the clip 400 to 502 kept it out of that
# signal path, and LuxTTS raises it *after* encode_prompt (Whisper ASR, inside
# the synthesis lock), so a client that kept asking paid a near-full synthesis
# slot per sentence, per reply, forever. The engine's own sentence carries the
# fix ("raise prompt_duration"), so this table only has to classify
# permanence, not blame.
#
# This is also the single source of the permanence decision: _http_status_for_failure()
# and _voice_verdict_refusal() both read it, so the number a caller sees and
# the class the caller acts on can never disagree.
ENGINE_REFUSAL_STATUSES = (400, 422)

# The machine-readable half of every POST /api/tts failure body, for the one
# decision the browser makes about a synthesis: "is this persona's voice
# permanently unavailable, so should the rest of this reply stop asking?"
#
# `detail` is the sentence for the human. `refusal` is the fact for the client,
# because the HTTP status alone is NOT one: /api/tts answers 422 both for the
# verdicts below and for FastAPI rejecting the request BODY (TTSRequest's
# `text` is min_length=1) before this handler runs at all. Issue #173: the
# browser tripped its breaker on `status === 422` either way, so one
# malformed sentence muted a healthy reply and printed a serialized validation
# list under the bubble with a sentence claiming the voice was unavailable.
# The app already tells those two apart (`tts_client._is_content_refusal`);
# the browser was re-deriving the difference from `typeof detail` — a second
# copy of the rule, in a second language, guessing at a fact the server had
# all along. It now reads the fact.
#
# A refusal value names WHO decided, for the log and the console:
#   "engine"        — the engine's own pre-flight (422, or the clip 400),
#                     forwarded: this text/clip cannot be voiced as configured.
#   "persona_files" — this app's own pre-flight: the persona is missing a file
#                     the connected engine demands (ref.txt).
REFUSAL_ENGINE = "engine"
REFUSAL_PERSONA_FILES = "persona_files"

# The one place a SynthesisOutcome becomes a status code for OUR caller. The
# axis is permanence, exactly as above: 422 = "print this reason, stop asking
# for the rest of the reply", 502 = "engine hiccup, ask again on the next
# sentence", 503 = "this app is not configured to speak at all, and asking
# again changes nothing". The browser does not read that axis off the number
# any more (it reads `refusal`, which _voice_verdict_refusal derives from the
# same table); the number still carries it for logs, proxies, and curl.
#
# _http_status_for_failure() SUBSCRIPTS this table rather than calling
# .get(outcome, 502) on purpose: an outcome the table has never heard of raises
# KeyError naming itself instead of quietly inheriting the ask-me-again bucket.
# A silent default is exactly what made issue #171's prose match so cheap to
# leave in. The suite walks every enum member
# (test_every_synthesis_outcome_is_classified_by_the_router), so the KeyError
# stays where it belongs: in the commit that added the member.
_FAILURE_HTTP_STATUS = {
    SynthesisOutcome.INACTIVE: 503,
    SynthesisOutcome.LOCAL_REFUSAL: 422,
    SynthesisOutcome.LOCAL_FAILURE: 502,
}


def _http_status_for_failure(result: SynthesisResult) -> int:
    """The app's status code for a synthesis that brought back no audio.

    An ENGINE_VERDICT is classified by the engine's own number (see
    ENGINE_REFUSAL_STATUSES); every other outcome is decided by the table
    above, so a new SynthesisOutcome member has to be given a row here — it
    gets no free pass to 502.
    """
    if result.outcome is SynthesisOutcome.ENGINE_VERDICT:
        return 422 if result.engine_status in ENGINE_REFUSAL_STATUSES else 502
    return _FAILURE_HTTP_STATUS[result.outcome]


def _voice_verdict_refusal(result: SynthesisResult) -> Optional[str]:
    """The refusal class this result announces, or None when it is no verdict.

    Non-None means: "asking again will not help — this is a permanent statement
    about this persona's voice as configured", which is exactly the client's
    circuit-breaker question. Every other failure (an engine hiccup, a dead
    CUDA box, the feature switched off, this app giving up with no engine
    verdict in hand) answers None and must leave the client asking on the next
    sentence.

    Note what that makes of a 422 that SURVIVES the stale-document self-heal
    (`tts_client._self_heal_retry` returns the retry response verbatim, so it
    arrives here as an ENGINE_VERDICT with engine_status 422): it is labelled
    REFUSAL_ENGINE, not None. That is intended. The engine was asked twice,
    the second time with a payload rebuilt from a freshly fetched capabilities
    document, and refused both times — which is a permanent statement about the
    configured `tts.parameters`, and every remaining sentence of the reply is
    built from those same parameters, so the breaker's prediction holds.
    `docs/feature_tts_voice_verdict.md` records the same ruling; only its
    wording was poor, and `tts_client.PAYLOAD_REJECTION_DETAIL` fixes that.

    Deliberately derived from the same `ENGINE_REFUSAL_STATUSES` table the
    status code comes from: the number and the class are one decision read two
    ways, never two rules that can drift.
    """
    if result.outcome is SynthesisOutcome.LOCAL_REFUSAL:
        return REFUSAL_PERSONA_FILES
    if (result.outcome is SynthesisOutcome.ENGINE_VERDICT
            and result.engine_status in ENGINE_REFUSAL_STATUSES):
        return REFUSAL_ENGINE
    return None


def _failure_body(detail: str, refusal: Optional[str]) -> dict:
    """One POST /api/tts failure body: the sentence for the user, the class for the client.

    `refusal` is always present as a KEY — null included — so a body that lacks
    the key altogether is provably not this router's answer (FastAPI's request
    validation, a reverse proxy's error page, someone else's server). That
    distinction is the client's, and it is free here.
    """
    return {"detail": detail, "refusal": refusal}


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
        return JSONResponse(status_code=503, content={"detail": INACTIVE_DETAIL})
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

    Every failure body this handler produces is `{"detail": <sentence>,
    "refusal": <class or null>}`: the sentence is for the user's bubble, the
    class tells the client whether its circuit breaker applies at all. A 4xx/5xx
    body WITHOUT that key was never this handler's answer (FastAPI rejected the
    request model, a proxy answered instead) — see _failure_body().
    """
    config = get_personas()
    persona = next((p for p in config.personas if p.name == req.persona_name), None)
    if not persona:
        return JSONResponse(
            status_code=404,
            content=_failure_body(f"Persona '{req.persona_name}' not found", None),
        )

    # The persona axis: does this persona have a reference clip at all? A
    # transcript is NOT part of this question — that requirement belongs to the
    # engine and is asked of the capabilities document below (issue #164).
    #
    # Deliberately NO refusal class: this is permanent, but it is not a verdict
    # the client has to act on, because the client never asks — chat.js gates
    # enqueueing on the persona summary's `tts_capable` flag and the sidebar
    # already badges the persona (utils.js personaVoiceBlock). Labeling a 400
    # would hand the browser a breaker to trip on a request that is a client
    # bug or a hand-built probe — issue #173's mistake wearing another number.
    if not persona.tts_capable:
        return JSONResponse(
            status_code=400,
            content=_failure_body(
                "This persona has no reference audio for voice cloning", None
            ),
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
            content=_failure_body(
                "The connected TTS engine does not support reference-audio voice cloning", None
            ),
        )

    # Load reference audio and transcript
    audio_b64 = encode_reference_audio(persona.reference_audio)
    transcript = read_transcript(persona.reference_audio_transcript)

    if not audio_b64:
        return JSONResponse(
            status_code=503,
            content=_failure_body("TTS reference audio file is unreadable", None),
        )

    # The engine axis, answered from the document (warm cache only — synthesize()
    # repeats this check against the doc it has in hand, which is the backstop
    # for a cold slot). Answering 422 + REFUSAL_PERSONA_FILES is load-bearing:
    # that pair is the frontend's "permanent verdict about this persona's voice"
    # signal, which prints the reason under the reply and stops re-asking for
    # the rest of it. Refusing locally also keeps a doomed request off the
    # engine: a missing required field is a FastAPI validation 422, which the
    # app would read as "stale document" and self-heal-retry once per sentence.
    refusal = reference_text_refusal(persona.name, engine_doc, transcript)
    if refusal:
        warn_missing_reference_text(get_settings().tts.base_url, persona.name)
        return JSONResponse(
            status_code=422,
            content=_failure_body(refusal, REFUSAL_PERSONA_FILES),
        )

    result = await synthesize(
        text=req.text,
        reference_text=transcript,
        audio_base64=audio_b64,
        language=persona.reference_audio_language,
        persona_name=persona.name,
    )

    if result.succeeded:
        return result.audio

    # Propagate the verdict in the client's own words (engine pre-flights and
    # local refusals both name what the caller can change); collapsing them
    # into "returned no audio" leaves the user with silence and no lead to
    # follow. The status comes from _http_status_for_failure(), which reads
    # SynthesisResult.outcome and nothing else — no status code in this app is
    # decided by a sentence again.
    return JSONResponse(
        status_code=_http_status_for_failure(result),
        content=_failure_body(result.reason(), _voice_verdict_refusal(result)),
    )
