/**
 * test_tts_resilience.js — Regression tests for the streaming TTS client's
 * resilience to a tts-serve engine that refuses to speak (static/tts.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_tts_resilience.js
 *
 * Background: LuxTTS' tokenizer keeps only ASCII letters, CJK ideographs and
 * <pinyin>/[tag] parts, so a chunk of pure punctuation / digits / Markdown
 * fences / emoji — or text in a script that engine has no phonemes for — has
 * nothing to speak, and the server answers 422 with the reason (docs/02
 * language contract, schema_version 2 — the schema itself is fine). That is
 * LuxTTS' coverage, not a spec: other shipped engines advertise ru/ja/ko/ar,
 * which is why the client-side pre-filter may only test a chunk's FORM (see
 * invariant 1). A silent reference clip is a 400 there, which the
 * TalkWithMe router forwards as an app 422 too: both are permanent verdicts
 * about app-side configuration, and the client's axis is permanent vs
 * transient — which the router states in the body (`refusal`) rather than
 * leaving the browser to infer it from the number (invariant 3a below). One
 * sentence of a reply must not eat the reply's audio, and one reply's broken
 * voice must not eat the session.
 *
 * Invariants under test:
 *
 * 1. isSpeakableText tests FORM, never SCRIPT (issue #169): only a chunk with
 *    no letter at all (`\p{L}`) is skipped client-side — punctuation, digits
 *    (of any script), timestamps, Markdown fences/table rules, emoji. Any
 *    chunk carrying a letter of ANY script is sent and the ENGINE decides:
 *    accented Latin, mixed CJK/Latin, Cyrillic, Greek, kana, Hangul, Arabic
 *    and Thai all go out on the wire. The pre-filter used to hard-code
 *    LuxTTS' script coverage, which silently deleted whole-language replies
 *    with no request, no 422 and no note.
 * 2. enqueueStreamingTTS drops an unspeakable chunk WITHOUT a /api/tts request
 *    and logs the reason; a speakable chunk is queued unchanged, with the
 *    message ID stamped at enqueue time.
 * 3. A failure the SERVER labelled with a refusal CLASS trips the breaker for
 *    the message that ASKED: exactly one request goes out, that message's rest
 *    of the queue is dropped, the fetch pipeline is not left wedged, and the
 *    engine's own sentence reaches the user as one muted line anchored on
 *    that message's .bubble-content (the row is a flex row: a direct row
 *    child renders beside the bubble, not under it).
 *
 * 3a. The class, never the status code (issue #173). `/api/tts` answers 422
 *     both for those verdicts and for FastAPI rejecting the request BODY
 *     (`TTSRequest.text` is min_length=1) before the handler ran, and the two
 *     need OPPOSITE client behaviour: the verdict predicts the next sentence's
 *     verdict, the shape failure predicts nothing (the next sentence is a
 *     different, valid request). So the trip happens on `err.refusal` — a
 *     machine-readable class the router stamps on every failure body it
 *     authors — and a failure without one keeps every later sentence queued,
 *     shows no note, and is logged as the bug it probably is. Two grep guards
 *     near the end of this file keep the status code out of the decision.
 *
 *    3b. ACCEPTED CONSEQUENCE of 3, pinned rather than discovered later
 *        (issue #169): ONE refusal condemns the whole message, so a
 *        mixed-script reply on a narrow engine can lose audio the pre-#169
 *        letter table kept — a refused sentence that lands FIRST mutes its
 *        reply's remaining (perfectly voiceable) sentences, and one that
 *        lands LAST prints "voice unavailable for this reply" under a reply
 *        that mostly spoke. Both shapes are pinned below. They are not bugs
 *        to fix here: any client-side rule fine enough to separate "this
 *        language has no voice here" from "this one sentence has no letters
 *        this engine can speak" is a script table again — the #169 bug. A
 *        change that wants different behaviour must argue with those tests,
 *        not with a user report.
 * 4. A 5xx (engine hiccup) does NOT trip anything: every later sentence is
 *    still requested, and no note is shown.
 * 5. The verdict is keyed by MESSAGE ID, never by "the current reply": a TTS
 *    fetch is in flight for a whole synthesis, so a late 422 from responder A
 *    lands after responder B's "start" event. B's sentences stay queued and
 *    A's explanation never appears on B's bubble.
 * 6. fetchTTS throws with `.status` and `.detail` (verbatim engine sentence)
 *    on a non-2xx response, degrades to "HTTP <status>" on a non-JSON body,
 *    and keeps returning null for a successful response that carries no audio.
 * 7. resetTtsReplyState (the "start" event — STREAMING OR NOT, since the
 *    non-streaming queue feeds the same verdict set and nothing else ever
 *    drained it) clears the verdicts, so the next reply gets a fresh voice
 *    and the set stays bounded to one reply.
 * 8. The two engine sentences pinned below are VERBATIM from the tts-serve
 *    server that produces them, checked against the checkout when one sits
 *    beside this repo (a pre-merge copy of them pinned a wording the merged
 *    server never shipped, which is the drift this check exists to catch).
 *
 * How it works: the browser scripts share globals (no ES modules), so each
 * test evaluates them in a fresh vm.Context against a minimal DOM stub, a fake
 * AudioContext, and stubbed fetch/uploadAudio — same harness shape as
 * tests/test_speaking_highlight.js. The real processTTSRequests, fetchTTS and
 * processAudioBufferQueue are exercised; only the network is faked.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which must
 * run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_tts_resilience.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html: state, utils, tts, chat.
const APP_SCRIPTS = ["state.js", "utils.js", "tts.js", "chat.js"];

/* ==========================================================================
    The engine's own words

    Copied VERBATIM from the merged tts-serve server (1.5-dev,
    impl/server_luxTTS.py: _check_text_is_speakable / _check_reference_was_
    transcribed). They are the payload this client has to route, and the last
    test in this file re-reads them from a tts-serve checkout beside this repo
    so a server-side wording change is visible here instead of silently
    pinning a stale draft.

    The clip 400 is an f-string on the engine side; {prompt_duration:g} renders
    as "5" at the advertised default (capabilities prompt_duration: 5.0).
    ========================================================================== */

const TTS_SERVE_LUX_SERVER =
    path.join(__dirname, "..", "..", "tts-serve", "impl", "server_luxTTS.py");

const UNSPEAKABLE_TEXT_DETAIL =
    "text contains nothing this engine can speak: LuxTTS keeps only ASCII " +
    "letters, Chinese characters and <pinyin>/[tag] parts, and drops " +
    "punctuation, digits, emoji and every other script (Cyrillic, Greek, " +
    "Japanese, Korean, Arabic, ...).  Send at least one English or Chinese " +
    "word.";

const MISSING_TRANSCRIPT_DETAIL =
    "Persona 'Alex' has no reference-audio transcript, and the 'breeze-blue' " +
    "engine requires one. Open the Persona editor and fill in the words spoken " +
    "in Alex's reference clip.";

const NO_SPEECH_IN_CLIP_DETAIL =
    "reference audio contains no recognizable speech in its first 5 s: the " +
    "engine transcribes only that window with Whisper and conditions on the " +
    "transcript, so silence, music, or speech other than English / Chinese " +
    "cannot be cloned.  Start the clip with speech, or raise prompt_duration.";

/* ==========================================================================
    The refusal class (issue #173)

    Every POST /api/tts failure body from this app carries `refusal`: the
    machine-readable answer to "is this persona's voice permanently
    unavailable?", which is the ONLY question the client's circuit breaker
    answers. The HTTP status is not it — /api/tts 422s both for these verdicts
    and for a request FastAPI rejected before the handler ran. The values below
    are the backend's constants (app/routers/tts.py), re-checked against that
    file by the last test in this suite so the two languages cannot drift.
    ========================================================================== */

const REFUSAL_ENGINE = "engine";
const REFUSAL_PERSONA_FILES = "persona_files";

