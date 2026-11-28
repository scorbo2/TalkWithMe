# The TTS refusal class — telling a voice verdict from a bad request

*(Issue #173. Related: `feature_tts_transcript_requirement.md` — the persona/engine
two-axis rule that decides which refusals are legitimate in the first place.)*

## The problem

`POST /api/tts` answers **422** in two situations that have nothing to do with
each other:

1. **A verdict on the voice.** The engine pre-flighted the request and said this
   can never be spoken as configured ("text contains nothing this engine can
   speak…", "reference audio contains no recognizable speech…"), or TalkWithMe
   refused on the persona's behalf (no `ref.txt` while the connected engine
   demands one).
2. **A malformed request.** FastAPI validated the body against `TTSRequest`
   (`text: str = Field(..., min_length=1)`) and rejected it *before
   `tts_proxy()` ran*. No persona was looked up. No engine was contacted. No
   voice was mentioned anywhere in the exchange.

The browser only looked at the number:

```js
// the old rule — one status, two meanings
if (err && err.status === 422) handleUnvoiceableVerdict(item, err);
```

Case 2 therefore tripped the circuit breaker too. That is doubly wrong:

- **It mutes a healthy reply.** The breaker's whole justification is that it
  *predicts* — every remaining sentence of this reply will hear the same
  verdict. A rejected request body predicts nothing: the next sentence is a
  different, valid request. One bad chunk silenced sentences that would have
  spoken fine (and `enqueueStreamingTTS` then refused to even send them).
- **It lies in the bubble.** The note reads
  `Voice unavailable for this reply: [{"type":"string_too_short","loc":["body","text"],…}]`
  — a serialized validation list, quoting a verdict that was never rendered.

