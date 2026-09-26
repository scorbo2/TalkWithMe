/**
 * test_speaking_highlight.js — Regression tests for the speaking-bubble
 * highlight (static/tts.js + static/chat.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_speaking_highlight.js
 *
 * Invariants under test:
 *
 * 1. A message row is brightened (".speaking" class) exactly for the
 *    duration of its audio playback — live TTS (both the non-streaming
 *    queue and the streaming sentence pipeline) and manual playback of
 *    persisted audio (playPersistedAudio).
 * 2. Consecutive sentences of one message do NOT flicker: the highlight
 *    stays on across the 80 ms inter-sentence gap in streaming mode
 *    (endSpeaking of sentence N and beginSpeaking of sentence N+1 run in
 *    the same tick, so no frame can paint the off state).
 * 3. The highlight is per message, not per playback: two overlapping
 *    sources for the same message (a double-clicked play button, or a
 *    manual play while the message's live TTS is still finishing) keep
 *    the row highlighted until the LAST source ends — the per-message
 *    refcount in chat.js (speakingMessageIds) is what enforces this.
 * 4. The highlight is keyed by the row's data-message-id, so a row that
 *    is deleted (or the room switched away) mid-playback degrades to a
 *    no-op instead of an error; null/unknown IDs never touch the DOM.
 *
 * How it works: the browser scripts share globals (no ES modules), so
 * each test evaluates them in a fresh vm.Context against a minimal DOM
 * stub, a fake AudioContext whose sources end on a configurable timer,
 * and stubbed fetch/uploadAudio/getAudioUrl. The real processAudioQueue(),
 * processAudioBufferQueue() and playPersistedAudio() are exercised; the
 * ".speaking" class toggles are observed on the stub rows.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_speaking_highlight.js
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
    DOM / browser stubs
    ========================================================================== */

/**
 * Minimal stand-in for an HTMLElement. Implements what chat.js / tts.js
 * touch on rows and the messages container: classList (with a toggle
 * history for assertions), dataset, querySelector (backed by an explicit
 * link map — the stub implements no selector engine), appendChild/
 * insertBefore, and the scroll properties utils.js reads.
 */
function makeFakeElement(id) {
    const classSet = new Set();
    const childSelectors = new Map(); // querySelector arg -> element
    const toggleHistory = [];          // [{ cls, want }, ...] in call order

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
        querySelector(sel) {
            return childSelectors.get(sel) || null;
        },
        // Test helper: what querySelector(sel) should return.
        linkChildSelector(sel, child) {
            childSelectors.set(sel, child);
        },
        toggleHistory,
    };

    el.classList = {
        add: (c) => classSet.add(c),
        remove: (c) => classSet.delete(c),
        contains: (c) => classSet.has(c),
        toggle(c, force) {
            const want = force === undefined ? !classSet.has(c) : !!force;
            if (want) classSet.add(c);
            else classSet.delete(c);
            toggleHistory.push({ cls: c, want });
            return want;
        },
    };

    return el;
}

/**
 * Fake Web Audio: decodeAudioData always succeeds, and each BufferSource
 * ends after ctx.sourceDelayMs so tests can sample the "playing" window.
 */