/** A synthesis failure body as the router sends it: sentence + refusal class. */
function verdictBody(refusal, detail) {
    return { detail, refusal };
}

const UNSPEAKABLE_TEXT_BODY = verdictBody(REFUSAL_ENGINE, UNSPEAKABLE_TEXT_DETAIL);
const NO_SPEECH_IN_CLIP_BODY = verdictBody(REFUSAL_ENGINE, NO_SPEECH_IN_CLIP_DETAIL);
const MISSING_TRANSCRIPT_BODY = verdictBody(REFUSAL_PERSONA_FILES, MISSING_TRANSCRIPT_DETAIL);

/* ==========================================================================
    DOM / browser stubs (see tests/test_speaking_highlight.js)
    ========================================================================== */

function makeFakeElement(id) {
    const classSet = new Set();
    const childSelectors = new Map();

    const el = {
        id,
        textContent: "",
        style: {},
        dataset: {},
        children: [],
        scrollTop: 0,
        scrollHeight: 0,
        addEventListener() {},
        dispatch() {},
        appendChild(child) {
            el.children.push(child);
            return child;
        },
        insertBefore(child, ref) {
            const i = el.children.indexOf(ref);
            if (i === -1) el.children.push(child);
            else el.children.splice(i, 0, child);
            return child;
        },
        // Real DOM swaps the node in place. This stub resolves querySelector
        // from a static link map, so recording the replacement is enough —
        // setupAssistantBubble uses it to trade the loading dots for a bubble.
        replaceWith(child) {
            el.replacedBy = child;
            return child;
        },
        querySelector(sel) {
            return childSelectors.get(sel) || null;
        },
        linkChildSelector(sel, child) {
            childSelectors.set(sel, child);
        },
    };

    el.classList = {
        add: (c) => classSet.add(c),
        remove: (c) => classSet.delete(c),
        contains: (c) => classSet.has(c),
        toggle(c, force) {
            const want = force === undefined ? !classSet.has(c) : !!force;
            if (want) classSet.add(c);
            else classSet.delete(c);
            return want;
        },
    };

    return el;
}

/** Fake Web Audio: decodeAudioData always succeeds, sources end immediately. */
function makeFakeAudioContext() {
    const ctx = {
        destination: {},
        sourceDelayMs: 0,
        sources: [],
        decodeAudioData: async (buffer) => ({
            fake: true,
            bytes: buffer ? buffer.byteLength : 0,
        }),
        createBufferSource() {
            const source = {
                buffer: null,
                started: false,
                onended: null,
                connect() {},
                start() {
                    this.started = true;
                    setTimeout(() => {
                        if (this.onended) this.onended();
                    }, ctx.sourceDelayMs);
                },
            };
            ctx.sources.push(source);
            return source;
        },
    };
    return ctx;
}

/** Minimal fetch Response stand-in for a successful /api/tts reply. */
function ttsSuccessResponse() {
    return {
        ok: true,
        status: 200,
        json: async () => ({
            audio_base64: Buffer.from("fake-wav-bytes").toString("base64"),
            sample_rate: 24000,
        }),
    };
}

/** A /api/tts reply with a status and a JSON body (the error surface). */
function ttsErrorResponse(status, body) {
    return {
        ok: false,
        status,
        json: async () => {
            if (body === undefined) throw new Error("not json");
            return body;
        },
    };
}

/**
 * The router's own NON-verdict failure body: a sentence, and `refusal: null`
 * saying "we answered, and this is not a verdict on the voice".
 *
 * This is what an engine hiccup actually looks like on the wire (the 502 exit —
 * pinned server-side by
 * tests/test_routers_tts_stt.py::test_engine_hiccup_502_declares_no_refusal_class,
 * which asserts the KEY IS PRESENT, not merely null-valued). It used to be
 * `{ detail }`, a body this router stopped emitting in #173: harmless to the
 * assertions (absent and null both mean "not a verdict" to the client) and
 * therefore exactly the kind of fixture that quietly describes a wire format
 * that no longer exists. The fixture cross-check at the end of this file keeps
 * the two shapes apart.
 *
 * The OTHER non-verdict is a body with no `refusal` key at all — that one is
 * `validationBody()` (FastAPI, key absent because the router never ran) or a
 * non-JSON proxy page.
 */
function detailBody(detail) {
    return { detail, refusal: null };
}

/**
 * A fake ENGINE with a narrow script coverage (LuxTTS-shaped): refuses every
 * sentence of one script with the engine's own 422 sentence, voices the rest.
 * The script test deliberately lives HERE, in the stub: emulating what one
 * engine's phonemes carry is the stub's job, while static/tts.js must stay
 * script-blind (issue #169). Used by the mixed-script pins below.
 */
function engineThatRefuses(scriptRe) {
    return (call) => (scriptRe.test(JSON.parse(call.body).text)
        ? ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY)
        : ttsSuccessResponse());
}

const CYRILLIC = /\p{Script=Cyrillic}/u;

/**
 * FastAPI's request-model validation failure — the OTHER 422 (issue #173).
 *
 * `detail` is a list of {type, loc, msg, input} entries and there is no
 * `refusal` key at all, because the body is built by FastAPI's validation
 * handler: the TTS router never ran, so no persona, engine, or voice was ever
 * consulted. Shaped after the real answer for TTSRequest's `text: min_length=1`.
 */
function validationBody(field) {
    return {
        detail: [{
            type: "string_too_short",
            loc: ["body", field],
            msg: "String should have at least 1 characters",
            input: "",
        }],
    };
}