The backend had been telling these two apart for a long time
(`tts_client._is_content_refusal()`: a JSON object with a **string** `detail` is
a content refusal; a **list** means "the cached capabilities document may be
stale", so invalidate, refetch, retry once). The browser was re-deriving the same
difference from `typeof detail` — a second copy of the rule, in a second
language, guessing at a fact the server already had.

## The contract

Every failure body `POST /api/tts` authors is now built by one helper:

```python
# app/routers/tts.py
def _failure_body(detail: str, refusal: Optional[str]) -> dict:
    return {"detail": detail, "refusal": refusal}
```

| Field      | Audience                     | Meaning                                                     |
|------------|------------------------------|-------------------------------------------------------------|
| `detail`   | the user (the `.tts-note`)   | One sentence, verbatim from the engine when there is one.   |
| `refusal`  | the client's circuit breaker | A refusal **class**, or `null` for "this failure is not a verdict". |

Two classes exist today, and both mean "asking again will not help":

| Class             | Who decided                    | Examples                                                     |
|-------------------|--------------------------------|--------------------------------------------------------------|
| `"engine"`        | the TTS engine's own pre-flight | unvoiceable text (engine 422); a clip with no speech in its `prompt_duration` window (engine 400, forwarded as app 422) |
| `"persona_files"` | this app, before the POST       | the persona has no `ref.txt` and the connected engine requires one |

The class is derived from the same table the status code comes from, so the two
can never disagree:

```python
def _voice_verdict_refusal(result: SynthesisResult) -> Optional[str]:
    if result.outcome is SynthesisOutcome.LOCAL_REFUSAL:
        return REFUSAL_PERSONA_FILES
    if (result.outcome is SynthesisOutcome.ENGINE_VERDICT
            and result.engine_status in ENGINE_REFUSAL_STATUSES):
        return REFUSAL_ENGINE
    return None
```

**The key is always present on this router's bodies — `null` included.** A body
*without* a `refusal` key is therefore proof that the answer did not come from
the TTS router at all (FastAPI's validation handler, a reverse proxy's error
page, someone else's server entirely). The client treats absence and `null` the
same way — *not a verdict* — and only the difference tells it "and nobody here
judged this voice".

## What the browser does with it

```js
// static/tts.js — the only question the client asks about a failure
function isVoiceVerdict(err) {
    return Boolean(err && typeof err.refusal === "string" && err.refusal);
}
```

- **Verdict** → `handleUnvoiceableVerdict()`: record it against the message that
  asked, drop that message's queued sentences, print the server's sentence in a
  `.tts-note` under the bubble.
- **Not a verdict** → the remaining sentences stay queued and keep asking, no
  note is shown, and the failure is logged with `console.error`. A serialized
  validation list is *log* material; it never reaches the bubble again.

Both pipelines (streaming `processTTSRequests` and non-streaming
`processAudioQueue`) go through the same predicate.

### Seeing both 422s by hand

```bash
# 1. A real verdict: a persona with no transcript against a transcript-mandatory engine
curl -s -X POST localhost:8000/api/tts \
     -H 'Content-Type: application/json' \
     -d '{"text":"hello","persona_name":"BreezeOnly"}'
# → 422 {"detail":"Persona 'BreezeOnly' has no reference-audio transcript, and the
#        'breeze-blue' engine requires one. …","refusal":"persona_files"}

# 2. The one that is not a verdict: the request model itself rejects it
curl -s -X POST localhost:8000/api/tts \
     -H 'Content-Type: application/json' \
     -d '{"text":"","persona_name":"Luna"}'
# → 422 {"detail":[{"type":"string_too_short","loc":["body","text"], …}]}   ← no "refusal" key
```

## What is deliberately **not** labelled

- **400 — the persona has no reference clip.** Permanent, yes, but the frontend
  never asks: `chat.js` gates enqueueing on the persona summary's `tts_capable`
  flag and the sidebar badges the persona. A 400 here means a client bug or a
  hand-built probe, and labelling it would re-run issue #173 with the number
  changed.
- **503 — the TTS feature is off / the engine cannot clone.** Permanent, but
  about the app or the engine, not about *this* persona's voice — and the
  breaker is keyed per message, so it would blame a persona for a settings
  toggle.
- **502 — an engine hiccup.** Transient by definition; the next sentence is
  worth asking.

## Gotchas

- **A 422 with a *string* `detail` and no `refusal` key is still not a verdict.**
  Read the class, never the shape of `detail`. (That shape test is what the
  backend uses one hop closer to the engine, where it also has the raw response
  in hand — the browser does not.)
- **A forwarded engine *validation* failure looks like a verdict — and is
  one, but it no longer *reads* like one.** If the engine rejects our payload
  even after the stale-document self-heal rebuilt it from a freshly fetched
  capabilities document, the outcome is still `ENGINE_VERDICT` with the
  engine's 422, so `_voice_verdict_refusal()` labels it `"engine"`. That is
  intended: every remaining sentence of the reply is built from the same
  configured `tts.parameters`, so the breaker's prediction holds. What was
  fixed is the wording — the engine's body is a `{type, loc, msg, input}` list,
  which is log material, so `tts_client._payload_rejection_detail()` swaps it
  for `PAYLOAD_REJECTION_DETAIL` ("…check the TTS parameters in the Servers
  dialog") while the raw list stays in the `logger.warning` line, the only
  place the field name was ever readable. This is the **one** place the app
  authors a `detail` for a failure the engine decided; a tts-serve content
  pre-flight's own sentence is still forwarded verbatim, and so are a proxy's
  HTML page and a foreign server's envelope (we can't improve on words we
  didn't write, and inventing a cause we can't see is the worse lie).
- **Every refusal class the client knows is a mute.** `isVoiceVerdict()`
  returns a boolean because "the server labelled this a permanent verdict" and
  "stop asking for the rest of this reply" are currently the same fact — the
  class value names *who decided*, for the console and the log, and the client
  deliberately does not branch on it. The day a class means *"a verdict, but
  keep asking"* (or "mute the session, not the reply"), this is no longer a
  boolean question: add a second signal, don't quietly widen the predicate.
- **Version skew is only safe in one direction.** A stale cached `static/tts.js`
  against a new backend behaves as before #173 (over-eager, never worse). The
  reverse — new JS against an old server — would lose the note *and* the
  breaker. Frontend and backend ship from one tree and are served by one
  process, so keep them in one commit.
- **Adding a `SynthesisOutcome`?** `tests/test_routers_tts_stt.py`
  (`test_every_synthesis_outcome_declares_whether_it_is_a_voice_verdict`) fails
  and names it. Say out loud whether the new outcome is a permanent voice
  verdict; do not let it fall through to `None` by accident.
- **Renaming a class?** `tests/test_tts_resilience.js` re-reads
  `app/routers/tts.py` and compares it against the JS fixtures. Nothing else in
  the repo would notice, which is exactly why the check exists.

## Tests

- `tests/test_routers_tts_stt.py::TestTTSRefusalClasses` — the body contract:
  the #173 reproduction against the real request model, one driven request per
  failure exit of `tts_proxy()` (404 / 400 / both 503s / 422 / the shared
  verdict-and-hiccup exit), and the walk over every `SynthesisOutcome`.
- `tests/test_routers_tts_stt.py::test_every_json_response_the_tts_proxy_returns_is_a_failure_body`
  — the AST guard that makes the walk's coverage claim unnecessary to trust: it
  parses `app/routers/tts.py` and fails on **any** `JSONResponse` in `tts_proxy()`
  whose `content` was not built by `_failure_body()`, so a sixth failure exit
  added tomorrow is covered the moment it is written (the same trick
  `tests/test_yaml_io.py` plays on `open()`). Its companion
  `test_the_tts_proxy_failure_exits_all_go_through_the_helper` asserts the
  expected exit count, so a rename that makes the finder match nothing fails
  instead of going quietly green.
- `tests/test_tts_stt_clients.py` — that a shape rejection surviving the
  self-heal reaches the caller as `PAYLOAD_REJECTION_DETAIL` (with the field
  name still in the log), and that `_payload_rejection_detail()` fires for a
  validation **list** only — never for an engine sentence, an envelope, or a
  proxy page.
- `tests/test_tts_resilience.js` (plain Node, not part of pytest) — the client
  rule, the "keep asking" cases, the `isVoiceVerdict` truth table, the grep
  guards (no status *classification* in `static/tts.js` — equality, range, and
  `switch`-on-status all, with a self-check that feeds the guard each forbidden
  shape and demands it flinch; no 422 special-casing anywhere in `static/`
  except the written-down `settings.js` probe exemption; every
  `handleUnvoiceableVerdict` call site behind `isVoiceVerdict`), and the
  backend cross-check that the JS `REFUSAL_*` constants and the three body
  fixtures (verdict / router's `refusal: null` / key-absent validation) still
  match `app/routers/tts.py`.

```bash
python -m pytest tests/test_routers_tts_stt.py tests/test_tts_stt_clients.py
node tests/test_tts_resilience.js
```
