# TTS Generification

This document describes a change to the way TalkWithMe interacts with TTS servers.
The goal is to move away from a "lowest common denominator" approach, and provide
discoverability of unique TTS engine features. Those unique features can then be
exposed in TalkWithMe's Servers modal in a dynamic UI.

> **Status (2026-09-04):** M0–M4.1 complete (pytest 641 green + both Node
> suites green). M5: **OmniVoice fully verified live**, including the
> zero-code-change proof; Chatterbox, Qwen3-TTS and dots.tts are pending
> (single-engine-at-a-time box) — see the runbook at the end of M5.

## Current state

There are three officially supported TTS engines right now:
- dots.tts
- OmniVoice
- Qwen3-TTS

These engines are supported by custom server scripts (in Python, using FastAPI) that
present a consistent REST API for supplying certain parameters for voice cloning
and speech generation. Each of these scripts offers a `GET /health` endpoint that
returns a fixed string identifying the underlying TTS engine type. Each server
script also offers a `POST /synthesize` endpoint that accepts certain parameters:

- `text`: the text to be generated, as a string
- `audio_base64`: reference audio for the voice clone, base64-encoded
- `prompt_text`: reference audio transcript
- `language`: reference audio language
- `seed`: the random seed to use for generation
- `num_steps`: step count
- `guidance_scale`: CFG
- `speaker_scale`
- `ode_method`

The script then returns a standardized response object that the application can parse:

- `audio_base64`: the generated audio
- `sample_rate`: Audio sample rate (Hz)
- `seed`: Seed used for this generation
- `num_steps`: Number of steps used
- `time_used`: wall-clock generation time
- `rtf`: real-time factor (time used / audio duration)

This solution works reasonably well, but it has several drawbacks:

- Unique features offered by only one TTS engine generally get ignored.
- Adding support for a new TTS engine involves copy+paste+modifying one of the existing scripts, an error-prone process.
- Not all input parameters are needed or used by all TTS engines, leading to unnecessary input fields in our UI.

## Proposed approach

A new Python package `tts-engine-common` has been developed - pure fastapi + pydantic, no torch.
Capabilities are exposed via a new `GET /capabilities` endpoint. The returned Json document 
contains engine identity, output audio facts (sample rate, watermarking), reference-audio requirements,
supported languages, and the full parameter table. TalkWithMe can use this information to dynamically
build a server settings UI specifically for those capabilities.

### Goal 1 - unique TTS engine features can be exposed

TalkWithMe can build input fields on the Servers modal based on the capabilities document returned
by whatever TTS engine is currently connected. The user can view/modify advanced settings, even
if those settings are unique to that one TTS engine.

### Goal 2 - adding support for new TTS engines becomes much easier

The copy+paste+modify dance ends forever. Adding a new TTS engine to TalkWithMe simply means
adding a new server script that uses `tts-engine-common` to expose capabilities. The common
code handles the FastAPI side of things entirely.

### Changes needed in TalkWithMe

**Front end**:
- major changes to the Servers modal. The `TTS` section must be built dynamically.

**Back end**:
- changes to TTS request flow. Request must be formed using the parameters supplied by the front end. No more one-size-fits-all requests.

## Implementation plan

Status: **M0 LOCKED (2026-09-04); M1 complete; M2 complete; M3 complete (2026-09-04); M4 complete (2026-09-04)**
Date: 2026-09-03 (plan), 2026-09-04 (M0 lock, M2–M4)