function createHarness() {
    const elements = new Map();
    const elementById = (id) => {
        if (!elements.has(id)) elements.set(id, makeFakeElement(id));
        return elements.get(id);
    };

    const documentStub = {
        getElementById: elementById,
        createElement: (tag) => {
            const el = makeFakeElement(`created-${tag}`);
            el.tagName = tag;
            return el;
        },
        querySelector: () => null,
        querySelectorAll: () => [],
    };

    const audioCtx = makeFakeAudioContext();

    const fetchStub = async (url, options = {}) => {
        const u = String(url);
        const call = { url: u, method: (options && options.method) || "GET", body: options ? options.body : undefined };
        fetchStub.calls.push(call);
        // A route is called with its own recorded request, so a stub can answer
        // BY CONTENT ("this sentence's script is refused, that one is fine")
        // instead of counting calls. Routes that ignore the argument are
        // unaffected.
        const route = fetchStub.routes.get(u) || fetchStub.routes.get("*");
        return route ? route(call) : { ok: true, status: 200, json: async () => ({}) };
    };
    fetchStub.calls = [];
    fetchStub.routes = new Map();

    const uploadAudioCalls = [];
    let highlightCalls = 0;
    // The console is recorded, not forwarded: the resilience paths announce
    // themselves there (a dropped chunk, a failure that is NOT a voice verdict),
    // and "the user saw nothing but the log says why" is an assertion worth
    // making. Forwarding to the real console would just noise up the run.
    const logs = [];
    const record = (level) => (...args) => {
        logs.push({ level, text: args.map((a) => String(a)).join(" ") });
    };
    const sandbox = {
        console: {
            log: record("log"),
            info: record("info"),
            warn: record("warn"),
            error: record("error"),
            debug: record("debug"),
        },
        document: documentStub,
        fetch: fetchStub,
        atob: (s) => Buffer.from(s, "base64").toString("latin1"),
        requestAnimationFrame: () => 0,
        setTimeout,
        clearTimeout,
        // state.js declares `let audioCtx = null`; the lazy init in fetchTTS
        // reads window.AudioContext, which hands back this fake.
        window: {
            AudioContext: function FakeAudioContext() {
                return audioCtx;
            },
        },
        // Defined in persistence.js (not loaded here).
        uploadAudio: async (room, messageId, audioBase64, mimeType) => {
            uploadAudioCalls.push({ room, messageId, audioBase64, mimeType });
            return { filename: `${String(messageId).slice(0, 8)}_0.wav` };
        },
        getAudioUrl: (room, filename) => `/api/persist/audio/${room}/${filename}`,
        // Defined in persona.js (not loaded here); the "start" handler calls it
        // when the reply's persona differs from the sidebar selection.
        highlightSelectedPersona: () => {
            highlightCalls += 1;
        },
    };

    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, file), "utf8"), sandbox, {
            filename: file,
        });
    }

    // The streaming path calls beginSpeaking/endSpeaking (chat.js) against a
    // row in #messages; point the row globals at a stub row so the real
    // pipeline runs unguarded.
    const row = makeFakeElement("row-current");
    row.dataset.messageId = "M1";
    const content = makeFakeElement("content-current");
    row.linkChildSelector(".bubble-content", content);
    // setupAssistantBubble (called by the real "start" handler) reads the
    // avatar and name children of the row.
    row.linkChildSelector(".bubble-avatar", makeFakeElement("avatar-current"));
    row.linkChildSelector(".bubble-name", makeFakeElement("name-current"));
    row.linkChildSelector(".loading-bubble", makeFakeElement("loading-current"));
    const messagesEl = elementById("messages");
    messagesEl.linkChildSelector('.message-row[data-message-id="M1"]', row);
    messagesEl.linkChildSelector('.message-row.assistant[data-message-id="M1"]', row);

    /** Register another live reply row, as a second responder's would be. */
    function addRow(messageId) {
        const extra = makeFakeElement(`row-${messageId}`);
        extra.dataset.messageId = messageId;
        const extraContent = makeFakeElement(`content-${messageId}`);
        extra.linkChildSelector(".bubble-content", extraContent);
        extra.linkChildSelector(".bubble-avatar", makeFakeElement(`avatar-${messageId}`));
        extra.linkChildSelector(".bubble-name", makeFakeElement(`name-${messageId}`));
        extra.linkChildSelector(".loading-bubble", makeFakeElement(`loading-${messageId}`));
        messagesEl.linkChildSelector(`.message-row[data-message-id="${messageId}"]`, extra);
        messagesEl.appendChild(extra);
        return { row: extra, content: extraContent };
    }

    /** The note lines appended to one row's .bubble-content. */
    function notesIn(contentEl) {
        return contentEl.children.filter((c) => c.className === "tts-note");
    }

    // state.js declares these as `let` globals, which are NOT properties of
    // the vm context object — they can only be assigned from inside the
    // context. Hand the row in through a plain property first.
    sandbox.__rowStub = row;
    vm.runInContext(
        'currentAssistantRow = __rowStub; currentAssistantMessageId = "M1";',
        sandbox,
        { filename: "harness-setup.js" },
    );

    return {
        sandbox,
        row,
        content,
        elementById,
        messagesEl,
        audioCtx,
        fetchStub,
        uploadAudioCalls,
        /** Everything the scripts logged: {level, text}. */
        logs: () => logs,
        /** The logged lines at one level, joined for substring assertions. */
        loggedText: (level) => logs.filter((l) => l.level === level).map((l) => l.text).join("\n"),
        highlightCalls: () => highlightCalls,
        addRow,
        notesIn,
        /** Requests the streaming pipeline sent to /api/tts. */
        ttsCalls: () => fetchStub.calls.filter((c) => c.url === "/api/tts"),
        /** The note line the client appended under the current reply, if any. */
        notes: () => notesIn(content),
        /** True when the engine has refused this message's voice. */
        refused: (messageId) => vm.runInContext(
            `(() => ttsBrokenMessageIds.has(${JSON.stringify(messageId)}))()`, sandbox,
        ),
        get(expression) {
            return vm.runInContext(`(() => (${expression}))()`, sandbox);
        },
        run(statement) {
            return vm.runInContext(statement, sandbox);
        },
    };
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Poll until fn() is truthy (the queue re-entry runs on real timers). */
async function waitUntil(fn, timeoutMs = 2000, stepMs = 5) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if (fn()) return;
        if (Date.now() > deadline) throw new Error("waitUntil timed out");
        await sleep(stepMs);
    }
}

/** Let the fetch pipeline settle: queue drained, nothing in flight. */
async function pipelineIdle(h) {
    await waitUntil(
        () => h.get("ttsRequestQueue.length") === 0 && h.get("isFetchingTTS") === false,
    );
    // One extra tick for the trailing setTimeout(() => processTTSRequests(), 0).
    await sleep(10);
}

/* ==========================================================================
    The client-side pre-filter
    ========================================================================== */

test("isSpeakableText_withVoiceableCharacters_returnsTrue", () => {
    const h = createHarness();
    const voiceable = [
        "Hello there.",
        "Sure — here is the answer: 42.",
        "café über straße", // accents: letters are letters
        "你好，世界",
        "Sure, 这里 is mixed 中文 and English.",
        "Mix of 中文 and english, 123 numbers",
        // Issue #169: these were the silent-language cases. Whatever the
        // connected engine makes of them, it is the one that gets to say so —
        // every script below but Thai is advertised by a shipped tts-serve
        // engine (qwen3TTS ru/ja/ko, indexTTS ar, ming3TTS ja/ko).
        "Привет мир", // ru — server_qwen3TTS
        "こんにちは", // ja — server_qwen3TTS, server_indexTTS, server_ming3TTS
        "한국어", // ko — server_qwen3TTS, server_ming3TTS
        "مرحبا بالعالم", // ar — server_indexTTS
        "Ελληνικά", // the Greek LuxTTS drops but no client rule may
        "สวัสดี", // th — no shipped engine, still the engine's call
    ];

    // GIVEN chunks that carry at least one letter of any script,
    // WHEN the pre-filter classifies each, THEN none is skipped.
    for (const text of voiceable) {
        assert.equal(h.get(`isSpeakableText(${JSON.stringify(text)})`), true, `expected voiceable: ${text}`);
    }
});

test("isSpeakableText_withNoLetters_returnsFalse", () => {
    const h = createHarness();
    const unvoiceable = [
        "",
        "   \n  ",
        "...",
        "1234",
        "```", // closing code fence: the splitter emits it as its own chunk
        "| --- | --- |",
        "2026-11-10 08:30",
        "→ ❤ 😀",
        "!!! ???",
        // Form, not script: these are non-ASCII and non-Latin and still carry
        // nothing a TTS engine can voice. A rule that let them through "out of
        // script-fairness" would be back to paying for the fence.
        "١٢٣٤", // Arabic-Indic digits — Nd, not Lo
        "½¼", // symbols
        "※・", // CJK symbols/punctuation, no ideographs
        "«» ― …", // punctuation only
    ];

    // GIVEN chunks with no letter anywhere in them,
    // WHEN the pre-filter classifies each, THEN all are skipped.
    for (const text of unvoiceable) {
        assert.equal(h.get(`isSpeakableText(${JSON.stringify(text)})`), false, `expected unvoiceable: ${text}`);
    }
});

test("enqueueStreamingTTS_withUnvoiceableChunks_queuesNothingAndSendsNoRequest", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", ttsSuccessResponse);

    // GIVEN a streamed reply that closes with a fence, a table rule, a bare
    // ellipsis and a timestamp (all their own chunks after the splitter),
    h.run(`
        enqueueStreamingTTS("aria", "\`\`\`");
        enqueueStreamingTTS("aria", "| --- | --- |");
        enqueueStreamingTTS("aria", "...");
        enqueueStreamingTTS("aria", "2026-11-10 08:30");
    `);
    await sleep(20);

    // THEN no chunk is queued, no /api/tts request is made, and the reason is logged.
    assert.equal(h.get("ttsRequestQueue.length"), 0, "unvoiceable chunks must not be queued");
    assert.equal(h.ttsCalls().length, 0, "no request may be made for them");
    assert.equal(h.refused("M1"), false, "skipping is not a voice verdict");
    assert.equal(h.notes().length, 0, "no user-facing note for a skipped chunk");
});

