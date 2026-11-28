# The reference-audio transcript: whose requirement is it? (issue #164)

A persona with a perfectly good voice clip, a TTS engine that never asked for a
transcript, and absolute silence. Plus the sidebar badge that finally explains
it — and the one architectural rule that keeps the two facts from being
confused again.

## The symptom

Point TalkWithMe at a LuxTTS server, upload a `ref.wav` to a persona, leave the
"Reference Audio Transcript" box empty (the Persona form literally says
*optional*), and the persona never speaks. Not "speaks badly" — never issues a
single TTS request. The chat window shows the text and stays quiet; nothing is
logged, because as far as the app is concerned nothing was even attempted.

Worse, the UI argued with itself: the Persona form said "(optional)", the
`Personas/<Name>/` README tree said "(optional)", and the persona-field table
eight lines further down said "(required for TTS)".

## What the app used to do

One property answered a question that belonged to two different parties:

```python
# app/config.py — before
@property
def tts_capable(self) -> bool:
    """TTS requires both reference audio AND its transcript."""
    return bool(self.reference_audio and self.reference_audio_transcript)
```

That flag fed the sidebar's 🔇, `GET /api/personas`, and the frontend's
"should I enqueue TTS at all?" gate in `static/chat.js`. The router then
refused a second time (`400` for a non-capable persona, `503` when either file
was unreadable). Three gates, one rule, and the rule was wrong.

Meanwhile `build_synthesis_payload()` — written later, for the TTS
generification work — was already doing the right thing: it sends
`reference_text` **only if the engine advertises it**, because tts-serve's
request models are `extra="forbid"` and an unadvertised field is a hard 422.
So the payload layer knew engines differ; the permission layer above it did not.

## What the engines actually want

Every tts-serve engine publishes its answer in its `/capabilities` document, in
the `required` flag of the `reference_text` parameter (derived from the presence
of a default in the server's Pydantic model, so it cannot drift from what the
server enforces). Measured across the fleet:

| `reference_text` in the doc | Engines | Transcript genuinely needed? |
|---|---|---|
| not advertised at all | chatterbox, indexTTS, **luxTTS**, pocketTTS | **Never.** They transcribe the clip with Whisper themselves. Sending a transcript is not merely useless, it is rejected |
| advertised, `required: false` | dots.tts, OmniVoice, Qwen3-TTS, VoxCPM, KittenTTS, Ming3-TTS, audio8TTS, faster-Qwen3 | Optional — improves conditioning, engine falls back to auto-transcription or a speaker-embedding-only clone |
| advertised, `required: true` | Breeze Blue, Ming3-TTS-MoE, Qwen3-TTS-MLX | **Yes**, and the engine says so in as many words when it is missing |

Four of fifteen engines could therefore never be served by a transcriptless
persona — a quarter of the fleet silently muted by a rule that was invented
locally and never consulted the party that actually has the requirement.

## The rule: two axes, and only two places may join them

**The persona axis is files.** `Persona.tts_capable` is
`bool(self.reference_audio)`. Nothing else. A persona directory outlives
whichever engine happens to be configured in `settings.yaml`, so it must never
store or imply an engine's opinion.

**The engine axis is the capabilities document.** `tts_client.doc_requires_reference_text(doc)`
answers "does the connected engine demand a transcript?", and it returns True
**only** when the document advertises `reference_text` *and* marks it `required`.
Two polarity rules it shares with its sibling `doc_supports_reference_audio()`:

- a document that omits `reference_text` is *proof the transcript would be
  rejected*, not evidence of a requirement;
- `doc is None` means **"no evidence"**, never "known requirement". TalkWithMe
  must not be stricter than the engine it is talking to — out-ruling the engine
  is what the bug was.

Those two axes are allowed to meet in exactly two places:

| Site | What it may do | Why it is allowed |
|---|---|---|
| `app/routers/tts.py` + `app/services/tts_client.py::synthesize()` | refuse a **request** | the capabilities document is in hand at that moment |
| `personaVoiceBlock()` in `static/utils.js`, fed by `GET /api/personas` presence flags + `/api/tts/health` | draw a **badge** | display is where facts from different lifetimes may be joined, and only for this repaint |