function makeFakeAudioContext() {
    const ctx = {
        destination: {},
        sourceDelayMs: 50,
        sources: [],
        decodeAudioData: async (buffer) => ({
            fake: true,
            bytes: buffer ? buffer.byteLength : 0,
        }),
        createBufferSource() {
            const source = {
                buffer: null,
                connected: false,
                started: false,
                onended: null,
                connect() {
                    this.connected = true;
                },
                start() {
                    this.started = true;
                    // End on a later tick so tests can observe the playing
                    // window between start and onended.
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

/** Minimal fetch Response stand-in. */
function jsonResponse(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

/**
 * Canned /api/tts success response. The payload content is irrelevant:
 * the fake decodeAudioData accepts anything.
 */
function ttsResponse() {
    return jsonResponse({
        audio_base64: Buffer.from("fake-wav-bytes").toString("base64"),
        sample_rate: 24000,
    });
}

/** Canned persisted-audio file response. */
function audioFileResponse() {
    return { ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(8) };
}

/**
 * Build the sandbox for one test: fresh DOM elements, a fake AudioContext,
 * a fetch stub with per-URL routes, and stubs for the persistence.js /
 * browser globals the loaded scripts rely on (uploadAudio, getAudioUrl,
 * requestAnimationFrame, atob, window.AudioContext).
 */
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
        const method = (options && options.method) || "GET";
        fetchStub.calls.push({
            url: u,
            method,
            body: options ? options.body : undefined,
        });
        const route = fetchStub.routes.get(u) || fetchStub.routes.get("*");
        return route ? route() : jsonResponse({});
    };
    fetchStub.calls = [];
    fetchStub.routes = new Map();

    const uploadAudioCalls = [];
    const sandbox = {
        console,
        document: documentStub,
        fetch: fetchStub,
        atob: (s) => Buffer.from(s, "base64").toString("latin1"),
        requestAnimationFrame: () => 0,
        // The vm context has no timers; the app code uses setTimeout for
        // the inter-sentence gap and the queue re-entry delays.
        setTimeout,
        clearTimeout,
        // state.js declares `let audioCtx = null`; the scripts' lazy init
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
    };

    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, file), "utf8"), sandbox, {
            filename: file,
        });
    }

    return {
        sandbox,
        elementById,
        messagesEl: elementById("messages"),
        audioCtx,
        fetchStub,
        uploadAudioCalls,
        /** Evaluate an expression inside the app context (reaches let/const globals). */
        get(expression) {
            return vm.runInContext(`(() => (${expression}))()`, sandbox);
        },
        /** Run a statement inside the app context. */
        run(statement) {
            return vm.runInContext(statement, sandbox);
        },
    };
}

/* ==========================================================================
    Test helpers
    ========================================================================== */

/**
 * Build a message row stub and register it in the messages container
 * under every selector the app uses to find rows by message ID.
 */
function linkRow(h, messageId) {
    const row = makeFakeElement(`row-${messageId}`);
    row.dataset.messageId = messageId;
    // .bubble-content is what the addAudioButtonTo* helpers dig into when
    // the (stubbed) upload resolves.
    const content = makeFakeElement(`content-${messageId}`);
    row.linkChildSelector(".bubble-content", content);
    h.messagesEl.linkChildSelector(`.message-row[data-message-id="${messageId}"]`, row);
    h.messagesEl.linkChildSelector(`.message-row.assistant[data-message-id="${messageId}"]`, row);
    h.messagesEl.linkChildSelector(`.message-row.user[data-message-id="${messageId}"]`, row);
    return row;
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Poll until fn() is truthy (the fake audio timers make exact awaits racy). */
async function waitUntil(fn, timeoutMs = 2000, stepMs = 5) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if (fn()) return;
        if (Date.now() > deadline) throw new Error("waitUntil timed out");
        await sleep(stepMs);
    }
}

/* ==========================================================================
    Tests
    ========================================================================== */

test("processAudioQueue highlights the row for the duration of playback", async () => {
    const h = createHarness();
    const row = linkRow(h, "M1");
    h.fetchStub.routes.set("/api/tts", ttsResponse);

    // The "start" event would have stamped the row and adopted the ID:
    h.run("currentAssistantMessageId = 'M1'");
    h.run("enqueueTTS('Al', 'Hello there.')");

    // WHILE the fetched audio plays, the row is highlighted.
    await waitUntil(() => row.classList.contains("speaking"));
    assert.ok(row.classList.contains("speaking"));

    // AFTER playback ends, the highlight is cleared.
    await waitUntil(() => !row.classList.contains("speaking"));
    assert.ok(!row.classList.contains("speaking"));

    // The queue drained and the audio was fetched for the right text.
    assert.equal(h.get("audioQueue.length"), 0);
    assert.equal(h.get("isPlayingAudio"), false);
    const ttsCall = h.fetchStub.calls.find((c) => c.url === "/api/tts");
    assert.ok(ttsCall, "no /api/tts request was made");
    assert.equal(JSON.parse(ttsCall.body).text, "Hello there.");
});

