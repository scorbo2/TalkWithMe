/**
 * test_expressive_tts.js — Frontend side of expressive speech
 * (general.expressive_speech): static/utils.js + static/tts.js.
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_expressive_tts.js
 *
 * Invariants under test:
 *
 * 1. stripDirectionTags() hides every {direction} tag in a bubble,
 *    including a half-streamed one at the end, and leaves vocal events
 *    like (laugh) visible.
 * 2. extractSentences() never ends a sentence on punctuation inside a
 *    {direction} tag, closed or still streaming in.
 * 3. A direction carries on: each streamed sentence is sent with the
 *    tag it starts with, else the one in force from an earlier sentence
 *    of the same reply. A sentence that is only a tag is not sent.
 * 4. With the feature off, nothing changes: no instruction is sent and
 *    the text goes out as the LLM wrote it.
 *
 * Same approach as test_speaking_highlight.js: the browser scripts share
 * globals, so they are evaluated in a fresh vm.Context per test against a
 * DOM stub that hands out inert elements, with a fetch stub recording the
 * /api/tts request bodies.
 *
 * NOTE: intentionally NOT part of the pytest suite (which must run with
 * nothing but Python installed).
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html.
const APP_SCRIPTS = ["state.js", "utils.js", "tts.js"];

function makeInertElement() {
    return {
        textContent: "",
        value: "",
        disabled: false,
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
        addEventListener() {},
        querySelector: () => null,
        querySelectorAll: () => [],
        appendChild() {},
    };
}

function createHarness() {
    const fetchCalls = [];
    const sandbox = {
        console,
        document: {
            getElementById: () => makeInertElement(),
            createElement: () => makeInertElement(),
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        // The TTS request itself is all we look at; failing it keeps the
        // pipeline from decoding or persisting anything.
        fetch: async (url, options = {}) => {
            fetchCalls.push({ url: String(url), body: options.body ? JSON.parse(options.body) : null });
            return { ok: false, status: 503, json: async () => ({}) };
        },
        setTimeout,
        clearTimeout,
        requestAnimationFrame: () => 0,
        window: {},
    };
    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, file), "utf8"), sandbox, { filename: file });
    }
    return {
        fetchCalls,
        get: (expression) => vm.runInContext(`(() => (${expression}))()`, sandbox),
        run: (statement) => vm.runInContext(statement, sandbox),
    };
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForCalls(h, count, timeoutMs = 2000) {
    const deadline = Date.now() + timeoutMs;
    while (h.fetchCalls.length < count) {
        if (Date.now() > deadline) throw new Error(`expected ${count} TTS calls, got ${h.fetchCalls.length}`);
        await sleep(5);
    }
    await sleep(20); // and nothing more arrives
}

/** Stream a reply token by token through the real accumulator + final flush. */
function streamReply(h, reply) {
    h.run(`currentAssistantMessageId = "msg-1"; sentenceBuffer = ""; streamingDirection = null;`);
    for (const ch of reply) {
        h.run(`accumulateForTTS(${JSON.stringify(ch)}, "Robot")`);
    }
    h.run(`if (sentenceBuffer.trim()) enqueueStreamingTTS("Robot", sentenceBuffer.trim()); sentenceBuffer = "";`);
}

/* ==========================================================================
    1. Bubble text
    ========================================================================== */

test("stripDirectionTags hides tags and keeps events", () => {
    const h = createHarness();
    const strip = (t) => h.get(`stripDirectionTags(${JSON.stringify(t)})`);

    assert.equal(strip("{coldly, slowly} Well (laugh), that went well."), "Well (laugh), that went well.");
    assert.equal(strip("Fine. {angry} Now listen."), "Fine. Now listen.");
    assert.equal(strip("Fine {sad}."), "Fine.");
    assert.equal(strip("No tags here."), "No tags here.");
});

test("an event in braces is shown as an event", () => {
    const h = createHarness();
    assert.equal(h.get(`stripDirectionTags("{sigh} Fine. {coldly} Go.")`), "(sigh) Fine. Go.");
});

