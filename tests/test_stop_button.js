/**
 * test_stop_button.js — Regression tests for the top-bar stop button
 * (docs/feature_stop_button.md; static/tts.js + static/chat.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_stop_button.js
 *
 * The stop button is a temporary "mute", not a cancel. Invariants under
 * test:
 *
 * 1. The button is disabled while no audio source is producing sound and
 *    enabled while at least one is — tracked via the activeAudioSources
 *    Set in state.js (a Set, not a single reference, because two sources
 *    can legitimately overlap).
 * 2. Clicking stop halts every currently playing source, disables the
 *    button IMMEDIATELY (before the async onended callbacks fire), and
 *    clears the speaking highlight when the source's onended lands.
 * 3. "Stop" never discards audio: items already fetched or still queued
 *    are persisted to disk and get their replay button under the correct
 *    bubble — they just never play (and never paint a highlight). This is
 *    checked for both playback queues: the non-streaming audioQueue and
 *    the streaming pipeline (processTTSRequests -> audioBufferQueue).
 * 4. The streaming TTS fetch pipeline is untouched by the mute: every
 *    /api/tts request goes out and every response is persisted, sentence
 *    by sentence.
 * 5. The mute does not persist: it is cleared by the next user prompt
 *    (exercised through the REAL sendMessage() with a stubbed /api/chat
 *    SSE stream) and by a manual replay click — and an item whose
 *    playback start falls after the mute lifts plays normally.
 *
 * How it works: the browser scripts share globals (no ES modules), so
 * each test evaluates them in a fresh vm.Context against a minimal DOM
 * stub, a fake AudioContext whose sources end on a timer AND can be
 * stopped (stop() cancels the timer and fires onended asynchronously,
 * mirroring the Web Audio spec closely enough), and stubbed
 * fetch/uploadAudio/getAudioUrl.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_stop_button.js
 */

"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
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
 * touch: classList (with a className accessor that stays in sync),
 * dataset, children with parent tracking (append/insertBefore/remove/
 * replaceWith), and querySelector — backed by an explicit link map for
 * attribute selectors and a recursive descendant scan for simple
 * ".class" selectors, mirroring what the real DOM's querySelector does.
 */