test("enqueueStreamingTTS_withANonLatinReply_asksTheEngineAndShowsItsRefusal", async () => {
    const h = createHarness();
    // The client is not the authority on scripts (issue #169). A Russian reply
    // on LuxTTS — the one engine that pre-flights its own coverage — must
    // reach the server and come back as the engine's 422 sentence, which the
    // verdict path puts under the bubble. Before the fix it died in a
    // console.warn nobody opens: the resilience machinery never saw it.
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));

    h.run(`
        enqueueStreamingTTS("boris", "Привет мир.");
        enqueueStreamingTTS("boris", "Как дела?");
    `);
    await pipelineIdle(h);

    // THEN the first sentence went out (the engine answered, not the client),
    assert.equal(h.ttsCalls().length, 1, "a non-Latin sentence must reach the engine");
    assert.equal(JSON.parse(h.ttsCalls()[0].body).text, "Привет мир.");
    // AND the engine's verdict is recorded against this message, so the rest
    // of the reply is not re-paid for,
    assert.equal(h.refused("M1"), true, "the engine's verdict is recorded");
    assert.equal(h.get("ttsRequestQueue.length"), 0, "the rest of the reply is dropped");
    // AND the reason reaches the user.
    const notes = h.notes();
    assert.equal(notes.length, 1, "exactly one note line");
    assert.ok(notes[0].textContent.includes(UNSPEAKABLE_TEXT_DETAIL), notes[0].textContent);
});

test("processTTSRequests_onARefusedFirstSentenceOfAMixedReply_mutesTheRestOfThatReply", async () => {
    // PINS AN ACCEPTED TRADE-OFF (see invariant 3b), not a desired behaviour.
    // A mixed-script reply on LuxTTS: a Russian greeting, then two English
    // sentences the engine voices happily. The greeting is refused, the
    // verdict condemns the message, and the English sentences never get asked
    // — audio the pre-#169 client DID play, because it dropped the Cyrillic
    // line itself and never told the pipeline. #169 chose "one engine verdict
    // per reply, explained" over "a client-side script table that decides
    // which languages deserve a voice", and this test is the price tag.
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", engineThatRefuses(CYRILLIC));

    h.run(`
        enqueueStreamingTTS("boris", "Привет мир.");
        enqueueStreamingTTS("boris", "Here is the answer.");
        enqueueStreamingTTS("boris", "And here is the second one.");
    `);
    await pipelineIdle(h);

    // THEN only the refused sentence was ever requested — the reply's
    // voiceable half is muted by its voiceless one,
    assert.equal(h.ttsCalls().length, 1, "the refused sentence is the only request the reply ever makes");
    assert.equal(JSON.parse(h.ttsCalls()[0].body).text, "Привет мир.");
    assert.equal(h.refused("M1"), true, "one refusal condemns the message");
    assert.equal(h.get("ttsRequestQueue.length"), 0, "the voiceable sentences are dropped, not deferred");
    assert.equal(h.audioCtx.sources.length, 0, "and nothing plays");
    // AND the user is told, which is what was bought for that audio.
    assert.equal(h.notes().length, 1, "one note line");
});

test("processTTSRequests_onAMixedReplyWithTheRefusalLast_spokeTheRestAndStillPrintedOneNote", async () => {
    // THE OTHER HALF of the accepted trade-off (invariant 3b): the refusal
    // lands on the reply's closing line, after the voiceable sentences have
    // already been synthesized and played. The reply speaks almost entirely —
    // and still carries a "Voice unavailable for this reply" line, which
    // overstates it. Accepted: the alternative is the client deciding, per
    // sentence, which scripts the engine can carry.
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", engineThatRefuses(CYRILLIC));

    h.run(`
        enqueueStreamingTTS("boris", "Here is the answer.");
        enqueueStreamingTTS("boris", "And here is the second one.");
        enqueueStreamingTTS("boris", "Пока.");
    `);
    await pipelineIdle(h);
    await waitUntil(() => h.get("audioBufferQueue.length") === 0 && h.get("isPlayingAudioBuffer") === false);

    // THEN every sentence of the reply was asked (the verdict arrived too late
    // to spare anything) and the two voiceable ones played,
    assert.equal(h.ttsCalls().length, 3, "three asks: the verdict came last");
    assert.ok(h.audioCtx.sources.length >= 2, "the voiceable sentences were played");
    // AND the note still lands under a reply that mostly spoke — the wording
    // this shape is known to produce, pinned so a reword is a decision.
    const notes = h.notes();
    assert.equal(notes.length, 1, "exactly one note line");
    assert.ok(notes[0].textContent.includes("Voice unavailable for this reply"), notes[0].textContent);
    assert.equal(h.refused("M1"), true);
});

test("enqueueStreamingTTS_withAVoiceableChunk_queuesItWithTheStampedMessageId", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", ttsSuccessResponse);

    h.run('enqueueStreamingTTS("aria", "Here are the numbers: 1234.")');

    // GIVEN a chunk containing voiceable text (even beside a wall of digits),
    // THEN it is queued with the message ID stamped at enqueue time.
    await waitUntil(() => h.ttsCalls().length === 1);
    assert.equal(JSON.parse(h.ttsCalls()[0].body).text, "Here are the numbers: 1234.");
    assert.equal(JSON.parse(h.ttsCalls()[0].body).persona_name, "aria");
    await pipelineIdle(h);
});

/* ==========================================================================
    The per-message circuit breaker
    ========================================================================== */

test("processTTSRequests_onEngineContent422_stopsTheReplyAndShowsTheEngineSentence", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));

    h.run(`
        enqueueStreamingTTS("aria", "First speakable sentence.");
        enqueueStreamingTTS("aria", "Second speakable sentence.");
        enqueueStreamingTTS("aria", "Third speakable sentence.");
    `);
    await pipelineIdle(h);

    // GIVEN three queued sentences and an engine that rejects the voice,
    // THEN exactly one request went out and the rest of the reply is dropped.
    assert.equal(h.ttsCalls().length, 1, "the later sentences must not be requested");
    assert.equal(h.refused("M1"), true, "the verdict must be recorded for this message");
    assert.equal(h.get("ttsRequestQueue.length"), 0, "the rest of the reply is dropped");
    assert.equal(h.get("isFetchingTTS"), false, "the fetch pipeline must not be left wedged");

    // AND the engine's own sentence reaches the user, under the reply: inside
    // the row's .bubble-content column, because .message-row is a flex row
    // (avatar | content) and a direct row child renders beside the bubble.
    const notes = h.notes();
    assert.equal(notes.length, 1, "exactly one note line");
    assert.ok(notes[0].textContent.includes(UNSPEAKABLE_TEXT_DETAIL), notes[0].textContent);
    assert.equal(h.row.children.filter((c) => c.className === "tts-note").length, 0,
        "the note must not be appended to the flex row itself");
});

test("noteTtsUnavailableForReply_withAnActionRow_insertsTheNoteAboveTheButtons", async () => {
    // GIVEN a reply row whose copy/delete action row exists (both the action
    // wrapper and the audio container are direct children of .bubble-content,
    // which is exactly what insertBefore requires),
    const h = createHarness();
    const actions = makeFakeElement("actions-current");
    actions.className = "message-actions";
    h.content.appendChild(actions);
    h.content.linkChildSelector(".message-actions", actions);

    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));
    h.run('enqueueStreamingTTS("aria", "A speakable sentence.")');
    await pipelineIdle(h);

    // WHEN the refusal note is appended, THEN it lands under the bubble text
    // and above the buttons, not after them.
    const classNames = h.content.children.map((c) => c.className);
    assert.ok(
        classNames.includes("tts-note") && classNames.indexOf("tts-note") < classNames.indexOf("message-actions"),
        `the note must precede the action row, got: ${classNames.join(", ")}`,
    );
});