M2 note: the legacy migration (T2) is implemented as a `TTSConfig`
before-validator in `app/config.py` rather than inside `load_settings()` —
same observable behaviour, and it also covers the router-side construction
of `TTSConfig` on save. Two extra migration rules were needed for the real
world: `seed: 0` is omitted as well as `seed: null` (the old UI's "0 =
random" encoding is the same value as "absent"), and a non-dict
`tts.parameters` (hand-editing accident) is dropped with a warning instead
of crashing startup. The T7 validator (`validate_tts_parameters` in
`tts_client.py`) accepts an integer-valued float (`10.0`) for an
`integer`-typed parameter on purpose: tts-serve's pydantic models run in
lax mode and coerce it, so rejecting it here would 422 a value the server
takes. `synthesize()` folds `settings.tts.parameters` into the payload
unfiltered starting in M2 — this is what makes a migrated legacy
`settings.yaml` keep working with zero user action (T2); M3's T4 filter and
the `prompt_text` → `reference_text` rename (T11) replace the blind fold
with the doc-driven payload builder.

M3 note: live-verified on 2026-09-04 against a real OmniVoice tts-serve
instance (`/health`, `/capabilities`, `/synthesize`): the lifespan
warm-up cached the doc (`engine=omnivoice schema_version=2`),
`GET /api/tts/capabilities` served it raw, and a full `POST /api/tts`
round-trip returned 24 kHz PCM audio with the configured
`tts.parameters` (`num_steps`) accepted by the engine. The 422 self-heal
was exercised for real: with a stale dots-style doc seeded in the cache,
the engine rejected the stale `speaker_scale` field with a genuine
`extra_forbidden` 422, the cache refetched the live doc, and the retry
succeeded. The T6 language fit check only applies when a doc is
available to judge against — in the no-doc fallback the code is part of
the core vocabulary and goes out unfiltered (a first-draft bug where the
check ran with no doc and silently dropped the code, caught by tests).
Post-M3 hardening: the non-cloning check now also lives in
`synthesize()`, closing the cache-cold window — the router's 503 can only
consult a *warm* cache, so the first call after a cache invalidation
(TTS down at startup, settings save) used to fetch the doc and then let a
non-cloning engine answer in its default voice. `synthesize()` now
refuses (502 via the router) before the first POST when the doc it
already fetched says `reference_audio: null`; the shared predicate
`doc_supports_reference_audio()` backs both the router 503 and that
backstop, and the next call gets the router's clean 503.

M4 note: the Servers-modal TTS section is rendered from the live `/capabilities` document by
`static/tts-params.js` (plan T8/T9); `tests/test_tts_settings.js` (plain Node, 57 tests) locks
the T9 widget table against all four real snapshots plus the modal wiring. Two plan deviations,
both surfaced by usability testing:

- **M4.1 — reconnect (new section below).** The M4 bullet "Base URL `change` → refetch +
  re-render" could not be implemented as written: the plain `/api/tts/capabilities` serves the
  document for the SAVED base_url only, so a pre-save refetch after a URL edit would render the
  *old* engine's parameter set. M4.1 adds a `?base_url=` probe (one-shot, never touches the
  capabilities cache) plus a Refresh button, so an unsaved URL can be probed in place. The
  engine-switch comparison normalizes trailing slashes (the backend does on save via
  `clean_base_url`), so `http://x/` is not a switch away from a saved `http://x`.
- **Never-wipe pass-through.** The M4 bullet said `collectSettingsFromForm()` sends "an empty
  object when nothing is rendered". Instead, when no capabilities document is loaded the
  last-saved `tts.parameters` are passed through unchanged — sending `{}` there would silently
  wipe the user's parameters on every unrelated save (the same data-loss class the persona form
  was burned by). Usability testing also surfaced the `findTtsParamRows` wipe bug: the
  collection walk gated its descent on `Array.isArray(element.children)`, which is `false` for
  a real browser's HTMLCollection, so in production every save collected `{}` and silently
  wiped `tts.parameters` (reopening the modal showed the engine's doc defaults instead of the
  saved values). The walk now iterates `children` unconditionally, and the Node harness mimics
  the browser by making `children` array-like non-Arrays, so the regression class is caught by
  `node tests/test_tts_settings.js` (the save→reopen round-trip test reproduces the
  user-visible symptom).

M0 note: the confirmed code-only language policy (open question 3) changed
tts-serve's API surface, which bumped the capabilities `schema_version`
from 1 to 2 (see `tts-serve/docs/02-language-handling.md`). All
supported-version references in this plan were corrected v1 → v2 to match
the four live snapshots; the *shape* of the gate itself is unchanged.

The `tts-serve` repository contains a `docs/` directory with low-level details
and a full Json specification of the capabilities document, with examples.
Additionally, the old server scripts have been ported to use the new `tts-engine-common`,
and those new scripts will serve as excellent examples for implementation purposes here.

### Target protocol (recap)

What TalkWithMe will talk to (one tts-serve server per engine, e.g.
`tts-serve/impl/server_omnivoice.py`):

- `GET /health` → `{status, serverType, model, device}` (unchanged from today).
- `GET /capabilities` → the machine-readable document: `schema_version` (2 —
  bumped from 1 by the code-only language contract, see M0 note),
  `engine` (stable slug), `model`, `device`, `sample_rate`, `watermarked`,
  `endpoint`, `reference_audio` (null for non-cloning engines), `languages`
  (array or null), and `parameters[]` — one entry per request field with
  `name`, `type` (`string`/`integer`/`number`/`boolean`), `required`,
  `default`, `description`, `min`/`max`/`step`, `enum`, `min_length`/`max_length`,
  `group` (`common`/`engine`), `advanced`.
