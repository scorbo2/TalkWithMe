/**
 * test_persona_picker.js — Regression tests for the persona picker modal
 * (static/chatrooms.js — the "add personas to a room" dialog).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_persona_picker.js
 *
 * The invariant under test: the picker's avatar bubbles follow the SAME
 * rule as the sidebar and the Persona Editor list — the persona's avatar
 * image (served by the dedicated avatar endpoint) when the persona has
 * one, and the colored first-initial bubble ONLY as a fallback: no image
 * on the server, or a stale presence flag whose endpoint errors once the
 * browser requests the file. All three lists share renderPersonaAvatar()
 * in static/utils.js, so the picker can never drift back to the old
 * always-initial behavior.
 *
 * How it works: chatrooms.js and its dependencies are browser scripts
 * that share globals (no ES modules), so each test evaluates them in a
 * fresh vm.Context against a minimal DOM stub. renderPersonaPickerList()
 * is synchronous and reads the shared personas / roomPersonas /
 * currentChatRoom globals — the tests set those directly in the context.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_persona_picker.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html: utils before state before chatrooms.
const APP_SCRIPTS = ["utils.js", "state.js", "chatrooms.js"];

/* ==========================================================================
    DOM / browser stubs
    ========================================================================== */

/**
 * Minimal stand-in for an HTMLElement. Implements only what the picker
 * rendering touches: classList, event listeners, appendChild/children,
 * dataset, and an innerHTML setter that clears children on "" (like the
 * real DOM).
 */
function makeFakeElement(id) {
    const listeners = new Map(); // event type -> [listener, ...]
    const classSet = new Set();
    let innerHtml = "";

    const el = {
        id,
        checked: false,
        textContent: "",
        style: {},
        dataset: {},
        children: [],
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        // Test helper: fire the listeners registered for `type`.
        dispatch(type, event) {
            for (const fn of listeners.get(type) || []) fn(event);
        },
        appendChild(child) {
            el.children.push(child);
            return child;
        },
    };

    Object.defineProperty(el, "innerHTML", {
        get: () => innerHtml,
        // Real DOM clears child nodes when innerHTML is set to "".
        set: (v) => {
            innerHtml = v;
            if (v === "") el.children.length = 0;
        },
        enumerable: true,
    });

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

/**
 * Build the sandbox for one test: fresh DOM elements (auto-created per id,
 * as state.js grabs its element references at load time) and the app
 * scripts evaluated in load order.
 */
function createPickerHarness() {
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

    const sandbox = {
        console,
        document: documentStub,
        fetch: async () => {
            throw new Error("the picker list must not hit the network");
        },
    };

    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        const code = fs.readFileSync(path.join(STATIC_DIR, file), "utf8");
        vm.runInContext(code, sandbox, { filename: file });
    }

    return {
        sandbox,
        elementById,
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

/** Persona-summary fixture matching PersonaResponse in app/models.py. */
function summaryFixture({ name = "Al", avatarImage = false } = {}) {
    return {
        name,
        description: "A test persona",
        avatar_color: "#123456",
        allow_tool_calls: false,
        tts_capable: false,
        avatar_image: avatarImage,
    };
}

/**
 * Seed the shared globals and render the picker list. The picker reads
 * personas / roomPersonas / currentChatRoom from state.js — they are
 * `let` bindings inside the vm context, so they are set via run().
 */
function renderPickerList(h, personas, { room = "Lounge", inRoom = [] } = {}) {
    h.run(`personas = ${JSON.stringify(personas)}`);
    h.run(`currentChatRoom = ${JSON.stringify(room)}`);
    h.run(`roomPersonas = ${JSON.stringify({ [room]: inRoom })}`);
    h.sandbox.renderPersonaPickerList();
    return h.elementById("pp-list");
}

/** The avatar bubble div of the index-th rendered pp-list-item. */
function avatarBubbleOf(ppListEl, index) {
    const item = ppListEl.children[index];
    const avatar = item.children.find((c) => c.className === "pp-list-item-avatar");
    assert.ok(avatar, "pp-list-item has no .pp-list-item-avatar child");
    return avatar;
}

/* ==========================================================================
    Tests
    ========================================================================== */

test("renderPersonaPickerList_personaWithAvatarImage_showsImageBubbleNotInitial", async () => {
    // GIVEN one persona whose summary reports an avatar image on the server:
    const h = createPickerHarness();

    // WHEN the picker list renders:
    const ppListEl = renderPickerList(h, [summaryFixture({ name: "Ada Lovelace", avatarImage: true })]);

    // THEN the bubble holds an <img> aimed at the avatar endpoint (name
    // percent-encoded), and NO initial text competes with the image:
    const avatar = avatarBubbleOf(ppListEl, 0);
    assert.equal(avatar.children.length, 1, "avatar bubble should contain exactly the image");
    const img = avatar.children[0];
    assert.equal(img.tagName, "img");
    assert.equal(img.src, "/api/personas/Ada%20Lovelace/avatar");
    assert.equal(img.alt, "Ada Lovelace");
    assert.equal(avatar.textContent, "", "initial text must not be rendered alongside the image");
});

test("renderPersonaPickerList_personaWithoutAvatarImage_showsColoredInitialBubble", async () => {
    // GIVEN one persona with no avatar image on the server:
    const h = createPickerHarness();

    // WHEN the picker list renders:
    const ppListEl = renderPickerList(h, [summaryFixture({ name: "Ada Lovelace", avatarImage: false })]);

    // THEN the bubble is the fallback: uppercase initial on the persona's
    // configured color, and no image element:
    const avatar = avatarBubbleOf(ppListEl, 0);
    assert.equal(avatar.children.length, 0, "no <img> for a persona without an avatar image");
    assert.equal(avatar.textContent, "A");
    assert.equal(avatar.style.backgroundColor, "#123456");
});

test("renderPersonaPickerList_avatarImageFailsToLoad_fallsBackToInitialBubble", async () => {
    // GIVEN a persona whose presence flag is stale — the avatar endpoint
    // answers with an error once the browser actually requests the file:
    const h = createPickerHarness();
    const ppListEl = renderPickerList(h, [summaryFixture({ name: "Ada Lovelace", avatarImage: true })]);
    const avatar = avatarBubbleOf(ppListEl, 0);
    assert.equal(avatar.children[0].tagName, "img");

    // WHEN the image fails to load (onerror fires):
    avatar.children[0].onerror();

    // THEN the bubble degrades to the initial instead of a broken image:
    assert.equal(avatar.children.length, 0, "failed image must be removed from the bubble");
    assert.equal(avatar.textContent, "A");
});

test("renderPersonaPickerList_personaAlreadyInRoom_stillShowsItsAvatarImage", async () => {
    // The "(already in room)" annotation must not change the avatar rule:
    // an in-room persona with an image still gets the image bubble.
    const h = createPickerHarness();

    const ppListEl = renderPickerList(
        h,
        [summaryFixture({ name: "Ada Lovelace", avatarImage: true })],
        { inRoom: ["Ada Lovelace"] },
    );

    const avatar = avatarBubbleOf(ppListEl, 0);
    assert.equal(avatar.children.length, 1, "avatar bubble should contain exactly the image");
    assert.equal(avatar.children[0].tagName, "img");
    assert.equal(avatar.textContent, "");
});