test("processTTSRequests_onAnUnlabelled422FromSomethingElse_keepsAskingAndAccusesNobody", async () => {
    // A 422 whose body is not even JSON (a reverse proxy's error page, an empty
    // gateway answer). The TTS router stamps EVERY failure body it authors with
    // a refusal class, so a body without one is not this app judging this
    // persona's voice — and it used to trip the breaker anyway and print
    // "Voice unavailable for this reply: HTTP 422", an accusation with no
    // evidence behind it (issue #173).
    const h = createHarness();
    let answered = 0;
    h.fetchStub.routes.set("/api/tts", () => {
        answered += 1;
        return ttsErrorResponse(422, undefined);
    });

    h.run(`
        enqueueStreamingTTS("aria", "First speakable sentence.");
        enqueueStreamingTTS("aria", "Second speakable sentence.");
    `);
    await pipelineIdle(h);

    // GIVEN two sentences and an answer nobody authored,
    // THEN the second sentence — a different request — is still asked,
    assert.equal(answered, 2, "an unjudged failure must not mute the rest of the reply");
    // ...no verdict is recorded, no line blames the voice,...
    assert.equal(h.refused("M1"), false, "no refusal class, no verdict");
    assert.equal(h.notes().length, 0, "no note about a voice that was never judged");
    // ...and the failure is not swallowed either: it is on the console.
    assert.ok(h.loggedText("error").includes("HTTP 422"),
        `the unlabelled failure must be logged: ${h.loggedText("error")}`);
});

test("isVoiceVerdict_onlyAPresentRefusalClassIsAVerdict", () => {
    const h = createHarness();
    const cases = [
        // The two classes the backend can send (re-checked against its file
        // below) — both are verdicts, whatever the number that carried them.
        [{ refusal: REFUSAL_ENGINE }, true],
        [{ refusal: REFUSAL_PERSONA_FILES }, true],
        // The router's own null: it answered, and said "not a verdict".
        [{ refusal: null }, false],
        // FastAPI's validation body, or a foreign server's envelope.
        [{}, false],
        // Junk that must never be read as a verdict by a truthiness shortcut.
        [{ refusal: "" }, false],
        [{ refusal: 0 }, false],
        [{ refusal: false }, false],
        [{ refusal: ["engine"] }, false],
        [null, false],
        [undefined, false],
        [{}, false],
    ];

    // GIVEN each body shape as it arrives on `err.refusal`,
    // WHEN isVoiceVerdict classifies it, THEN only a non-empty string is a verdict.
    for (const [err, expected] of cases) {
        assert.equal(
            h.get(`isVoiceVerdict(${JSON.stringify(err)})`), expected,
            `expected ${expected} for ${JSON.stringify(err)}`,
        );
    }
});

test("enqueueStreamingTTS_afterAContent422_sendsNoFurtherRequestForThisReply", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));

    h.run('enqueueStreamingTTS("aria", "First speakable sentence.")');
    await pipelineIdle(h);
    assert.equal(h.ttsCalls().length, 1, "the first request went out");

    // GIVEN the engine already refused this reply's voice,
    // WHEN a perfectly good sentence arrives, THEN it is not re-requested.
    h.run('enqueueStreamingTTS("aria", "A perfectly speakable sentence.")');
    await sleep(20);
    assert.equal(h.ttsCalls().length, 1, "no further request for this reply");
});

test("processTTSRequests_onTransient502_keepsEveryLaterSentence", async () => {
    const h = createHarness();
    let calls = 0;
    h.fetchStub.routes.set("/api/tts", () => {
        calls += 1;
        // The first sentence hiccups (engine restart); the rest succeed.
        if (calls === 1) return ttsErrorResponse(502, detailBody("TTS server returned no audio"));
        return ttsSuccessResponse();
    });

    h.run(`
        enqueueStreamingTTS("aria", "First sentence.");
        enqueueStreamingTTS("aria", "Second sentence.");
        enqueueStreamingTTS("aria", "Third sentence.");
    `);
    await pipelineIdle(h);

    // GIVEN a transient 502 on the first of three sentences,
    // THEN all three are requested and no verdict is recorded.
    assert.equal(h.ttsCalls().length, 3, "a hiccup must not swallow the reply");
    assert.equal(h.refused("M1"), false, "502 is not a voice verdict");
    assert.equal(h.notes().length, 0, "no user-facing note for a transient failure");
});

test("processTTSRequests_onTheClipRefusalMappedToApp422_stopsTheReplyAndShowsTheAdvice", async () => {
    const h = createHarness();
    // The engine answers 400 ("the clip's first prompt_duration seconds hold
    // no speech") and the router forwards it as an app 422: the clip is the
    // persona's, so every sentence of every reply fails identically, and
    // LuxTTS raises it after encode_prompt (Whisper ASR, inside its synthesis
    // lock) — a client that kept asking would pay a near-full slot per
    // sentence, per reply, forever, and the user would never read the advice.
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, NO_SPEECH_IN_CLIP_BODY));

    h.run(`
        enqueueStreamingTTS("aria", "First sentence.");
        enqueueStreamingTTS("aria", "Second sentence.");
    `);
    await pipelineIdle(h);

    assert.equal(h.ttsCalls().length, 1, "the second sentence must not be requested");
    assert.equal(h.refused("M1"), true, "the clip verdict is recorded");
    assert.equal(h.notes().length, 1, "the advice reaches the user");
    assert.ok(h.notes()[0].textContent.includes("prompt_duration"), h.notes()[0].textContent);
});

test("processTTSRequests_onARequestModelValidationFailure_keepsTheReplyVoiceableAndLogsTheBug", async () => {
    // The issue #173 case as the browser sees it. `/api/tts` answers 422 for
    // TWO unrelated things: the voice verdicts above, and FastAPI rejecting the
    // request BODY (TTSRequest.text is min_length=1) before the handler ran —
    // a `detail` LIST, with no persona, engine, or voice consulted anywhere in
    // it. Reading that as "this persona cannot speak" muted a healthy reply
    // (the next sentence is a different, valid request) and printed a
    // serialized validation list under the bubble.
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", (call) => (
        // One sentence's request is malformed; the reply itself is fine.
        JSON.parse(call.body).text.startsWith("Bad")
            ? ttsErrorResponse(422, validationBody("text"))
            : ttsSuccessResponse()
    ));

    h.run(`
        enqueueStreamingTTS("aria", "Bad first sentence.");
        enqueueStreamingTTS("aria", "Second speakable sentence.");
        enqueueStreamingTTS("aria", "Third speakable sentence.");
    `);
    await pipelineIdle(h);

    // GIVEN a reply whose first sentence's request is rejected on shape,
    // THEN the other two sentences are still requested and played:
    assert.equal(h.ttsCalls().length, 3, "a bad request says nothing about its siblings");
    assert.equal(h.refused("M1"), false, "no verdict for a failure that judged nothing");
    // THEN no line reaches the bubble, and specifically no serialized list
    // ever does — the list is log material, not bubble material:
    assert.equal(h.notes().length, 0, "the user reads nothing about a shape failure");
    assert.ok(h.loggedText("error").includes("String should have at least 1 characters"),
        `the request bug must be logged where a developer will find it: ${h.loggedText("error")}`);
});

test("processAudioQueue_onARequestModelValidationFailure_showsNoNoteInNonStreamingMode", async () => {
    const h = createHarness();
    // Non-streaming mode holds one item per reply, so there is no sibling
    // sentence to save — but the note is the same lie there: a persona whose
    // request was rejected on shape has not been told it cannot speak.
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, validationBody("text")));

    h.run('enqueueTTS("aria", "A whole reply to synthesize in one go.")');
    await playbackIdle(h);

    assert.equal(h.get("audioQueue.length"), 0, "the item was processed");
    assert.equal(h.refused("M1"), false, "a shape failure is not a voice verdict");
    assert.equal(h.notes().length, 0, "no false verdict line under the reply");
    assert.ok(h.loggedText("error").length > 0, "the failure is logged, not swallowed");
});