Everything else — the persona model, the persona form, validation, persistence,
`/api/personas` — is persona-only, deliberately blind to which engine is
running. An AST guard in `tests/test_config.py`
(`test_persona_model_never_reaches_for_the_tts_capabilities_document`) fails the
build if `app/config.py` ever imports the TTS client or if `tts_capable` starts
reading `get_settings()` / `get_capabilities()`. It was written *because* the
obvious fix is to make that mistake.

### Why the requirement can't just be a form field

Tempting: read `/capabilities`, and if `reference_text.required` is true make
the Persona form's transcript box mandatory. It breaks twice over —

1. the persona is saved once and outlives every future engine switch, so the
   data would be valid only for the engine that happened to be configured at
   save time;
2. `GET /api/personas` is served by synchronous handlers, and the capabilities
   document is a lazily-cached network artifact. Marrying them makes the persona
   list a function of cache state: the 🔇 would flicker after every settings save
   (a save clears the cache slot) and go stale on every engine swap.

## The refusal: a 422 the client already knows how to handle

When the connected engine provably demands a transcript the persona does not
have, TalkWithMe refuses **locally**, before any GPU time is spent, with a
sentence that names the persona, the engine, and the fix:

```
Voice unavailable for this reply: Persona 'Alex' has no reference-audio
transcript, and the 'breeze-blue' engine requires one. Open the Persona editor
and fill in the words spoken in Alex's reference clip.
```

Both halves of that line are load-bearing:

- **The status is 422** *(and, since issue #173, the body carries
  `"refusal": "persona_files"` — see
  [`feature_tts_voice_verdict.md`](feature_tts_voice_verdict.md), which amends
  this section: the signal the client acts on is that class, not the number)*.
  This refusal is the frontend's *permanent verdict about this voice* class:
  `handleUnvoiceableVerdict()` records it against the message that asked, stops
  re-asking for the rest of that reply, and prints the reason under the bubble
  (`.tts-note`). No new UI machinery, and no once-per-sentence log spam.
  Answering 400/403/502 — or answering 422 without the class — would silently
  undo the fix: the note would never print and the client would keep asking.
- **The check runs before `POST /synthesize`.** A missing *required* field is a
  FastAPI **validation** 422 (`detail` is a list), which `_is_content_refusal()`
  deliberately does not recognise — so an un-preflighted request would trigger
  the stale-document self-heal (invalidate → refetch → rebuild → retry) *once per
  sentence of the reply*, and the user would read a serialized validation list
  where a sentence belongs.

The check lives in two places on purpose, mirroring the long-standing
non-cloning-engine pattern: the router answers from the **cached** document
(warm slot, and only when it belongs to the `base_url` being used), and
`synthesize()` repeats it against the document it just fetched — the backstop for
the cache-cold window. Both call the same pure predicate
(`reference_text_refusal()`); neither duplicates the wording or the reasoning.

## The sidebar badge (phase 2)

One badge, two reasons, and a tooltip that says which:

```js
// static/persona.js, renderPersonaList()
const voiceBlock = personaVoiceBlock(p, ttsReferenceTextRequired);
if (voiceBlock) { /* 🔇 with mute.title = "<Name> will be silent: <reason>" */ }
```

```jsonc
// GET /api/personas — presence flags, never paths
{ "name": "Bob", "tts_capable": true,
  "has_reference_audio": true, "has_reference_transcript": false }
```

```jsonc
// GET /api/tts/health — the engine-axis half
{ "enabled": true, "available": true, "streaming": false,
  "server_type": "breeze-blue", "reference_text_required": true }
```

Two implementation notes that are easy to get wrong:

**1. Health is the one route allowed to FETCH the capabilities document.** A
settings save deliberately clears the cache slot, so a cache-only read would
report `reference_text_required: false` for an engine that does demand one — and
the badge would flicker off exactly when the user switches engines and looks at
the sidebar. The synthesis path may never fetch (streaming issues one
`/synthesize` per sentence); health runs on page load and after a save, so it
fetches and warms the slot for everyone. `/health` and `/capabilities` are
independent, so they run under `asyncio.gather` — a dead TTS box costs one
timeout, not two, and both calls self-guard on `is_active` (zero network when TTS
is off).

