/**
 * test_sentence_split.js — Regression tests for the streaming-TTS sentence
 * splitter (extractSentences / accumulateForTTS in static/tts.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_sentence_split.js
 *
 * Invariants under test:
 *
 * 1. A "." that does not end a sentence never splits one: titles and other
 *    listed abbreviations ("Mrs. Hudson"), initials and dotted forms
 *    ("J. R. R. Tolkien", "e.g.", "U.S."), numbered-list markers ("1. this"),
 *    and decimals ("3.50").
 * 2. A line break always ends a chunk, so each list item is its own
 *    sentence instead of "1." / "this 2." / "that".
 * 3. Real boundaries still split: . ! ? … (closing quotes/brackets stay with
 *    the sentence they close), a number ending a sentence ("in 1999."), and
 *    CJK 。！？.
 * 4. Streaming-safe: feeding the text one character at a time through the
 *    real accumulateForTTS() (then flushing the buffer the way chat.js does
 *    on "done") yields exactly the same sentences as splitting the whole
 *    text — token boundaries never change where a split happens.
 *
 * How it works: the browser scripts share globals (no ES modules), so each
 * test evaluates state.js + utils.js + tts.js in a fresh vm.Context with a
 * stub document (state.js looks up DOM elements at load). The real
 * extractSentences() and accumulateForTTS() run; enqueueStreamingTTS() is
 * replaced with a recorder so no fetch/audio pipeline is involved.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_sentence_split.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html: state, utils, tts.
const APP_SCRIPTS = ["state.js", "utils.js", "tts.js"];

function createContext() {
    const ctx = vm.createContext({
        console,
        document: { getElementById: () => ({}) },
    });
    for (const name of APP_SCRIPTS) {
        vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, name), "utf8"), ctx, { filename: name });
    }
    return ctx;
}

/**
 * extractSentences() in the vm context, with its array copied into this realm:
 * assert.deepEqual rejects arrays whose prototype comes from another context.
 */
function extract(ctx, text) {
    const { sentences, remaining } = ctx.extractSentences(text);
    return { sentences: Array.from(sentences), remaining };
}

/** Split `text` in one go, flushing the remainder like chat.js does on "done". */
function splitWhole(text) {
    const { sentences, remaining } = extract(createContext(), text);
    return remaining.trim() ? [...sentences, remaining.trim()] : sentences;
}

/** Stream `text` one character per token through the real accumulateForTTS(). */
function splitStreamed(text) {
    const ctx = createContext();
    const queued = [];
    ctx.enqueueStreamingTTS = (_persona, sentence) => queued.push(sentence);
    for (const ch of text) ctx.accumulateForTTS(ch, "Alex");
    const remaining = vm.runInContext("sentenceBuffer", ctx).trim();
    if (remaining) queued.push(remaining);
    return queued;
}

const CASES = [
    {
        name: "titles do not split (the Mrs. Hudson bug)",
        text: "Mrs. Hudson opened the door. Dr. Watson and Mr. Holmes were out.",
        want: ["Mrs. Hudson opened the door.", "Dr. Watson and Mr. Holmes were out."],
    },
    {
        name: "numbered list: one chunk per item (the '1.' / 'this 2.' bug)",
        text: "Here are two options:\n1. this\n2. that",
        want: ["Here are two options:", "1. this", "2. that"],
    },
    {
        name: "numbered list items with their own sentences",
        text: "Steps:\n1. Open the lid. Carefully!\n2. Pour it in.\n",
        want: ["Steps:", "1. Open the lid.", "Carefully!", "2. Pour it in."],
    },
    {
        name: "bullet list without punctuation",
        text: "You need:\n- flour\n- eggs\nThat is all.",
        want: ["You need:", "- flour", "- eggs", "That is all."],
    },
    {
        name: "initials and dotted abbreviations",
        text: "J. R. R. Tolkien lived in the U.K. for years. Fruit, e.g. apples, i.e. food.",
        want: ["J. R. R. Tolkien lived in the U.K. for years.", "Fruit, e.g. apples, i.e. food."],
    },
    {
        name: "other listed abbreviations",
        text: "It was us vs. them, etc. and so on. See fig. 3 on St. Mary's page.",
        want: ["It was us vs. them, etc. and so on.", "See fig. 3 on St. Mary's page."],
    },
    {
        name: "decimals and versions are never cut",
        text: "It costs 3.50 dollars in v1.2. Cheap!",
        want: ["It costs 3.50 dollars in v1.2.", "Cheap!"],
    },
    {
        name: "a number can still end a sentence",
        text: "It happened in 1999. Then more happened.",
        want: ["It happened in 1999.", "Then more happened."],
    },
    {
        name: "closing quotes and brackets stay with their sentence",
        text: "He said \"hi.\" Then he left (quietly.) The end.",
        want: ["He said \"hi.\"", "Then he left (quietly.)", "The end."],
    },
    {
        name: "! ? and ellipses split",
        text: "Wait... what? Yes! Okay… fine.",
        want: ["Wait...", "what?", "Yes!", "Okay…", "fine."],
    },
    {
        name: "CJK terminal punctuation splits without spaces",
        text: "你好。今天好吗？很好！",
        want: ["你好。", "今天好吗？", "很好！"],
    },
];

for (const c of CASES) {
    test(`${c.name} (whole text)`, () => {
        assert.deepEqual(splitWhole(c.text), c.want);
    });
    test(`${c.name} (streamed one char at a time)`, () => {
        assert.deepEqual(splitStreamed(c.text), c.want);
    });
}

test("a trailing '.' is held back until the next token decides", () => {
    const ctx = createContext();
    const held = extract(ctx, "Say hi to Mrs.");
    assert.deepEqual(held.sentences, []);
    assert.equal(held.remaining, "Say hi to Mrs.");

    const decimal = extract(ctx, "It costs 3.");
    assert.deepEqual(decimal.sentences, []);
    assert.equal(decimal.remaining, "It costs 3.");
});

test("a complete sentence is released as soon as whitespace follows it", () => {
    const ctx = createContext();
    const { sentences, remaining } = extract(ctx, "First one. Second");
    assert.deepEqual(sentences, ["First one."]);
    assert.equal(remaining, " Second");
});