test("processAudioBufferQueue keeps the highlight on across sentences of one message", async () => {
    const h = createHarness();
    const rowA = linkRow(h, "A");
    const rowB = linkRow(h, "B");
    h.audioCtx.sourceDelayMs = 50; // each sentence plays 50 ms, gaps are 80 ms
    h.fetchStub.routes.set("/api/tts", ttsResponse);

    // Three queued sentences: two from message A, then one from message B.
    // They enter through the real fetch pipeline (processTTSRequests ->
    // fetchTTS -> audioBufferQueue), which also performs the audioCtx lazy
    // init that production playback always goes through.
    h.run(
        "ttsRequestQueue.push({ personaName: 'Al', text: 'One.', messageId: 'A' });" +
        "ttsRequestQueue.push({ personaName: 'Al', text: 'Two.', messageId: 'A' });" +
        "ttsRequestQueue.push({ personaName: 'Bo', text: 'Three.', messageId: 'B' });" +
        "processTTSRequests();"
    );

    // Sentence 1 (A) is playing.
    await waitUntil(() => rowA.classList.contains("speaking"));

    // Mid-gap after sentence 1 (it ended ≈50 ms in; the gap runs to
    // ≈130 ms): A must still be highlighted — no flicker between
    // consecutive sentences of the same message.
    await sleep(90);
    assert.ok(
        rowA.classList.contains("speaking"),
        "A flickered off during the inter-sentence gap"
    );

    // By ≈140 ms sentence 2 (A) is playing.
    await sleep(50);
    assert.ok(rowA.classList.contains("speaking"));

    // When sentence 3 (B) starts, A is cleared and B is highlighted.
    await waitUntil(() => rowB.classList.contains("speaking"));
    assert.ok(!rowA.classList.contains("speaking"), "A stayed highlighted while B spoke");

    // After B's sentence ends, B is cleared too and the queue is drained.
    await waitUntil(() => !rowB.classList.contains("speaking"));
    assert.equal(h.get("audioBufferQueue.length"), 0);
    assert.equal(h.get("isPlayingAudioBuffer"), false);
});

test("playPersistedAudio highlights the row while its audio plays", async () => {
    const h = createHarness();
    const row = linkRow(h, "U1");
    h.fetchStub.routes.set("/api/persist/audio/default/rec.wav", audioFileResponse);

    h.sandbox.playPersistedAudio("default", "rec.wav", row);

    // WHILE the fetched file plays, the row is highlighted.
    await waitUntil(() => row.classList.contains("speaking"));
    // AFTER it ends, the highlight is cleared and the refcount is clean.
    await waitUntil(() => !row.classList.contains("speaking"));
    assert.equal(h.get("speakingMessageIds.size"), 0);
});

test("playPersistedAudio does not highlight when the fetch fails", async () => {
    const h = createHarness();
    const row = linkRow(h, "U2");
    h.fetchStub.routes.set(
        "/api/persist/audio/default/missing.wav",
        () => ({ ok: false, status: 404 }),
    );

    h.sandbox.playPersistedAudio("default", "missing.wav", row);
    await sleep(120); // well past the fake playback window

    assert.ok(!row.classList.contains("speaking"));
    assert.equal(h.get("speakingMessageIds.size"), 0);
});

test("two overlapping plays of one message keep the highlight until the last source ends", async () => {
    const h = createHarness();
    const row = linkRow(h, "D1");
    h.fetchStub.routes.set("/api/persist/audio/default/dup.wav", audioFileResponse);
    h.audioCtx.sourceDelayMs = 80;

    // Two overlapping plays of the same message: the first source ends
    // ≈80 ms in, while the second (started ≈30 ms later) is still going.
    h.sandbox.playPersistedAudio("default", "dup.wav", row);
    await sleep(30);
    h.sandbox.playPersistedAudio("default", "dup.wav", row);

    // t≈90 ms: source 1 has ended, source 2 is still playing. The
    // refcount must keep the row highlighted — a plain boolean would
    // have cleared it the moment the first source's onended fired.
    await sleep(60);
    assert.ok(
        row.classList.contains("speaking"),
        "highlight died when the first of two overlapping sources ended"
    );

    // t≈150 ms: both sources have ended; now the highlight is cleared.
    await sleep(60);
    assert.ok(!row.classList.contains("speaking"));
    assert.equal(h.get("speakingMessageIds.size"), 0);
});

test("setSpeakingHighlight / begin / end are no-ops for null or unknown IDs", async () => {
    const h = createHarness();
    h.sandbox.setSpeakingHighlight(null, true);
    h.sandbox.setSpeakingHighlight("does-not-exist", true);
    h.sandbox.beginSpeaking(null);
    h.sandbox.beginSpeaking("does-not-exist");
    h.sandbox.endSpeaking("does-not-exist");
    h.sandbox.endSpeaking(null);

    // Unknown IDs may never leave a dangling refcount or touch the DOM.
    assert.equal(h.get("speakingMessageIds.has('does-not-exist')"), false);
    assert.equal(h.get("speakingMessageIds.size"), 0);
});
