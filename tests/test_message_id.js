/**
 * test_message_id.js — Regression tests for generateMessageId() in
 * static/utils.js.
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_message_id.js
 *
 * Invariants under test:
 *
 * 1. In a secure context (crypto.randomUUID exists) the browser's own
 *    implementation is used.
 * 2. In an insecure context — the app reached over plain HTTP by IP or
 *    hostname, where browsers leave crypto.randomUUID undefined — IDs are
 *    still valid, unique RFC 4122 v4 UUIDs, built from
 *    crypto.getRandomValues() with the version and variant bits forced.
 * 3. No frontend file calls crypto.randomUUID() directly: that call throws
 *    in an insecure context and broke sending messages (chat.js) and voice
 *    input (stt.js). All message IDs go through generateMessageId().
 *
 * How it works: static/utils.js declares plain globals (no ES modules), so
 * each test evaluates it in a fresh vm.Context with a stub `crypto`.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_message_id.js
 */

"use strict";

const assert = require("node:assert/strict");
const nodeCrypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Load static/utils.js into a fresh context with the given `crypto` global. */
function loadUtils(crypto) {
    const ctx = vm.createContext({ crypto });
    vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, "utils.js"), "utf8"), ctx, { filename: "utils.js" });
    return ctx;
}

/** An insecure-context crypto: getRandomValues() only, no randomUUID(). */
function insecureCrypto(fill) {
    return {
        getRandomValues(arr) {
            if (fill === undefined) return nodeCrypto.getRandomValues(arr);
            arr.fill(fill);
            return arr;
        },
    };
}

test("uses crypto.randomUUID() when the browser provides it", () => {
    const ctx = loadUtils({
        randomUUID: () => "11111111-2222-4333-8444-555555555555",
        getRandomValues: () => { throw new Error("must not be used"); },
    });
    assert.equal(ctx.generateMessageId(), "11111111-2222-4333-8444-555555555555");
});

test("without crypto.randomUUID() it still returns valid v4 UUIDs", () => {
    const ctx = loadUtils(insecureCrypto());
    for (let i = 0; i < 200; i++) {
        assert.match(ctx.generateMessageId(), UUID_V4_RE);
    }
});

test("the fallback forces the version and variant bits", () => {
    assert.equal(loadUtils(insecureCrypto(0x00)).generateMessageId(),
        "00000000-0000-4000-8000-000000000000");
    assert.equal(loadUtils(insecureCrypto(0xff)).generateMessageId(),
        "ffffffff-ffff-4fff-bfff-ffffffffffff");
});

test("fallback IDs are unique", () => {
    const ctx = loadUtils(insecureCrypto());
    const ids = new Set();
    for (let i = 0; i < 1000; i++) ids.add(ctx.generateMessageId());
    assert.equal(ids.size, 1000);
});

test("no frontend file calls crypto.randomUUID() outside generateMessageId()", () => {
    const offenders = [];
    for (const name of fs.readdirSync(STATIC_DIR).filter((f) => f.endsWith(".js"))) {
        const lines = fs.readFileSync(path.join(STATIC_DIR, name), "utf8").split("\n");
        lines.forEach((line, i) => {
            // Skip comments: JSDoc lines ("* ...", "/** ...") and trailing "//".
            if (/^\s*(\*|\/\*)/.test(line)) return;
            const code = line.replace(/\/\/.*$/, "");
            if (!/crypto\.randomUUID\s*\(/.test(code)) return;
            // The one sanctioned call: inside generateMessageId() in utils.js.
            if (name === "utils.js" && /return crypto\.randomUUID\(\);/.test(code)) return;
            offenders.push(`static/${name}:${i + 1}: ${line.trim()}`);
        });
    }
    assert.deepEqual(offenders, [], "use generateMessageId() instead:\n" + offenders.join("\n"));
});