test("handleSSEEvent_secondResponderStart_lateFirstResponder422MutesNeitherTheNewReplyNorItsBubble", async () => {
    // A TTS fetch is in flight for as long as one synthesis (seconds), so the
    // verdict for responder A can land long after responder B's "start" event
    // made B the current reply. Keying the breaker on "the current reply" let
    // A's late 422 mute B's whole reply and print A's explanation on B's
    // bubble — routine with max_persona_replies > 1, and near-certain in the
    // echo chamber, where the LLM emits every responder long before the first
    // synthesis finishes.
    const h = createHarness();
    const b = h.addRow("M2");
    h.sandbox.__rowB = b.row;

    let calls = 0;
    h.fetchStub.routes.set("/api/tts", async () => {
        calls += 1;
        if (calls === 1) {
            await sleep(40); // A's synthesis is still running
            return ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY);
        }
        return ttsSuccessResponse();
    });

    // GIVEN responder A's first sentence is in flight,
    h.run('currentAssistantMessageId = "M1";');
    h.run('enqueueStreamingTTS("Alex", "Hello from Alex.")');

    // WHEN responder B opens its reply (the real "start" handler) and queues
    // its own sentences while A's fetch is still running,
    h.run("ttsStreaming = true; selectedPersona = 'aria'");
    h.run('handleSSEEvent({ type: "start", persona: selectedPersona, message_id: "M2" })');
    h.run("currentAssistantRow = __rowB;");
    h.run('enqueueStreamingTTS("Luna", "Hello from Luna.")');

    await pipelineIdle(h);

    // THEN A is judged for A's message only, and B is not muted by it...
    assert.equal(h.refused("M1"), true, "the verdict belongs to the message that asked");
    assert.equal(h.refused("M2"), false, "a late verdict for A must not condemn B");
    assert.equal(h.ttsCalls().length, 2, "B's sentence is still requested");

    // ...and A's explanation is anchored on A's row, not on the row that
    // happened to be current when the verdict arrived.
    assert.equal(h.notesIn(b.content).length, 0, "B's bubble carries no note");
    assert.equal(h.notes().length, 1, "A's note is under A's reply");
});

test("resetTtsReplyState_withTheBreakerTripped_givesTheNextReplyAFreshVoice", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));

    h.run('enqueueStreamingTTS("aria", "A speakable sentence.")');
    await pipelineIdle(h);
    assert.equal(h.refused("M1"), true, "verdict recorded for the reply");

    h.run('enqueueStreamingTTS("aria", "Still muted on this reply.")');
    await sleep(20);
    assert.equal(h.ttsCalls().length, 1, "muted while broken");

    // GIVEN a recorded verdict, WHEN the next "start" event resets the state,
    // THEN the next reply is voiced again.
    h.run("resetTtsReplyState()");
    assert.equal(h.get("ttsBrokenMessageIds.size"), 0, "the verdict set is cleared");
    h.run('enqueueStreamingTTS("aria", "A fresh reply sentence.")');
    await sleep(20);
    assert.equal(h.ttsCalls().length, 2, "the next reply is requested again");
});

/* ==========================================================================
    The "start" event wiring (chat.js)
    ========================================================================== */

test("handleSSEEvent_onStartEvent_clearsTheBreakerOfThePreviousReply", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));

    h.run('enqueueStreamingTTS("aria", "A speakable sentence.")');
    await pipelineIdle(h);
    assert.equal(h.refused("M1"), true, "the reply recorded a verdict");

    // GIVEN a broken voice, streaming TTS enabled, and "aria" the selected
    // persona (the state the sidebar would be in),
    h.run("ttsStreaming = true; selectedPersona = 'aria'");

    // WHEN the server opens the next assistant reply (the real "start"
    // handler, not a hand-call of the reset helper),
    h.run('handleSSEEvent({ type: "start", persona: selectedPersona, message_id: "M2" })');

    // THEN the verdicts are gone, so the next reply is voiced again.
    assert.equal(h.refused("M1"), false, "start cleared the verdicts");
    assert.equal(h.get("currentAssistantMessageId"), "M2", "and adopted the new message id");

    h.fetchStub.routes.set("/api/tts", ttsSuccessResponse);
    h.run('enqueueStreamingTTS("aria", "A fresh reply sentence.")');
    await sleep(20);
    assert.equal(h.ttsCalls().length, 2, "the next reply is requested");
});

test("handleSSEEvent_onStartEventInNonStreamingMode_stillClearsTheBreaker", async () => {
    // ttsBrokenMessageIds is fed by BOTH queues: processAudioQueue's 422
    // branch records a verdict per non-streaming reply just like the streaming
    // pipeline does. resetTtsReplyState() used to sit inside the
    // `if (ttsStreaming)` guard of the "start" handler, so with streaming off
    // (the DEFAULT) every refused reply added a message ID that nothing ever
    // removed — a set that only grows for the session, and a state.js comment
    // describing a clear site that did not run.
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, MISSING_TRANSCRIPT_BODY));

    // GIVEN a non-streaming reply whose voice the app refused,
    h.run('enqueueTTS("Alex", "The whole reply, spoken in one piece.")');
    await playbackIdle(h);
    assert.equal(h.get("ttsStreaming"), false, "streaming off — the default configuration");
    assert.equal(h.refused("M1"), true, "the non-streaming queue recorded the verdict");

    // WHEN the server opens the next reply, through the real "start" handler
    // with streaming still off,
    h.run('handleSSEEvent({ type: "start", persona: "Alex", message_id: "M2" })');

    // THEN the verdict set is drained, so it stays bounded to one reply in
    // both modes.
    assert.equal(h.get("ttsBrokenMessageIds.size"), 0, "start cleared the verdicts with streaming off");
});

/* ==========================================================================
    Non-streaming mode gets the same verdict line (processAudioQueue)
    ========================================================================== */

/** Let the non-streaming playback queue settle: drained, nothing in flight. */
async function playbackIdle(h) {
    await waitUntil(() => h.get("audioQueue.length") === 0 && h.get("isPlayingAudio") === false);
    // One extra beat for the trailing setTimeout(() => processAudioQueue(), 100).
    await sleep(120);
}

test("processAudioQueue_onAMissingRequiredTranscript422_printsTheReasonUnderTheReplyAndPlaysNothing", async () => {
    // GIVEN non-streaming mode (one /api/tts request for the whole reply) and
    // an app 422 saying this persona has no reference transcript while the
    // connected engine demands one,
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, MISSING_TRANSCRIPT_BODY));

    h.run('enqueueTTS("Alex", "The whole reply, spoken in one piece.")');
    await playbackIdle(h);

    // THEN the reply is silent, exactly one request was made, and the reason is
    // on screen. Before this fix the whole catch block was a console.warn, so a
    // persona the engine cannot serve produced absolute, unexplained silence
    // for everyone who left streaming mode off (which is the default).
    assert.equal(h.ttsCalls().length, 1);
    assert.equal(h.audioCtx.sources.length, 0, "nothing may be played");
    const notes = h.notes();
    assert.equal(notes.length, 1, "exactly one note line");
    assert.ok(notes[0].textContent.includes(MISSING_TRANSCRIPT_DETAIL), notes[0].textContent);
    assert.equal(h.refused("M1"), true, "the verdict belongs to the message that asked");
    assert.equal(h.get("isPlayingAudio"), false, "the playback queue must not be left wedged");
});

test("processAudioQueue_onTransient502_printsNoNoteAndRecordsNoVerdict", async () => {
    // GIVEN the same non-streaming queue and an ENGINE HICCUP instead of a
    // verdict,
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(502, detailBody("CUDA out of memory")));

    h.run('enqueueTTS("Alex", "The whole reply, spoken in one piece.")');
    await playbackIdle(h);

    // THEN nothing is printed and no verdict is recorded: 5xx is transient, and
    // the next reply must still be asked — the new 422 branch must not swallow it.
    assert.equal(h.notes().length, 0);
    assert.equal(h.refused("M1"), false);
    assert.equal(h.get("isPlayingAudio"), false);
});