test("braces with only sounds show as sounds, mixed ones stay directions", () => {
    const h = createHarness();
    const strip = (t) => h.get(`stripDirectionTags(${JSON.stringify(t)})`);

    assert.equal(strip("{cough, sighing} Fine."), "(cough) (sigh) Fine.");
    assert.equal(strip("{pause, then coldly} Fine."), "Fine.");
    assert.deepEqual({ ...h.get(`findDirectionTags("{cough, sigh} A. {pause, then coldly} B.")`) },
        { first: "pause, then coldly", last: "pause, then coldly" });
});

test("a half-streamed tag never shows up", () => {
    const h = createHarness();
    assert.equal(h.get(`stripDirectionTags("Fine. {whisp")`), "Fine. ");
});

/* ==========================================================================
    2. Sentence splitting
    ========================================================================== */

test("punctuation inside a tag does not end a sentence", () => {
    const h = createHarness();
    const result = h.get(`extractSentences("{slowly. coldly!} Hello there. Next")`);

    assert.deepEqual([...result.sentences], ["{slowly. coldly!} Hello there."]);
    assert.equal(result.remaining, " Next");
});

test("an unclosed tag holds the split back until it closes", () => {
    const h = createHarness();
    const result = h.get(`extractSentences("Done. {very. slow")`);

    assert.deepEqual([...result.sentences], ["Done."]);
    assert.equal(result.remaining, " {very. slow");
});

/* ==========================================================================
    3./4. Directions on the TTS requests
    ========================================================================== */

test("a direction applies to its sentence and carries on until the next tag", async () => {
    const h = createHarness();
    h.run("expressiveSpeechEnabled = true");

    streamReply(h, "{coldly} First. Second (sigh). {shouting} Third! Fourth.");
    await waitForCalls(h, 4);

    const bodies = h.fetchCalls.map((c) => c.body);
    assert.deepEqual(bodies.map((b) => b.text),
        ["{coldly} First.", "Second (sigh).", "{shouting} Third!", "Fourth."]);
    assert.deepEqual(bodies.map((b) => b.instruction),
        ["coldly", "coldly", "shouting", "shouting"]);
    assert.ok(bodies.every((b) => b.persona_name === "Robot"));
});

test("an event in braces neither directs nor carries", async () => {
    const h = createHarness();
    h.run("expressiveSpeechEnabled = true");

    streamReply(h, "{coldly} One. {sigh} Two. Three.");
    await waitForCalls(h, 3);

    assert.deepEqual(h.fetchCalls.map((c) => c.body.instruction), ["coldly", "coldly", "coldly"]);
});

test("no instruction is sent before the first tag", async () => {
    const h = createHarness();
    h.run("expressiveSpeechEnabled = true");

    streamReply(h, "Plain start. {sad} Then sad.");
    await waitForCalls(h, 2);

    assert.equal("instruction" in h.fetchCalls[0].body, false);
    assert.equal(h.fetchCalls[1].body.instruction, "sad");
});

test("a reply ending on a bare tag sends nothing for it", async () => {
    const h = createHarness();
    h.run("expressiveSpeechEnabled = true");

    streamReply(h, "Done. {smug}");
    await waitForCalls(h, 1);

    assert.equal(h.fetchCalls.length, 1);
    assert.equal(h.fetchCalls[0].body.text, "Done.");
});

test("each reply starts without a direction", async () => {
    const h = createHarness();
    h.run("expressiveSpeechEnabled = true");

    streamReply(h, "{coldly} One.");
    streamReply(h, "Two.");
    await waitForCalls(h, 2);

    assert.equal(h.fetchCalls[0].body.instruction, "coldly");
    assert.equal("instruction" in h.fetchCalls[1].body, false);
});

test("feature off: text as written, no instruction", async () => {
    const h = createHarness();

    streamReply(h, "{coldly} First. Second.");
    await waitForCalls(h, 2);

    assert.deepEqual(h.fetchCalls.map((c) => c.body.text), ["{coldly} First.", "Second."]);
    assert.ok(h.fetchCalls.every((c) => !("instruction" in c.body)));
});