function makeFakeElement(id) {
    const classSet = new Set();
    const childSelectors = new Map(); // querySelector arg -> element
    const toggleHistory = [];          // [{ cls, want }, ...] in call order

    const el = {
        id,
        textContent: "",
        value: "",
        disabled: false,
        style: {},
        dataset: {},
        children: [],
        parent: null,
        scrollTop: 0,
        scrollHeight: 0,
        addEventListener() {},
        dispatchEvent() {},
        setAttribute() {},
        focus() {},
        appendChild(child) {
            child.parent = el;
            el.children.push(child);
            return child;
        },
        insertBefore(child, ref) {
            const i = el.children.indexOf(ref);
            if (i === -1) el.children.push(child);
            else el.children.splice(i, 0, child);
            child.parent = el;
            return child;
        },
        remove() {
            if (el.parent) {
                const i = el.parent.children.indexOf(el);
                if (i !== -1) el.parent.children.splice(i, 1);
                el.parent = null;
            }
        },
        replaceWith(newEl) {
            if (!el.parent) return newEl;
            const i = el.parent.children.indexOf(el);
            if (i === -1) return newEl;
            el.parent.children[i] = newEl;
            newEl.parent = el.parent;
            el.parent = null;
            return newEl;
        },
        querySelector(sel) {
            // Explicit link map first: attribute selectors such as
            // .message-row[data-message-id="..."] have no scan fallback.
            const linked = childSelectors.get(sel);
            if (linked) return linked;
            // Simple .class selectors: recursive descendant scan, like the
            // real DOM's querySelector (the context element itself is
            // never a candidate).
            if (sel.startsWith(".")) {
                const cls = sel.slice(1);
                const find = (node) => {
                    if (node.classList && node.classList.contains(cls)) return node;
                    for (const child of node.children) {
                        const found = find(child);
                        if (found) return found;
                    }
                    return null;
                };
                for (const child of el.children) {
                    const found = find(child);
                    if (found) return found;
                }
            }
            return null;
        },
        // Test helper: what querySelector(sel) should return.
        linkChildSelector(sel, child) {
            childSelectors.set(sel, child);
        },
        toggleHistory,
    };

    Object.defineProperty(el, "className", {
        get: () => [...classSet].join(" "),
        set: (v) => {
            classSet.clear();
            String(v).split(/\s+/).filter(Boolean).forEach((c) => classSet.add(c));
        },
    });

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
 * ends after ctx.sourceDelayMs UNLESS stopped: stop() cancels the natural
 * end and fires onended on a later tick, exactly like the spec requires
 * for the stop-button flow (onended is the single cleanup path).
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
                ended: false,
                stopped: false,
                onended: null,
                _endTimer: null,
                connect() {
                    this.connected = true;
                },
                start() {
                    this.started = true;
                    // End on a later tick so tests can observe the playing
                    // window between start and onended.
                    this._endTimer = setTimeout(() => {
                        source.ended = true;
                        if (source.onended) source.onended();
                    }, ctx.sourceDelayMs);
                },
                stop() {
                    // Web Audio: stop() is legal only after start(), and
                    // onended fires asynchronously (never synchronously).
                    if (!this.started) {
                        throw new Error("InvalidStateError: stop() before start()");
                    }
                    if (this._endTimer !== null) {
                        clearTimeout(this._endTimer);
                        this._endTimer = null;
                    }
                    if (this.ended) return;
                    this.stopped = true;
                    setTimeout(() => {
                        source.ended = true;
                        if (source.onended) source.onended();
                    }, 0);
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
 * Canned /api/chat SSE response: every event is emitted in one chunk,
 * matching the wire format the real server uses ("data: <JSON>\n\n").
 */
function sseResponse(events) {
    const payload = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
    return {
        ok: true,
        status: 200,
        body: {
            getReader() {
                let sent = false;
                return {
                    read: async () => {
                        if (!sent) {
                            sent = true;
                            return { done: false, value: new TextEncoder().encode(payload) };
                        }
                        return { done: true, value: undefined };
                    },
                };
            },
        },
    };
}

/**
 * Build the sandbox for one test: fresh DOM elements, a fake AudioContext,
 * a fetch stub with per-URL routes, and stubs for the persistence.js /
 * browser globals the loaded scripts rely on (uploadAudio, getAudioUrl,
 * requestAnimationFrame, atob, crypto, TextDecoder, window.AudioContext).
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
        querySelector: (sel) => documentStub.querySelectorRoutes.get(sel) || null,
        querySelectorAll: () => [],
        querySelectorRoutes: new Map(),
    };

    // getWhoAnswers() (chat.js) always queries the checked "who answers"
    // radio; point it at "random" so the real sendMessage() runs without
    // persona.js (whose sidebar helpers it would otherwise hit).
    const whoRadio = makeFakeElement("who-radio");
    whoRadio.value = "random";
    documentStub.querySelectorRoutes.set('input[name="who_answers"]:checked', whoRadio);

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
        // sendMessage() generates the user message ID with generateMessageId()
        // (utils.js), which uses crypto.randomUUID() when it exists.
        crypto,
        TextDecoder,
        TextEncoder,
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
    // The stop button ships with the `disabled` attribute in
    // templates/index.html (no audio plays at page load); mirror that
    // initial DOM state.
    vm.runInContext("stopAudioBtn.disabled = true", sandbox);

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
    // the (stubbed) upload resolves. It is also pushed into the row's
    // children so countAudioButtons' descendant scan can reach the
    // injected .message-audio containers.
    const content = makeFakeElement(`content-${messageId}`);
    content.className = "bubble-content";
    content.parent = row;
    row.children.push(content);
    row.linkChildSelector(".bubble-content", content);
    h.messagesEl.linkChildSelector(`.message-row[data-message-id="${messageId}"]`, row);
    h.messagesEl.linkChildSelector(`.message-row.assistant[data-message-id="${messageId}"]`, row);
    h.messagesEl.linkChildSelector(`.message-row.user[data-message-id="${messageId}"]`, row);
    return row;
}

/**
 * Count the replay buttons injected under a row's bubble (the app puts
 * them in .message-audio containers inside the row's content).
 */
function countAudioButtons(row) {
    const containers = [];
    const scan = (node) => {
        for (const child of node.children) {
            if (child.classList && child.classList.contains("message-audio")) {
                containers.push(child);
            }
            scan(child);
        }
    };
    scan(row);
    return containers.reduce(
        (n, c) =>
            n +
            c.children.filter(
                (b) => b.classList && b.classList.contains("audio-play-btn")
            ).length,
        0
    );
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

/**
 * One persona in the room, TTS on, and the "start" event's persona
 * pre-selected in the sidebar — so handleSSEEvent() never reaches
 * highlightSelectedPersona() (persona.js is not loaded here).
 */
function seedChatState(h) {
    h.run("personas = [{ name: 'Al', tts_capable: true, avatar_color: '#f00' }]");
    h.run("selectedPersona = 'Al'");
    h.run("roomPersonas = { default: ['Al'] }");
    h.run("ttsEnabled = true");
}

/* ==========================================================================
    Tests
    ========================================================================== */

test("stop button is disabled while idle and enabled while a source plays", async () => {
    const h = createHarness();
    const row = linkRow(h, "M1");
    h.fetchStub.routes.set("/api/tts", ttsResponse);

    // GIVEN no audio source has ever started,
    assert.equal(h.get("stopAudioBtn.disabled"), true, "idle: stop button starts disabled");

    // WHEN a TTS item is enqueued and its source starts playing,
    h.run("currentAssistantMessageId = 'M1'");
    h.run("enqueueTTS('Al', 'Hello.')");
    await waitUntil(() => h.audioCtx.sources.length === 1);

    // THEN the button is enabled for the duration of playback...
    assert.equal(h.get("stopAudioBtn.disabled"), false, "playing: stop button enabled");

    // ...and disabled again once the source ends.
    await waitUntil(() => h.audioCtx.sources.every((s) => s.ended));
    assert.equal(h.get("stopAudioBtn.disabled"), true, "idle again: stop button disabled");
    assert.ok(row.classList.contains("speaking") === false);
});

test("stop halts the playing source, disables the button immediately, and clears the highlight", async () => {
    const h = createHarness();
    const row = linkRow(h, "M1");
    h.fetchStub.routes.set("/api/tts", ttsResponse);

    h.run("currentAssistantMessageId = 'M1'");
    h.run("enqueueTTS('Al', 'Hello there.')");

    // WHILE the audio plays: button enabled, row highlighted.
    await waitUntil(() => row.classList.contains("speaking"));
    assert.equal(h.get("stopAudioBtn.disabled"), false);

    // WHEN the user clicks stop,
    h.sandbox.stopAudioPlayback();

    // THEN the button is disabled IMMEDIATELY (onended has not fired yet —
    // it is scheduled on a later tick), the mute is engaged, and the
    // playing source is stopped.
    assert.equal(h.get("stopAudioBtn.disabled"), true, "button must disable on the click");
    assert.equal(h.get("audioPlaybackStopped"), true);
    const source = h.audioCtx.sources[0];
    assert.equal(source.stopped, true, "the playing source must be stopped");

    // Once the source's onended lands, the highlight clears and the
    // refcount is clean.
    await waitUntil(() => !row.classList.contains("speaking"));
    assert.equal(h.get("speakingMessageIds.size"), 0);

    // The queue is drained and no further source may start.
    await waitUntil(() => h.get("isPlayingAudio") === false);
    assert.equal(h.audioCtx.sources.length, 1, "no second source may start");
});

test("items queued after stop are fetched and persisted (replay buttons) but never played", async () => {
    const h = createHarness();
    const row1 = linkRow(h, "OLD1");
    const row2 = linkRow(h, "OLD2");
    h.fetchStub.routes.set("/api/tts", ttsResponse);

    // First item starts playing.
    h.run("currentAssistantMessageId = 'OLD1'");
    h.run("enqueueTTS('Al', 'One.')");
    await waitUntil(() => h.audioCtx.sources.length === 1);

    // Stop, then queue a second item (the doc's "subsequently queued").
    h.sandbox.stopAudioPlayback();
    h.run("audioQueue.push({ personaName: 'Al', text: 'Two.', messageId: 'OLD2' })");

    // The second item is still fetched and persisted to disk, and its
    // replay button lands under the correct bubble...
    await waitUntil(() => h.uploadAudioCalls.some((c) => c.messageId === "OLD2"));
    assert.ok(h.uploadAudioCalls.some((c) => c.messageId === "OLD1"));
    await waitUntil(() => countAudioButtons(row2) === 1);
    assert.equal(countAudioButtons(row1), 1);

    // ...but it is never played, its row is never highlighted, and the
    // queue drains.
    await sleep(300); // well past the item's 100 ms queue slot
    assert.equal(h.audioCtx.sources.length, 1, "muted item must not start a source");
    assert.ok(!row2.classList.contains("speaking"));
    assert.equal(h.get("audioQueue.length"), 0);
    assert.equal(h.get("isPlayingAudio"), false);
    assert.equal(h.get("audioPlaybackStopped"), true);
});

test("streaming pipeline keeps fetching and persisting while muted; no further playback", async () => {
    const h = createHarness();
    const rowA = linkRow(h, "A");
    const rowB = linkRow(h, "B");
    h.fetchStub.routes.set("/api/tts", ttsResponse);

    // Three sentences enter the fetch pipeline the way the "token" handler
    // would enqueue them mid-stream: two for message A, one for message B.
    h.run(
        "ttsRequestQueue.push({ personaName: 'Al', text: 'One.', messageId: 'A' });" +
        "ttsRequestQueue.push({ personaName: 'Al', text: 'Two.', messageId: 'A' });" +
        "ttsRequestQueue.push({ personaName: 'Bo', text: 'Three.', messageId: 'B' });" +
        "processTTSRequests();"
    );

    // Sentence 1 (A) is playing.
    await waitUntil(() => rowA.classList.contains("speaking"));

    // Stop mid-playback of the first sentence.
    h.sandbox.stopAudioPlayback();
    assert.equal(h.get("stopAudioBtn.disabled"), true);
    assert.equal(h.get("audioPlaybackStopped"), true);

    // The TTS requests for the remaining sentences still go out and every
    // sentence's audio is persisted (the doc: "TTS requests and responses
    // continue!").
    await waitUntil(() => h.uploadAudioCalls.length === 3);
    assert.deepEqual(h.uploadAudioCalls.map((c) => c.messageId), ["A", "A", "B"]);

    // Replay buttons land on the right bubbles: two under A, one under B.
    await waitUntil(() => countAudioButtons(rowA) === 2 && countAudioButtons(rowB) === 1);

    // But no further audio plays: exactly one source was ever started —
    // the first sentence's — and it is the one that was stopped.
    await sleep(200);
    assert.equal(h.audioCtx.sources.length, 1, "only the first sentence may start a source");
    assert.equal(h.audioCtx.sources[0].stopped, true);
    assert.ok(!rowA.classList.contains("speaking"), "A's highlight must clear");
    assert.ok(!rowB.classList.contains("speaking"), "B must never be highlighted");

    // The buffer queue drained completely, the fetch pipeline is idle.
    assert.equal(h.get("audioBufferQueue.length"), 0);
    assert.equal(h.get("isFetchingTTS"), false);
    assert.equal(h.get("isPlayingAudioBuffer"), false);
});

test("a new user prompt clears the mute: the new reply plays, the interrupted audio is not auto-replayed", async () => {
    const h = createHarness();
    seedChatState(h);
    const rowOld = linkRow(h, "OLD1");
    const rowNew = linkRow(h, "NEW1");
    h.fetchStub.routes.set("/api/tts", ttsResponse);
    h.fetchStub.routes.set("/api/chat", () =>
        sseResponse([
            { type: "start", persona: "Al", message_id: "NEW1", user_message_id: "U1" },
            { type: "token", token: "Hi " },
            { type: "token", token: "there." },
            { type: "done", persona: "Al", text: "Hi there." },
            { type: "complete" },
        ])
    );

    // A reply from the previous turn is playing.
    h.run("currentAssistantMessageId = 'OLD1'");
    h.run("enqueueTTS('Al', 'Old reply.')");
    await waitUntil(() => h.audioCtx.sources.length === 1);

    // The user stops it.
    h.sandbox.stopAudioPlayback();
    assert.equal(h.get("audioPlaybackStopped"), true);
    await waitUntil(() => !rowOld.classList.contains("speaking"));

    // The user issues a new prompt (the real sendMessage() against the
    // stubbed SSE stream).
    h.elementById("message-input").value = "hello";
    h.sandbox.sendMessage();

    // The mute clears immediately, before the fetch even starts...
    assert.equal(
        h.get("audioPlaybackStopped"),
        false,
        "a sent user prompt must clear the mute"
    );

    // ...and the new reply's audio plays (enqueued on the "done" event).
    await waitUntil(() => h.audioCtx.sources.length === 2);
    await waitUntil(() => rowNew.classList.contains("speaking"));

    // The interrupted previous-turn audio is NOT auto-replayed — it was
    // stopped mid-play — but it was persisted and has its replay button.
    await sleep(150); // past the queue's 100 ms re-entry delay
    assert.equal(h.audioCtx.sources.length, 2, "only the new reply may play");
    assert.deepEqual(
        h.uploadAudioCalls.map((c) => c.messageId).sort(),
        ["NEW1", "OLD1"]
    );
    assert.equal(countAudioButtons(rowOld), 1);
    assert.equal(countAudioButtons(rowNew), 1);
});

test("a manual replay click clears the mute and the clicked replay plays", async () => {
    const h = createHarness();
    const rowP1 = linkRow(h, "P1");
    const rowP2 = linkRow(h, "P2");
    h.fetchStub.routes.set("/api/tts", ttsResponse);
    h.fetchStub.routes.set("/api/persist/audio/default/p2.wav", audioFileResponse);

    // Engage the mute: play P1, then stop.
    h.run("currentAssistantMessageId = 'P1'");
    h.run("enqueueTTS('Al', 'One.')");
    await waitUntil(() => h.audioCtx.sources.length === 1);
    h.sandbox.stopAudioPlayback();
    assert.equal(h.get("audioPlaybackStopped"), true);

    // WHEN the user clicks a replay button under another bubble,
    h.sandbox.playPersistedAudio("default", "p2.wav", rowP2);

    // THEN the mute clears and the clicked replay actually plays —
    // the highlight and the stop button follow it.
    assert.equal(h.get("audioPlaybackStopped"), false, "replay click must clear the mute");
    await waitUntil(() => h.audioCtx.sources.length === 2);
    await waitUntil(() => rowP2.classList.contains("speaking"));
    assert.equal(h.get("stopAudioBtn.disabled"), false, "playing again: stop button enabled");

    // The first (stopped) source is untouched.
    assert.equal(h.audioCtx.sources[0].stopped, true);
});

test("the mute lifted mid-drain resumes playback on the very next queued item", async () => {
    const h = createHarness();
    const row1 = linkRow(h, "Q1");
    const row2 = linkRow(h, "Q2");
    const row3 = linkRow(h, "Q3");
    const rowR = linkRow(h, "R");
    h.fetchStub.routes.set("/api/tts", ttsResponse);
    h.fetchStub.routes.set("/api/persist/audio/default/r.wav", audioFileResponse);

    // Q1 is playing when the user stops.
    h.run("currentAssistantMessageId = 'Q1'");
    h.run("enqueueTTS('Al', 'One.')");
    await waitUntil(() => h.audioCtx.sources.length === 1);
    h.sandbox.stopAudioPlayback();

    // Two more items are queued while muted; they drain through the queue
    // one per 100 ms slot, fetched + persisted but never played.
    h.run("audioQueue.push({ personaName: 'Al', text: 'Two.', messageId: 'Q2' })");
    h.run("audioQueue.push({ personaName: 'Al', text: 'Three.', messageId: 'Q3' })");
    await waitUntil(() => h.uploadAudioCalls.some((c) => c.messageId === "Q2"));

    // Q2's slot has just finished (fetched, skipped). Q3's slot is ~100 ms
    // away. Lift the mute NOW, between the two slots: a replay click.
    h.sandbox.playPersistedAudio("default", "r.wav", rowR);
    assert.equal(h.get("audioPlaybackStopped"), false);

    // The clicked replay plays and highlights its row — poll this BEFORE
    // waiting for Q3: the fake replay source ends after 50 ms, and Q3's
    // slot is ~100 ms out, so the highlight window is already closed by
    // then.
    await waitUntil(() => rowR.classList.contains("speaking"));

    // Q3's playback start falls after the lift, so it plays too.
    // Sources: Q1 (stopped), replay, Q3.
    await waitUntil(() => h.audioCtx.sources.length === 3);
    await waitUntil(() => row3.classList.contains("speaking"));

    // Q2 stays unplayed (its playback start happened while muted) but was
    // persisted and got its replay button, like everything else.
    await sleep(150);
    assert.equal(h.audioCtx.sources.length, 3, "no further sources may start");
    assert.ok(!row2.classList.contains("speaking"));
    assert.deepEqual(
        h.uploadAudioCalls.map((c) => c.messageId).sort(),
        ["Q1", "Q2", "Q3"]
    );
    assert.equal(countAudioButtons(row1), 1);
    assert.equal(countAudioButtons(row2), 1);
    assert.equal(countAudioButtons(row3), 1);
});