/* ==========================================================================
    A persona's missing transcript is a verdict, not a mystery (issue #164)
    ========================================================================== */

test("processTTSRequests_onTheMissingRequiredTranscript422_showsTheAppSentenceAndStopsTheReply", async () => {
    // GIVEN an engine that demands the reference transcript (breeze-blue,
    // ming3-tts-moe, qwen3-tts-mlx) and a persona that has a clip but no
    // ref.txt: TalkWithMe answers the 422 itself, before the engine is asked,
    // and the sentence names the persona, the engine, and where to fix it.
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, MISSING_TRANSCRIPT_BODY));

    h.run(`
        enqueueStreamingTTS("Alex", "First speakable sentence.");
        enqueueStreamingTTS("Alex", "Second speakable sentence.");
    `);
    await pipelineIdle(h);

    assert.equal(h.ttsCalls().length, 1, "one ask, then the verdict stands");
    const notes = h.notes();
    assert.equal(notes.length, 1);
    assert.ok(notes[0].textContent.includes("no reference-audio transcript"), notes[0].textContent);
    assert.ok(notes[0].textContent.includes("Persona editor"), notes[0].textContent);
    assert.equal(h.refused("M1"), true);
});

/* ==========================================================================
    fetchTTS's error surface
    ========================================================================== */

test("fetchTTS_onHttp422_rejectsWithStatusAndTheEngineDetailVerbatim", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, UNSPEAKABLE_TEXT_BODY));

    // GIVEN a 422 whose body is the engine's sentence for the user,
    // THEN the caller gets the status AND the words.
    const err = await h
        .get("fetchTTS('aria', '....', 'M1')")
        .then(() => null, (e) => e);
    assert.ok(err, "a non-2xx response must reject");
    assert.equal(err.status, 422, "status attached");
    assert.equal(err.detail, UNSPEAKABLE_TEXT_DETAIL, "detail attached verbatim");
    // The class is what the pipeline acts on, and it travels beside the
    // sentence rather than being inferred from it (issue #173).
    assert.equal(err.refusal, REFUSAL_ENGINE, "the server's refusal class attached");
});

test("fetchTTS_onAVerdictBodyWithNoSentence_stillCarriesTheRefusalClass", async () => {
    const h = createHarness();
    // A body that declares a verdict and quotes nothing: the class alone is
    // enough to trip the breaker, and the note falls back to the error message
    // rather than printing "undefined" under the reply.
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, { refusal: REFUSAL_PERSONA_FILES }));

    const err = await h.get("fetchTTS('aria', 'Hello.', 'M1')").then(() => null, (e) => e);

    assert.equal(err.refusal, REFUSAL_PERSONA_FILES, "class attached with no sentence");
    assert.ok(!String(err.detail).includes("undefined"), err.detail);
});

test("fetchTTS_onAValidationListBody_serializesItForTheLogAndCarriesNoRefusal", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, validationBody("text")));

    const err = await h.get("fetchTTS('aria', 'Hello.', 'M1')").then(() => null, (e) => e);

    // The serialized list stays on the error as LOG material (it names the
    // offending field, which is the whole diagnostic there) — what changed in
    // #173 is that nothing puts it on screen any more, because `refusal` says
    // this failure judged nothing about the voice.
    assert.equal(err.status, 422, "status attached");
    assert.ok(err.detail.includes("String should have at least 1 characters"), err.detail);
    assert.equal(err.refusal, null, "a body with no refusal class carries no verdict");
});

test("fetchTTS_onANonJsonErrorBody_degradesToTheStatus", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(502, undefined));

    const err = await h.get("fetchTTS('aria', 'Hello.', 'M1')").then(() => null, (e) => e);

    assert.equal(err.status, 502, "status attached");
    assert.equal(err.detail, "HTTP 502", "a proxy HTML page degrades to the status");
    assert.equal(err.refusal, null, "an unreadable body cannot be a verdict either");
});

test("fetchTTS_onSuccessWithNoAudio_returnsNullAndNeverThrows", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ({
        ok: true,
        status: 200,
        json: async () => ({ audio_base64: null, sample_rate: 24000 }),
    }));

    // GIVEN an engine that answers 200 with no audio (the engine's own "nothing
    // to do" path), THEN it stays a null, now that non-2xx throws.
    const result = await h.get("fetchTTS('aria', 'Hello.', 'M1')");
    assert.equal(result, null, "no audio -> null");
});

test("fetchTTS_onSuccess_decodesQueuesAndPersistsTheAudio", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", ttsSuccessResponse);

    h.run('enqueueStreamingTTS("aria", "A speakable sentence.")');
    await pipelineIdle(h);
    await waitUntil(() => h.uploadAudioCalls.length === 1);

    // GIVEN a successful synthesis, WHEN it flows through the real pipeline,
    // THEN it is persisted for replay, queued for playback, and the reply is
    // left in a clean state (no breaker, no note, nothing wedged).
    assert.equal(h.uploadAudioCalls[0].messageId, "M1", "persisted against its message");
    assert.equal(h.ttsCalls().length, 1, "one request");
    assert.equal(h.refused("M1"), false, "success leaves no verdict");
    assert.equal(h.notes().length, 0, "no note on success");
    assert.equal(h.get("audioBufferQueue.length"), 0, "the playback queue drained");
    assert.equal(h.get("isFetchingTTS"), false, "the fetch pipeline is idle");
});

/* ==========================================================================
    The shape of the rule itself (grep guards)
    ========================================================================== */

test("ttsScript_neverClassifiesAnHttpStatusCodeAndNoScriptSpecialCases422", () => {
    // Issue #173 in two regexes. The browser used to answer "is this persona's
    // voice gone?" with `err.status === 422`, and no number can carry that
    // question: /api/tts 422s for a verdict AND for a request body Pydantic
    // rejected before the handler ran. The server now states the class and the
    // client reads it, so a status comparison is the bug walking back in —
    // grep for it rather than trusting a code review.
    //
    // Two scopes, because status checks are legal elsewhere: the file that
    // owns the TTS pipeline may not compare a status at all, and nowhere in
    // static/ may anything special-case 422 (chat.js legitimately branches on
    // 404 when deleting a message; settings.js on a 422 from the capabilities
    // PROBE, which is a form error the user can fix in the field).
    // Any way a status can be classified, not just the one shape the bug had.
    // Equality was the starting point; `>= 400` and `switch (err.status)` are
    // what someone "simplifying" the check writes next, and a guard that lets
    // the refactor through guards nothing. Range comparisons, membership, and
    // switch-on-status are all the same decision made louder.
    const statusComparison = /\.status\s*(?:[=!]==?|[<>]=?|\bin\b)/;
    const statusSwitch = /switch\s*\(\s*[\w.]*\.status\s*\)/;
    const anyStatusJudgement = (line) => statusComparison.test(line) || statusSwitch.test(line);
    const overload = /\.status\s*(?:[=!]==?|[<>]=?\s*)\s*422|case\s+422\b/;

    const offendingLines = (source, isOffending) => source.split("\n")
        .map((line, i) => ({ line, n: i + 1 }))
        .filter(({ line }) => !line.trim().startsWith("//") && isOffending(line));

    // Self-check first: a guard that matches nothing is indistinguishable from
    // a fix. Feed each forbidden shape through the predicate and demand it
    // flinch, including the two the original regex waved through.
    for (const shape of [
        "if (err.status === 422) { trip(); }",
        "if (err.status == 502) return;",
        "if (err.status !== 422) { keepGoing(); }",
        "if (err.status >= 422) { trip(); }",
        "if (err.status > 499) { retry(); }",
        "switch (err.status) { case 422: trip(); }",
    ]) {
        assert.ok(anyStatusJudgement(shape), `the guard must reject: ${shape}`);
    }
    assert.ok(!anyStatusJudgement('if (isVoiceVerdict(err)) { trip(); }'),
        "the sanctioned predicate must not read as a status judgement");
    assert.ok(!anyStatusJudgement("console.error('failed:', err.status, err.detail);"),
        "reading a status for a LOG LINE is legal — only deciding from one is not");

    const ttsSource = fs.readFileSync(path.join(__dirname, "..", "static", "tts.js"), "utf8");
    assert.deepEqual(offendingLines(ttsSource, anyStatusJudgement), [],
        "static/tts.js must decide the voice verdict from the server's `refusal` "
        + "class, never from an HTTP status (issue #173)");

    // Every script is swept by default; an exemption has to be written down
    // with the route whose 422 it legitimately reads. settings.js is the one
    // today: it branches on the 422 from GET /api/tts/capabilities?base_url=…
    // ("that url has no scheme"), a form error on the Servers modal for a
    // route that never answers a synthesis and never claims anything about a
    // persona's voice.
    const LEGAL_422_READERS = new Set(["settings.js"]);

    for (const file of fs.readdirSync(path.join(__dirname, "..", "static"))) {
        if (!file.endsWith(".js") || LEGAL_422_READERS.has(file)) continue;
        const source = fs.readFileSync(path.join(__dirname, "..", "static", file), "utf8");
        assert.deepEqual(offendingLines(source, (line) => overload.test(line)), [],
            `${file} special-cases HTTP 422, a number /api/tts reserves for two unrelated things (issue #173)`);
    }

    // And the structural half: every verdict handler must sit behind the one
    // predicate. A second call site that re-derives the decision "next to" the
    // helper is how the rule gets copied a second time.
    const handlerCalls = (ttsSource.match(/^\s+handleUnvoiceableVerdict\(item, err\);/gm) || []).length;
    const guarded = (ttsSource.match(/if\s*\(\s*isVoiceVerdict\(err\)\s*\)/g) || []).length;
    assert.ok(handlerCalls >= 2,
        `both playback pipelines must be able to record a verdict, found ${handlerCalls}`);
    assert.equal(guarded, handlerCalls,
        "every circuit-breaker call must be guarded by isVoiceVerdict(err)");
});

