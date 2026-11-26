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
 * transient. One sentence of a reply must not eat the reply's audio, and one
 * reply's broken voice must not eat the session.
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
 * 3. A 422 (the engine's verdict on the voice) trips the breaker for the
 *    message that ASKED: exactly one request goes out, that message's rest of
 *    the queue is dropped, the fetch pipeline is not left wedged, and the
 *    engine's own sentence reaches the user as one muted line anchored on
 *    that message's .bubble-content (the row is a flex row: a direct row
 *    child renders beside the bubble, not under it).
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

/** A body that quotes an engine sentence, as the router sends it. */
function detailBody(detail) {
    return { detail };
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
        ? ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL))
        : ttsSuccessResponse());
}

const CYRILLIC = /\p{Script=Cyrillic}/u;

/** A FastAPI validation failure: `detail` is a list, not a sentence. */
function validationBody(field) {
    return {
        detail: [{ loc: ["body", field], msg: "Extra inputs are not permitted", type: "extra_forbidden" }],
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
    const sandbox = {
        console,
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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));

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

    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));
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

test("processTTSRequests_onContent422WithANonJsonBody_fallsBackToTheStatus", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, undefined));

    h.run('enqueueStreamingTTS("aria", "A speakable sentence.")');
    await pipelineIdle(h);

    // GIVEN a 422 whose body is not JSON (a proxy in front of the backend),
    // THEN the verdict is recorded all the same and the note degrades to the
    // status. (The backend self-heals a non-JSON 422 — a proxy page is not a
    // tts-serve content sentence — so what reaches here is the surviving one.)
    assert.equal(h.refused("M1"), true, "verdict recorded without a detail");
    assert.equal(h.notes().length, 1, "one note line");
    assert.ok(h.notes()[0].textContent.includes("HTTP 422"), h.notes()[0].textContent);
});

test("enqueueStreamingTTS_afterAContent422_sendsNoFurtherRequestForThisReply", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(NO_SPEECH_IN_CLIP_DETAIL)));

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

test("processTTSRequests_onSchema422WithAValidationList_tripsTheBreakerLikeAny422", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, validationBody("temperature")));

    h.run('enqueueStreamingTTS("aria", "A speakable sentence.")');
    await pipelineIdle(h);

    // GIVEN a schema rejection (detail is a list, not a sentence),
    // THEN the client cannot repair it mid-reply: the verdict is recorded, and
    // the list shape reaches the note rather than being swallowed.
    assert.equal(h.refused("M1"), true, "a schema 422 also ends this reply");
    assert.ok(h.notes()[0].textContent.includes("Extra inputs are not permitted"), h.notes()[0].textContent);
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
            return ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL));
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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(MISSING_TRANSCRIPT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(MISSING_TRANSCRIPT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(MISSING_TRANSCRIPT_DETAIL)));

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
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, detailBody(UNSPEAKABLE_TEXT_DETAIL)));

    // GIVEN a 422 whose body is the engine's sentence for the user,
    // THEN the caller gets the status AND the words.
    const err = await h
        .get("fetchTTS('aria', '....', 'M1')")
        .then(() => null, (e) => e);
    assert.ok(err, "a non-2xx response must reject");
    assert.equal(err.status, 422, "status attached");
    assert.equal(err.detail, UNSPEAKABLE_TEXT_DETAIL, "detail attached verbatim");
});

test("fetchTTS_onHttp422WithAValidationList_serializesTheListIntoTheDetail", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(422, validationBody("temperature")));

    const err = await h.get("fetchTTS('aria', 'Hello.', 'M1')").then(() => null, (e) => e);

    assert.equal(err.status, 422, "status attached");
    assert.ok(err.detail.includes("Extra inputs are not permitted"), err.detail);
});

test("fetchTTS_onANonJsonErrorBody_degradesToTheStatus", async () => {
    const h = createHarness();
    h.fetchStub.routes.set("/api/tts", () => ttsErrorResponse(502, undefined));

    const err = await h.get("fetchTTS('aria', 'Hello.', 'M1')").then(() => null, (e) => e);

    assert.equal(err.status, 502, "status attached");
    assert.equal(err.detail, "HTTP 502", "a proxy HTML page degrades to the status");
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