**2. The badge must be repainted, because of `init()` ordering.**
`init()` awaits `loadPersonas()` *before* `checkTTSHealth()`, so the sidebar is
rendered before the engine answer exists. `checkTTSHealth()` therefore calls
`refreshPersonaSidebar()` — deliberately **not** `applyChatRoomFilter()`, which
would also re-POST the room's active personas to `/api/session/personas` on what
is only a repaint. Both entry points derive the visible list from
`currentRoomPersonas()` so a repaint can never show a different list than a room
switch.

A badge is a hint, never a gate: the chat path still asks, and the server still
answers with the sentence above. Pre-skipping on the frontend would restore the
original crime — silence with no explanation.

## Gotchas

- **No capabilities document** (a hand-rolled or pre-ported TTS server, not a
  tts-serve release): the rule is unknown, so TalkWithMe sends whatever the
  persona has and lets the server decide. If that server *does* require a
  transcript, its own error text appears under the reply instead of the friendly
  sentence. The sidebar shows no transcript badge in this state — an unknown
  requirement must never render as a confident 🔇.
- **Engine swapped behind the same URL without a settings save**: the cached
  document can be stale, so the local pre-flight may miss. The engine's
  validation 422 still becomes one app 422 → one note per reply. Bounded, and
  the note reads worse than the outcome.
- **A blank (`"   \n"`) `ref.txt` is no transcript.** It is dropped on load with a
  warning; it does not count as "has a transcript", and it no longer costs the
  persona its voice.
- **A non-boolean `required` in the document** (a foreign server that read
  `ParamSpec` creatively) is treated as "not required": `spec.get("required") is
  True`, strict. Malformed document fields are skipped, never guessed, exactly
  as elsewhere in the capabilities handling.
- **No clip is reported first.** A persona with a transcript and no clip gets the
  "no reference audio" reason, not "add a transcript" — the latter would be
  useless advice.
- **`synthesize()`'s `persona_name` argument** exists purely to name the persona
  in that sentence and its log line. It is never added to the payload
  (`build_synthesis_payload()` has no idea it exists, and `extra="forbid"` would
  reject it).
- **The log line is deduped** per `(base_url, persona)` by
  `warn_missing_reference_text()`, cleared by `invalidate_capabilities()` —
  streaming asks per sentence and the verdict is about the persona, not the
  sentence.
- **Renaming or cloning a persona carries `ref.wav` and `ref.txt` along** as it
  always did (directory move / copy). No new field, nothing to migrate: this
  change touched no on-disk format.

## Where it is locked in

| Suite | What it pins |
|---|---|
| `tests/test_config.py` | `tts_capable` is the clip alone (four shapes); the **AST guard** against the persona model ever importing the TTS client or reading live settings |
| `tests/test_persona_store.py` | a clip with no `ref.txt` (and one with a blank `ref.txt`) is still `tts_capable` |
| `tests/test_tts_stt_clients.py` (`TestReferenceTextRequirement`) | the predicate over optional / absent / required / `None` / malformed-flag documents; the refusal sentence; **no POST** when pre-flighted; the per-`(engine, persona)` log dedupe and its reset on engine switch |
| `tests/test_routers_tts_stt.py` | the 422 + sentence before `synthesize()` is reached; the permissive paths (optional / absent / cold cache / doc-for-another-`base_url`); the `/api/tts/health` payload as a whole dict, including the fetch-and-warm behaviour a settings save would otherwise defeat |
| `tests/test_routers_personas.py` | `has_reference_audio` / `has_reference_transcript` reported separately; the summary leaks no paths |
| `tests/test_tts_resilience.js` | a 422 from this refusal prints one `.tts-note` under the reply — in **both** streaming and non-streaming mode (before this fix, non-streaming mode's whole catch block was a `console.warn`: unexplained silence for anyone who left streaming off, which is the default) |
| `tests/test_voice_badge.js` | `personaVoiceBlock()` truth table; the badge rendering and its tooltip; the repaint after health resolves adds the badge **without** any network traffic and keeps the room filter and remove-button rule intact |

## Related

- `docs/feature_TTS_generification.md` — where the doc-driven payload and the
  capabilities cache came from (plans T3/T4/T6). This document is the story of
  one requirement that never got the same treatment.
- `docs/feature_persona_autodiscovery.md` — the persona directory layout, and why
  `reference_audio_transcript` is a per-persona file.
- `docs/feature_modal_backdrop_close.md` — another bug whose "obvious" fix was
  the wrong shape; both are locked in with guards that fail the build, not just
  the feature.