test("refusalClassFixtures_matchTheConstantsTheBackendSends", () => {
    // The client acts on a string the backend writes. Nothing in this repo
    // would notice if one side renamed "engine" to "engine_verdict": the
    // fixtures here would keep describing the OLD wire format, every verdict
    // test would still pass against those fixtures, and the real browser would
    // stop seeing verdicts altogether (silent muting on one side, silent
    // re-asking on the other). So the fixtures are checked against the source
    // of the truth.
    const router = fs.readFileSync(
        path.join(__dirname, "..", "app", "routers", "tts.py"), "utf8");
    const backendConstant = (name) => {
        const match = router.match(new RegExp(`^${name} = "([^"]+)"`, "m"));
        assert.ok(match, `app/routers/tts.py no longer defines ${name} — the client's `
            + "refusal classes and this suite's fixtures must be updated together");
        return match[1];
    };

    assert.equal(REFUSAL_ENGINE, backendConstant("REFUSAL_ENGINE"));
    assert.equal(REFUSAL_PERSONA_FILES, backendConstant("REFUSAL_PERSONA_FILES"));

    // ...and the two bodies the verdict tests drive the pipeline with really
    // carry those classes: a fixture that lost its `refusal` would quietly
    // turn every "the breaker tripped" assertion into "it did not".
    assert.equal(UNSPEAKABLE_TEXT_BODY.refusal, REFUSAL_ENGINE);
    assert.equal(NO_SPEECH_IN_CLIP_BODY.refusal, REFUSAL_ENGINE);
    assert.equal(MISSING_TRANSCRIPT_BODY.refusal, REFUSAL_PERSONA_FILES);

    // And the three non-verdict shapes must stay distinguishable, because the
    // client's rule is "a present, non-empty class" and the fixtures are what
    // teach it the difference. `detailBody` is the ROUTER answering "not a
    // verdict" (key present, null — app/routers/tts.py::_failure_body always
    // stamps it); `validationBody` is FastAPI answering before the router ran
    // (key absent); a non-JSON proxy page is neither, and reaches the client as
    // `{ detail: "HTTP <status>", refusal: null }`. A fixture that drifted into
    // another shape would leave the "keep asking" tests asserting a wire format
    // nobody emits.
    assert.equal("refusal" in detailBody("CUDA out of memory"), true,
        "the router's own non-verdict body carries the key with a null value");
    assert.equal(detailBody("CUDA out of memory").refusal, null,
        "null means 'we answered, this is not a verdict' — never a class");
    assert.equal("refusal" in validationBody("text"), false,
        "the validation fixture must stay the UNLABELLED 422 (the router never ran)");
});

/* ==========================================================================
    The engine's words are the contract — check them against the server
    ========================================================================== */

test("engineDetailStrings_areVerbatimInTheTtsServeServerWhenTheCheckoutIsPresent", () => {
    // GIVEN a tts-serve checkout beside this repo. A fresh clone or a CI box
    // without one is not a failure: the pinned constants above are the
    // contract, this check is the alarm that they stopped matching the server
    // that writes them (a pre-merge copy of these two sentences pinned a
    // wording the merged 1.5 server never shipped, and the suite stayed green).
    if (!fs.existsSync(TTS_SERVE_LUX_SERVER)) {
        console.log(`  [verbatim] no tts-serve checkout at ${TTS_SERVE_LUX_SERVER} — constants unchecked`);
        return;
    }

    // WHEN the engine's refusal sentences are read out of the server source
    // (f-string fragments included — the clip 400 names its window from the
    // request, so the source interleaves f"..." and "..." literals). One
    // iteration is a string literal plus whitespace and nothing else: a group
    // that allows slop between literals swallows the closing paren and runs on
    // into the next function's docstring, which is how one match ate the file.
    const source = fs.readFileSync(TTS_SERVE_LUX_SERVER, "utf8");
    const refusals = [...source.matchAll(/status_code=(\d+),\s*detail=\(\s*((?:f?"[^"]*"\s*)+)\)/g)]
        .map((m) => ({
            status: Number(m[1]),
            text: [...m[2].matchAll(/"([^"]*)"/g)].map((part) => part[1]).join(""),
        }));
    const textRefusal = refusals.find((r) => r.status === 422 && r.text.includes("nothing this engine can speak"));
    const clipRefusal = refusals.find((r) => r.status === 400 && r.text.includes("no recognizable speech"));

    // THEN both pre-flights still exist. A pre-flight that is simply absent
    // means the checkout predates tts-serve's issue 70 (the 1.5-dev merge of
    // PR #71) — this client's whole resilience design routes those two
    // sentences, and a tts-serve that cannot send them makes this release's
    // README promises false, so it has to be loud, not skipped.
    assert.ok(
        textRefusal,
        `${TTS_SERVE_LUX_SERVER} has no unvoiceable-text 422 — that checkout is ` +
        "older than tts-serve issue 70 (1.5-dev, PR #71); update it, this client ships against it",
    );
    assert.ok(
        clipRefusal,
        `${TTS_SERVE_LUX_SERVER} has no reference-clip 400 — that checkout is ` +
        "older than tts-serve issue 70 (1.5-dev, PR #71); update it, this client ships against it",
    );

    // ...and the pinned copies are character-for-character identical (the
    // engine's double spaces after full stops included: the UI shows them).
    // The clip sentence is an f-string on the engine side; the window renders
    // from the advertised default (capabilities prompt_duration: 5.0).
    assert.equal(
        UNSPEAKABLE_TEXT_DETAIL, textRefusal.text,
        "the pinned unvoiceable-text sentence drifted from tts-serve — re-copy it verbatim",
    );
    assert.equal(
        NO_SPEECH_IN_CLIP_DETAIL, clipRefusal.text.replace("{prompt_duration:g}", "5"),
        "the pinned reference-clip sentence drifted from tts-serve — re-copy it verbatim",
    );
});