- `POST /synthesize` → JSON body with top-level fields **exactly as
  advertised**; `extra="forbid"`, so an unadvertised field is a loud
  `422` naming the field. Core vocabulary: `text`, `audio_base64`,
  `reference_text` (**renamed from the old scripts' `prompt_text`**),
  `language`, `seed`. Response: `audio_base64`, `sample_rate`, `seed`,
  `time_used`, `rtf`, plus engine extras.

Reference documents: `tts-serve/docs/01-server-generification.md` (design,
incl. §4.2 field reference and §4.3 UI rendering rules),
`tts-serve/docs/02-language-handling.md` (the code-only language contract
behind T6), `tts-serve/tts-engine-common/README.md` (package API), and the
four live snapshots in `tts-serve/impl/tests/snapshots/*_capabilities.json`
(the exact shape of the document, per engine).

### Key design decisions

| # | Decision | Rationale |
|---|----------|-----------|
| T1 | **Engine parameters are user settings, stored generically.** `TTSConfig` drops the hard-coded `num_steps` / `guidance_scale` / `seed` fields and gains `parameters: dict[str, Any]` (parameter name → value). `settings.yaml` gains `tts.parameters:`. | The parameter set is engine-declared; there is no fixed schema to model. A flat dict is exactly what the UI renders and the request builder filters. |
| T2 | **Legacy migration on load, not on save.** `load_settings()` folds `num_steps` / `guidance_scale` / `seed` from a legacy `tts:` block into `parameters` (idempotent, logged once). `save_settings()` writes the new shape, so legacy keys leave disk after the first save. | An existing `settings.yaml` must keep working with zero user action. Migrating on load (not a destructive one-time rewrite) means a failed save never corrupts the file. |
| T3 | **Capabilities cache lives in the backend** (`app/services/tts_client.py`): a single slot holding `(base_url, doc-or-None)`. Warmed at startup (lifespan), invalidated on settings save, and self-healed on a synthesis `422` (refetch + one retry). Fetch failures are **negatively cached** for the process lifetime (until invalidation). | Streaming TTS issues one `/synthesize` per sentence — a per-sentence `/capabilities` GET is unacceptable. The doc is static for the server's lifetime and carries no `Cache-Control` (per tts-serve), so the invalidation events above are the complete freshness story. |
| T4 | **The synthesis payload is built from the capabilities doc.** Always: `text`. Then, only if advertised and available: `audio_base64` (persona ref audio), `reference_text` (persona transcript), `language` (persona language, per T6), and finally every `settings.tts.parameters` entry whose name is advertised and whose value is not empty. Fields the engine doesn't advertise are **never sent** (they would 422). `text`/`audio_base64`/`reference_text`/`language` can never be supplied from `parameters` (app-managed; defense against a stale hand-edited YAML). | `extra="forbid"` makes "send only what is advertised" a hard requirement, and it makes engine switches safe by construction: switching the TTS server in the Servers modal automatically stops sending the old engine's parameters. |
| T5 | **New endpoint `GET /api/tts/capabilities`** — returns the (cached or freshly fetched) document with `200`, or `503` with a detail string when TTS is inactive or the server is unreachable/lacks `/capabilities`. No wrapper: the doc is the payload, and it is self-describing (the frontend gates on `schema_version`). | The browser cannot reach the TTS server directly (separate host/port, CORS); everything else is proxied the same way. `503` matches the existing STT "inactive" convention, so the frontend needs no new error machinery. |
| T6 | **Language pass-through policy (confirmed, Q3).** The app **never** converts language codes: it sends the persona's stored two-letter code (`en`) as-is. Engine-specific conversions (Qwen3's code→name, dots' code→`auto_detect`, …) live entirely inside the tts-serve server scripts (see `tts-serve/docs/02-language-handling.md`). Payload rule: send the persona value only when the engine advertises a `language` parameter AND (the parameter has no `enum`, OR the persona value is in the `enum`); otherwise **omit** `language` (the server defaults omitted/empty to `en`) and log a warning. | The API surface is codes-only by contract, so no mapping table is needed in the app. A code the enum rejects would 422 the whole synthesis — dropping the hint degrades gracefully instead. |
| T7 | **Settings save validates parameters against a *fresh* doc only.** `PUT /api/settings` validates `tts.parameters` (unknown name, out-of-bounds number, non-enum string → `422` naming the parameter) **only when** the cached doc belongs to the exact `base_url` being saved. If the user switches engines in the same save, validation is skipped (the doc is stale; T4 makes the switch safe anyway). | Catches garbage from the UI/API without bricking a legitimate engine switch, and keeps the save path synchronous and offline-safe (no network during save). |
| T8 | **Frontend: the TTS section of the Servers modal is built dynamically** by a new `static/tts-params.js` module with *pure* core functions (doc → widget specs, container → collected values, values → validation error) plus thin DOM builders. Widget rules per `tts-serve/docs/01-server-generification.md` §4.3, refined in T9. `advanced: true` params render inside a collapsed "Advanced" `<details>`. App-managed fields (`text`, `audio_base64`, `reference_text`, `language`) are **never rendered** as settings. | The whole point of the feature is zero per-engine UI code. Pure core functions make the renderer testable in the plain-Node `vm.Context` harness (same pattern as `tests/test_persona_form.js`), keeping pytest Python-only. |
| T9 | **Widget rules (final, testable).** `boolean` → checkbox (pre-set from `default`; always sent). `string` + `enum` → `<select>` (leading "— not set —" option when `default` is null; otherwise preselected and always sent). `string` without enum → text input (empty = not sent). `number` with `min`+`max`+`step` and a non-null `default` → range slider with live value readout (always sent). `integer` with `min`+`max`+`step` → slider. `integer` with `min`+`max`, no step → slider if the span is ≤ 100, else number input. Any other numeric shape → number input (empty = not sent). Any **unrecognized** `type` → raw-JSON escape-hatch input (user types the JSON value; invalid JSON is a validation error). Empty = "let the engine decide" is the universal meaning of a blank field — this is what makes Qwen3's default-`null` params (`temperature`, `top_p`, …) behave correctly. | Derived from §4.3 plus the actual four snapshots (e.g. Qwen3 `temperature` has bounds+step but `default: null`, so it must be a blankable number input, not a slider; `seed` is 1–1000 with no step and null default → number input, blank = random). The rules are implemented once and locked by Node tests against all four real snapshots. |
| T10 | **Version gate + disclosures.** `schema_version > 2` → minimal mode: no parameter inputs, a notice ("TTS server speaks capabilities schema vN — this app supports up to v2"), synthesis still sends `text` + reference data only. `watermarked: true` → a visible notice in the modal (Chatterbox's PerTh watermark). `sample_rate` / `model` / `engine` / `device` → an information block (replacing today's static "Server Type" field's role; the health-derived `server_type` string is kept as-is, it still works with the new servers' `/health`). | Forward-compat rule from tts-serve §3.5; the watermark notice is the responsible-AI surface from §7.5. No resampling: the browser's `decodeAudioData` handles 24 kHz and 48 kHz alike, so `sample_rate` is display-only. |
| T11 | **Old pre-ported server scripts are unsupported (hard cutover).** The new app sends `reference_text` (old scripts want `prompt_text`) and only advertised fields (old scripts expect `num_steps`/`guidance_scale` unconditionally). A user running an old script gets loud 422s from the server — logged with the full detail — and a README pointer to the tts-serve ported scripts. | Per the tts-serve Q3 answer, the old scripts are not salvageable; pretending to support both protocols would recreate the LCM swamp this feature exists to kill. |

### Milestones

Each milestone ends with a clean `python3 -m pytest` (and the Node suite where
marked). M2–M4 form one release unit (the settings request model and the form
that fills it must switch together); M2 and M3 are individually green and
non-breaking on their own — between M2 and M4 the static parameter fields in
the old UI simply have no effect (their values are ignored by the new request
model and engine defaults apply).

#### M0 — Decision lock (this document)

Status: **LOCKED** (2026-09-04) — all four open questions answered; the
answers are recorded in the **Open questions** section below, and their
consequences (code-only language policy, `schema_version` 1 → 2) are folded
into T6 / T10 / the M3–M5 bullets.

No code. Confirm the open questions below (seed-as-generic-parameter, old
script cutover, language policy, endpoint shape). Deliverable: this section.

#### M1 — Backend: capabilities client + cache (purely additive)

- `app/services/tts_client.py`:
  - `fetch_capabilities() -> Optional[dict]` — `GET {base_url}/capabilities`,
    short timeout (3 s), returns the parsed JSON body; `404`/non-2xx/connection
    error → `None` + warning. Inactive TTS → `None` without touching the network.
  - Cache slot `_capabilities_cache: Optional[dict]` + `_capabilities_base_url:
    Optional[str]`; `get_capabilities() -> Optional[dict]` (return if the slot
    matches the current `base_url` — including a cached *failure*; else fetch
    and store, negative result included); `invalidate_capabilities()`.
  - `ensure_capabilities()` — used by the lifespan: fetch + log the engine slug
    when TTS is active, never raise.
- `app/main.py` lifespan: after `load_settings()`, `await ensure_capabilities()`
  (imported at call site so tests monkeypatch it the same way as `load_tools`).
- `tests/conftest.py`: **the autouse `isolated_app_state` fixture must reset
  the new module-level cache** (AGENTS.md rule: new module global → new patch).
- `tests/factories.py`: `make_capabilities_doc(engine=...)` — faithful copies
  of the four real snapshots from `tts-serve/impl/tests/snapshots/` (plus
  helpers for a minimal doc and an unknown-field 422 body).
- Tests (`tests/test_tts_stt_clients.py`, `tests/test_main.py`): fetch success /
  404 / 5xx / connection error / inactive-without-network; cache hit for same
  base_url; refetch on base_url change; negative cache (one fetch, many calls);
  invalidation forces refetch; lifespan warms the cache and survives a fetch
  failure.

**Acceptance:** all green; `/synthesize` behaviour byte-identical to today.

#### M2 — Backend: generic TTS config + settings round-trip

- `app/config.py`:
  - `TTSConfig`: remove `num_steps` / `guidance_scale` / `seed`; add
    `parameters: Dict[str, Any] = {}` (values pass through as-is; they are
    validated at save time per T7 and by the server's own 422s — the model
    deliberately stays untyped so a hand-edited YAML never crashes startup).
  - `load_settings()`: legacy migration per T2 (legacy keys → `parameters`;
    `parameters` present wins, legacy keys warned and ignored; `seed: null` →
    key omitted, not stored as null).
- `app/models.py`: `TTSSettingsRequest` → `enabled`, `base_url`, `timeout`,
  `streaming`, `parameters: Dict[str, Any]` (the old required
  `num_steps`/`guidance_scale` and the `seed` 0→None convention are gone —
  "no value" is simply an absent key); `TTSSettingsResponse` mirrors it.
- `app/routers/settings.py`: drop the `seed == 0` normalization; T7 validation
  (unknown name / out-of-bounds / non-enum → `422` naming the parameter, only
  when the cached doc matches the saved base_url); `mcp` carry-over untouched.
  On a successful save, call `tts_client.invalidate_capabilities()` — clears
  the slot only, no inline refetch (T7 keeps the save path offline-safe; the
  next `get_capabilities()` call refetches). This is where T3's "invalidated
  on settings save" freshness event gets implemented; without it, a negative
  cache from a TTS server that was down at startup would persist for the
  process lifetime (the 422 self-heal only fires on a failing synthesis).
- Tests: `test_config.py` (every legacy-migration shape, idempotence, warning
  on conflicting sources), `test_models.py`, `test_routers_settings.py` (new
  PUT shape accepted; old-shape PUT degrades without error; 422 cases for
  unknown/out-of-bounds parameters; engine-switch skips validation; mcp
  preservation unchanged).

**Acceptance:** all green; an on-disk legacy `settings.yaml` loads with the
values migrated into `parameters`; saving rewrites it in the new shape.

#### M3 — Backend: generic synthesis + `/api/tts/capabilities`

- `app/services/tts_client.py`:
  - New `synthesize(text, reference_text, audio_base64, language)` (the
    `prompt_text` parameter is **renamed** to `reference_text` — T11's breaking
    edge). Payload built per T4 from `get_capabilities()`; no doc cached →
    core vocabulary + all configured parameters, one warning per base_url.
  - T6 language fit helper (enum-membership check only — per the confirmed
    Q3 policy the app never maps codes, so no lookup table lives here).
  - 422 self-heal: on a `422` from `/synthesize`, invalidate the cache,
    refetch, rebuild the payload, and **retry exactly once**; log the server's
    422 detail at warning either way.
- `app/routers/tts.py`:
  - call-site rename (`prompt_text=` → `reference_text=`);
  - if the cached doc says `reference_audio: null` (a non-cloning engine),
    return `503` with a clear detail before calling out (persona TTS is
    fundamentally reference-audio-based; this engine simply doesn't fit);
  - **new** `GET /api/tts/capabilities` per T5.
- `app/models.py`: delete the dead `TTSResponse` model (the router returns the
  server's raw dict on purpose — engine extras like `fid`/`num_steps` pass
  through, and the frontend only reads `audio_base64`).
- `AGENTS.md`: add the endpoint to the table (enforced by `test_docs.py`) and
  update the TTS section (capabilities flow, `parameters` dict, self-heal).
- Tests: `test_tts_stt_clients.py` (payload matrix: core fields present;
  `reference_text` sent only when advertised — Chatterbox never receives it;
  `language` fit cases: code in enum / no-enum free-form / code not in enum
  → omitted + warned; unadvertised
  parameter dropped; empty/`None` values omitted; app-managed names never
  sourced from `parameters`; 422 self-heal retries once with the refetched
  doc; no-doc fallback unfiltered + warned), `test_routers_tts_stt.py`
  (new endpoint: 200 + doc from cache, 503 when inactive, 503 when
  unreachable, cache used not re-fetched when warm; proxy tests renamed),
  `test_docs.py`.

**Acceptance:** all green; against a real tts-serve engine:
`/api/tts/capabilities` serves the doc and a full synthesis round-trip works.

#### M4 — Frontend: dynamic TTS section of the Servers modal

- **New `static/tts-params.js`** (loaded after `utils.js`, before
  `settings.js`):
  - constants: `TTS_SUPPORTED_SCHEMA_VERSION = 2`,
    `TTS_APP_MANAGED_FIELDS = ["text","audio_base64","reference_text","language"]`;
  - pure core: `selectTtsParams(doc) -> {renderable, advanced, error}`
    (filters app-managed fields; splits on `advanced`; returns an error for
    `schema_version > 2` → T10), `widgetFor(spec)` (T9 table),
    `collectTtsParamValues(container) -> object` (empty = key absent; booleans
    and defaulted selects always present), `validateTtsParamValues(values,
    doc) -> string|null` (type, bounds, enum membership — server-side
    enforcement is the backstop, this gives the user an immediate message);
  - DOM builders: `renderTtsParameters(doc, container, savedValues)`
    (label + hint from `description`/bounds as `title`/`.field-hint`,
    `advanced` params inside a collapsed `<details>`),
    `renderTtsInfo(doc, container)` (engine, model, device, sample rate,
    watermark notice per T10).
- `templates/index.html`: TTS fieldset loses the three static parameter rows
  (Step Count, CFG, Seed) and gains `<div id="sf-tts-params">` +
  `<div id="sf-tts-info">`; Base URL, Timeout, Streaming and Server Type stay.
- `static/state.js`: replace `sfTtsNumSteps`/`sfTtsGuidanceScale`/`sfTtsSeed`
  with `sfTtsParams`/`sfTtsInfo` references.
- `static/settings.js`:
  - `openSettings()` → after populating, `await refreshTtsCapabilities()`:
    TTS enabled + non-blank Base URL → `GET /api/tts/capabilities` → render
    (503 → info line "TTS server not reachable — parameter options
    unavailable"); disabled/blank → "not configured" line;
  - Base URL `change` → refetch + re-render (saved values re-applied; note in
    the info line that unsaved parameter edits are discarded on engine switch)
    — implemented via the M4.1 probe, see M4 note;
  - `collectSettingsFromForm()` → `tts.parameters = collectTtsParamValues(...)`
    (empty object when nothing is rendered — superseded by the never-wipe
    pass-through, see M4 note); `validateSettings()` → base checks
    + `validateTtsParamValues` (the hard-coded 4–20 / 1.0–2.0 ranges die).
- `static/style.css`: slider row (range + readout), `<details>` styling,
  info block — small additions to the existing stylesheet.
- **New `tests/test_tts_settings.js`** (plain Node 20+, *not* part of pytest,
  same harness pattern as `test_persona_form.js`: fresh `vm.Context`, DOM
  stub, the real `utils.js` + `tts-params.js` evaluated, the four real
  capability snapshots copied into `tests/fixtures/`): widget rule per
  parameter across all four engines (Chatterbox's 10 params, Qwen3's
  default-null numbers → blankable inputs, dots' `ode_method` select,
  OmniVoice's `denoise` checkbox); app-managed fields never rendered;
  `advanced` params hidden until the disclosure opens; collect omits empties
  and coerces types; version gate renders the notice and no inputs; escape
  hatch accepts valid JSON, rejects invalid; `validateTtsParamValues` catches
  out-of-bounds / non-enum input.
- `AGENTS.md`: frontend module table + Node-test coverage map entries.

**Acceptance:** `python3 -m pytest` all green **and**
`node tests/test_tts_settings.js` all green. Manual: open the modal against
each engine — the parameter list matches `/capabilities` exactly.

#### M4.1 — Reconnect (added 2026-09-04 after usability testing)

Not in the original plan; scope added during M4's usability testing (an
engine at an unsaved URL was only visible after save + reopen).

- **Backend:** `GET /api/tts/capabilities?base_url=<url>` probes a specific
  (possibly unsaved) url: bypasses the `is_active` gate (the saved config
  may point elsewhere or nowhere), scheme-validates the url up front (422
  for a scheme-less typo, so the user can fix the field in place instead of
  hanging on a connect timeout), fetches via
  `tts_client.fetch_capabilities_url()`, and returns 503 when unreachable
  or when the server has no `/capabilities`. A probe never reads or writes
  the single-slot capabilities cache (it must not re-key the slot the
  synthesis path relies on).
- **Frontend:** a Refresh button beside the TTS Base URL field and the
  field's `change` listener both probe the field's current value via
  `?base_url=` and re-render the parameter section in place — no save +
  reopen round trip. Only a real engine switch (url change, trailing
  slashes normalized) discards on-screen parameter edits and shows the
  "unsaved parameter edits were discarded" note; a same-URL refetch keeps
  in-flight edits. A scheme-less probe url shows the specific 422 line, not
  the generic "not reachable" line.

**Acceptance:** pytest + `node tests/test_tts_settings.js` all green,
including the probe 422/503 paths, the cache-untouched assertion, and the
same-URL (incl. trailing-slash spelling) no-clobber tests.

#### M5 — E2E verification + documentation

- The four-engine matrix (mirrors `tts-serve/docs/01-server-generification.md`
  §11): for each of Chatterbox, OmniVoice, Qwen3-TTS, dots.tts — modal shows
  exactly the advertised params; every param moved to a non-default value
  synthesizes without 422; audio sanity-checked (dots at 48 kHz included).
- **Zero-code-change proof**: add a throwaway `test_knob` parameter to one
  server script, confirm TalkWithMe renders it, persists it, and sends it —
  with zero app code touched — then revert.
- Cross-cutting checks: Qwen3 receives `language: "en"` for an `en` persona
  and maps it to `English` server-side (T6); Chatterbox receives **no**
  `reference_text`; the watermark
  notice appears for Chatterbox only; switching the Base URL between engines
  mid-session re-renders and stops sending the previous engine's params (T4).
- Docs: `README.md` (new `tts.parameters` block in the settings.yaml sample;
  TTS section pointing at the tts-serve docs instead of the old ai-playground
  scripts; engine table), `AGENTS.md` final pass, `docs/feature_TTS_generification.md`
  marked done, updated `screenshots/server_settings.png`.

**Acceptance:** matrix + zero-code-change proof pass; docs current.

#### M5 result (2026-09-04)

**OmniVoice verified live** (app on `127.0.0.1:8001` against
`impl/server_omnivoice.py`, tts-serve @ `0e82733`, cuda). Every M5 check that
is reachable with a single engine passed:

- **Legacy migration (T2):** a legacy `settings.yaml` (`num_steps` /
  `guidance_scale` / `seed` directly under `tts:`) folded into `parameters`
  at load (`seed: null` dropped — blank = engine decides) and left disk on
  the first API save.
- **Capabilities (T5):** `GET /api/tts/capabilities` returned byte-identical
  content to the tts-serve `omnivoice_capabilities.json` snapshot (and to the
  `tests/fixtures/` copy).
- **Probe (M4.1):** `?base_url=` → 200 live, 422 scheme-less
  ("base_url must start with http:// or https://"), 503 dead, 422 empty,
  200 trailing-slash.
- **Frontend render:** the real `static/tts-params.js` (vm harness, live doc)
  rendered exactly the advertised params — `seed` → number input,
  `num_steps`/`guidance_scale` → slider, `denoise` → checkbox in the collapsed
  advanced disclosure; the four app-managed fields never rendered; info block
  showed engine/model/device/sample-rate and **no** watermark notice
  (OmniVoice is un-watermarked).
- **Synthesis (T4/T6):** baseline with the migrated legacy values produced
  24 kHz audio and the engine echoed `num_steps: 14` (proof the folded legacy
  param was sent). Then every advertised param at a non-default value
  (`num_steps: 16`, `guidance_scale: 5.0`, `denoise: false`, `seed: 42`) →
  save 200, synthesis 200, all four echoed by the engine. `language: "en"`
  was sent for an `en` persona (OmniVoice's free-form language accepts codes
  verbatim).
- **T7 save-time validation (live):** unknown param → 422 "unknown TTS
  parameter 'ode_method'"; out-of-range → 422 "TTS parameter 'num_steps' must
  be <= 128.0, got 999"; wrong type → 422 "TTS parameter 'guidance_scale'
  expects a number, got str".
- **T4 drop (live):** a save that switched the base-url spelling
  (`http://localhost:8000` → `http://127.0.0.1:8000`, i.e. the engine-switch
  skip-gap) **and** carried the stale param `ode_method` in the same save →
  200 (T7 skipped by design); the next synthesis was 200 and the
  server-side request dump confirmed `ode_method` was **not** sent.

**Zero-code-change proof (live):** a throwaway `test_knob` (integer 1–100,
step 5) was added to `server_omnivoice.py` (request field, response echo,
capabilities override). With **zero** TalkWithMe changes it: appeared in
`/capabilities`; was rendered by the real frontend as a slider; was accepted
on save (T7 against the refreshed doc) and persisted to `settings.yaml`; and
was sent on synthesis (echo `test_knob: 7` + server-side request dump).
Reverted cleanly — the server script restored and the doc identical to the
snapshot again.

**Observations worth keeping:**

- **Two consecutive API saves without a capabilities refetch can bypass T7.**
  A save *invalidates* the doc cache; T7 only validates when the cached doc
  belongs to the url being saved. Saving `test_knob: 500` (out of range) right
  after another save was therefore accepted and persisted. The backstop fired
  exactly as designed: the engine 422'd ("Input should be less than or equal
  to 100"), self-heal refetched the doc and retried once, and the client got
  a 502 naming the failure. The **UI cannot hit this** — opening the modal
  always refetches the doc, so a save through the modal is validated. This is
  the same accepted-risk class as a hand-edited `settings.yaml` (see Risks);
  it is API-only exposure and the server's 422 is the backstop.
- **Environment gotcha found while taking the screenshot:** the app on 8001
  had been launched with the *global* pyenv python, not the venv (global
  fastapi 0.101 / starlette 0.27). All `/api/*` endpoints worked, but
  `GET /` returned 500 (new-style `TemplateResponse(request, name)` vs the
  legacy signature in starlette 0.27). Relaunching with
  `.venv/bin/python -m uvicorn app.main:app` fixed it, and a re-check confirmed
  identical TTS behaviour on the canonical interpreter. Launch the app with
  the venv explicitly.

**Pending (needs the other engines, see runbook below):** Chatterbox
(incl. the watermark notice, and that it receives **no** `reference_text`),
Qwen3-TTS (incl. `language: "en"` accepted for an `en` persona and mapped to
`English` server-side), dots.tts (48 kHz audio sanity), and the cross-engine
base-URL switch re-render check in the live modal.

#### M5 per-engine runbook (remaining engines)

For each of **Chatterbox**, **Qwen3-TTS**, **dots.tts** (run on the box that
has that engine installed; one engine at a time — the app's TTS config is a
single slot):

1. Start the engine's tts-serve script (e.g.
   `python impl/server_chatterbox.py`) and wait for `GET /health`.
2. In the Servers dialog set TTS enabled + base URL (e.g.
   `http://localhost:8000`) and press **Refresh** (or change the URL — the
   field's `change` listener re-probes). The parameter section must
   re-render for the new engine.
3. Compare `GET /api/tts/capabilities` against the tts-serve snapshot
   `impl/tests/snapshots/<engine>_capabilities.json` — must be identical.
4. Modal check: rendered params must match the doc exactly (slider for
   int/number ranges, checkbox for boolean, select for enum, input for
   string; advanced collapsed).
   - Chatterbox: the **watermark notice IS present**; confirm (via the
     engine's request log) that **no `reference_text`** is sent on synthesis.
   - Qwen3-TTS: `language` for an `en` persona is sent as `en` and the server
     maps it to `English`.
   - dots.tts: audio sanity-checks at **48 kHz**; the `ode_method` select
     renders.
5. Set **every** advertised param to a non-default value, save, and synthesize
   a persona sentence: expect 200, audio decodes at the doc's sample rate, and
   no 422 in the app log.
6. T7 spot-check: save one param out of range → 422 naming that param.
7. Switch the base URL to the **next** engine: the previous engine's params
   must not be sent (no 422 storm) and the new engine's section renders.
8. After the last engine, re-point at the production engine and synthesize once
   more.

The zero-code-change proof needs to be done against **one** engine only
(done for OmniVoice) — it is a property of the app, not of any single server.

### Testing strategy (summary)

| Layer | What | Where |
|-------|------|-------|
| Backend unit | capabilities fetch/cache/negative-cache/invalidation | `tests/test_tts_stt_clients.py` |
| Backend unit | legacy settings migration (all shapes) | `tests/test_config.py` |
| Backend unit | payload builder matrix + 422 self-heal + language fit | `tests/test_tts_stt_clients.py` |
| API | `/api/tts/capabilities` (200/503 paths), settings PUT with `parameters` + T7 validation | `tests/test_routers_tts_stt.py`, `tests/test_routers_settings.py` |
| Contract | AGENTS.md endpoints table matches routes | `tests/test_docs.py` |
| Frontend (Node) | widget rules × 4 real snapshots, collect/validate semantics, version gate, escape hatch, advanced disclosure | `tests/test_tts_settings.js` (plain Node, not pytest) |
| E2E (manual) | four-engine matrix, zero-code-change proof, M5 checklist | on the GPU box |

Everything hermetic per AGENTS.md: fake httpx via `tests/factories.py` (new
`make_capabilities_doc` + 422 helpers), the new module-level capabilities
cache re-pointed by the autouse conftest fixture, router stubs at the
import site. The four tts-serve capability snapshots double as the fixture
source, so the tests exercise the *real* document shape, not a paraphrase.

### Risks & edge cases

- **Old server scripts (T11):** the one true break. Loud 422s + a README
  pointer are the mitigation; there is no silent-compat mode by design.
- **Per-sentence streaming traffic:** the negative cache (T3) guarantees at
  most one `/capabilities` GET per base_url per process (plus the documented
  invalidation events) even in streaming mode.
- **Hand-edited `settings.yaml` with garbage `tts.parameters` values**
  (e.g. a string where a number belongs): not caught at load (the doc is not
  available yet in the lifespan ordering); the server's own 422 will name the
  field on the first synthesis and the self-heal keeps things consistent.
  Accepted: the UI is the primary editor and validates live (T8/T9).
  Confirmed live at M5: the same backstop also covers the API-only variant
  (two consecutive `PUT /api/settings` without a capabilities refetch in
  between — a save invalidates the doc cache, so T7 skips; the engine's 422
  + self-heal still fires, and a modal round-trip re-validates).
- **Engine switch in the same save as new parameters (T7):** validation is
  skipped on purpose; T4 drops anything the new engine doesn't advertise, so
  the worst case is unused settings keys, never a 422 storm.
- **`language` omission (T6):** for an engine whose `language` enum does not
  contain the persona's code, the hint is dropped rather than guessed (no
  mapping is ever attempted) — cloning still works, just unconditioned on
  language. Logged so it is diagnosable.
- **Non-cloning engines (`reference_audio: null`):** out of scope for the
  persona-TTS model; the app 503s with an explanatory detail rather than
  pretending to work (M3).
- **Multiple TTS server versions at once:** out of scope (tts-serve Q5
  answer); the cache is a single slot keyed by base_url.

### Open questions (to confirm at M0)

1. **Seed as a generic parameter** — `seed` stops being a first-class
   settings field; the UI's "0 = random" convention becomes "blank = random"
   (the engine picks and echoes the seed in its response). Confirmed-by-absence
   of a better option, but it changes the on-disk shape, so worth an explicit
   thumbs-up. **Answer**: Confirmed. No objections.
2. **Hard cutover (T11)** — old pre-ported server scripts are unsupported
   immediately on this branch. OK to ship that way? **Answer**: Confirmed.
   This version will be a hard cutover. The old server scripts will no longer
   work with this new version. This will be strongly noted in the release notes.
3. **Language mapping table (T6)** — ISO→English-name table built in vs.
   omit-on-mismatch only. The plan says: small built-in table (Qwen3's
   `english`-style enums are the main beneficiary). **Answer**: No, this application
   should never map 2-letter language codes into any engine-specific format.
   I've modified `tts-serve` so that the API surface now always expects
   a two-letter language code (or `auto` if the engine supplies a default).
   Engine-specific mappings (example: `en` -> `English`) are now handled
   entirely in the server implementation script. Empty/missing values
   will be implicitly converted to `en` by the server.
4. **`GET /api/tts/capabilities` shape (T5)** — raw document with `503` when
   unavailable, no wrapper object. **Answer**: confirmed.

