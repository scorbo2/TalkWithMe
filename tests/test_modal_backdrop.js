/**
 * test_modal_backdrop.js — Regression tests for closing the app's modals
 * by clicking the dark backdrop (installBackdropClose() in static/utils.js,
 * wired up in persona.js / chatrooms.js / settings.js / gen-settings.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_modal_backdrop.js
 *
 * The bug under test: every modal used
 *
 *     overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
 *
 * Browsers fire `click` on the NEAREST COMMON ANCESTOR of the mousedown
 * target and the mouseup target. Press a checkbox / colour picker / slider
 * / button / file input inside the New Persona form, drag the pointer left
 * onto the backdrop and release: mousedown target = the control, mouseup
 * target = the overlay, so the common ancestor IS the overlay, the test
 * `e.target === overlay` passes, and the whole modal (with the half-typed
 * persona inside it) disappears. Text inputs escape it because a drag that
 * starts in a text field is a selection gesture and no click is synthesised
 * at all — which is why the report read like an unfalsifiable browser
 * gremlin ("it only happens on the checkboxes, sir, I swear").
 *
 * The rule that replaces it: the close decision is made from the PRESS pair
 * (mousedown + mouseup), never from the synthesised click — both ends of
 * the press must land on the backdrop. That also kills the mirror case
 * (press on the backdrop, release inside the modal box).
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_modal_backdrop.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html (only the modules that own a
// modal are needed here). persona.js before chatrooms.js: applyChatRoomFilter()
// calls renderPersonaList().
const APP_SCRIPTS = [
    "utils.js",
    "state.js",
    "persona.js",
    "chatrooms.js",
    "tts-params.js",
    "settings.js",
    "gen-settings.js",
];

/* ==========================================================================
    DOM / browser stubs
    ========================================================================== */

/**
 * Minimal stand-in for an HTMLElement: classList, event listeners with a
 * dispatch helper, appendChild/children, and an innerHTML setter that
 * clears children on "" (like the real DOM).
 */
function makeFakeElement(id) {
    const listeners = new Map(); // event type -> [listener, ...]
    const classSet = new Set();
    let innerHtml = "";

    const el = {
        id,
        checked: false,
        textContent: "",
        value: "",
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
        focus() {},
        appendChild(child) {
            child.parentNode = el;
            el.children.push(child);
            return child;
        },
    };

    Object.defineProperty(el, "innerHTML", {
        get: () => innerHtml,
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
 * as state.js grabs its element references at load time), the app scripts
 * evaluated in load order, and the chatrooms listener wiring that app.js
 * normally performs at startup.
 */
function createModalHarness() {
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

    // closeChatRoomsEditor() reloads the room list; keep every call offline
    // and shaped, so the async tail of a close can never reject.
    const fetchStub = async (url) => {
        fetchStub.calls.push(String(url));
        const u = String(url);
        if (u === "/api/chatrooms/all" || u === "/api/personas") return jsonResponse([]);
        return jsonResponse({});
    };
    fetchStub.calls = [];

    const sandbox = {
        console,
        document: documentStub,
        fetch: fetchStub,
    };

    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        const code = fs.readFileSync(path.join(STATIC_DIR, file), "utf8");
        vm.runInContext(code, sandbox, { filename: file });
    }
    // app.js calls this at startup; the picker/rooms backdrop listeners are
    // registered inside it.
    sandbox.setupChatRoomEventListeners();

    return {
        sandbox,
        elementById,
        fetchStub,
        /**
         * A fake control (checkbox, colour input, button, ...) nested inside
         * the given overlay — parentNode wired, because the position in the
         * tree is exactly what decides where the browser dispatches the
         * synthesised click.
         */
        controlIn(overlayEl, name) {
            const control = makeFakeElement(name);
            control.parentNode = overlayEl;
            return control;
        },
        /** Let any fire-and-forget async tail of a close settle. */
        async settle() {
            for (let i = 0; i < 5; i++) await Promise.resolve();
        },
    };
}

/** Minimal fetch Response stand-in: the app only reads .ok and .json(). */
function jsonResponse(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

/* ==========================================================================
    Test helpers
    ========================================================================== */

/**
 * Every modal the app can close via its backdrop: the element id plus the
 * module that owns it. All six must share installBackdropClose() — the
 * original bug was copy-pasted into each of them.
 */
const MODALS = [
    { id: "persona-editor-overlay", owner: "persona.js" },
    { id: "pe-confirm-overlay", owner: "persona.js" },
    { id: "chatrooms-overlay", owner: "chatrooms.js" },
    { id: "cr-confirm-overlay", owner: "chatrooms.js" },
    { id: "persona-picker-overlay", owner: "chatrooms.js" },
    { id: "settings-overlay", owner: "settings.js" },
    { id: "gen-settings-overlay", owner: "gen-settings.js" },
];

/**
 * The nearest common inclusive ancestor of two nodes — the node the
 * browser dispatches `click` on when mousedown and mouseup land on
 * different elements. null means "no common ancestor": the press ended
 * outside the document, and no click is dispatched at all.
 */
function nearestCommonAncestor(a, b) {
    const ancestorsOfDown = new Set();
    for (let node = a; node; node = node.parentNode) ancestorsOfDown.add(node);
    for (let node = b; node; node = node.parentNode) {
        if (ancestorsOfDown.has(node)) return node;
    }
    return null;
}

/**
 * Drive one full press on `overlayEl`: mouse down on `down`, drag, up on
 * `up` (undefined = released outside the window, so no mouseup is
 * delivered), followed by the `click` the browser synthesises afterwards.
 *
 * The synthesis is the whole point of this harness: a real browser does
 * NOT fire that click on either endpoint — it fires it on the nearest
 * common ancestor of the two. Skipping it would leave the pre-fix code
 * looking innocent, because the buggy handler only ever listened to
 * `click`. Inner presses reach the overlay's listeners by bubbling, so
 * every event here is dispatched on the overlay with the right `target`.
 */
function press(overlayEl, { down, up }) {
    overlayEl.dispatch("mousedown", { target: down });
    if (up === undefined) return;
    overlayEl.dispatch("mouseup", { target: up });

    const clickTarget = nearestCommonAncestor(down, up);
    if (clickTarget) overlayEl.dispatch("click", { target: clickTarget });
}

/* ==========================================================================
    installBackdropClose() — the helper contract, in isolation
    ========================================================================== */

test("installBackdropClose_pressDownAndUpOnBackdrop_callsCloseOnce", async () => {
    // GIVEN the helper installed on a fresh overlay:
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN a press starts and ends on the backdrop itself,
    press(overlay, { down: overlay, up: overlay });

    // THEN the modal is asked to close exactly once:
    assert.equal(closes, 1, "a genuine backdrop click must close once");
});

test("installBackdropClose_pressStartsOnControlEndsOnBackdrop_neverCallsClose", async () => {
    // GIVEN the helper installed on a fresh overlay:
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    const checkbox = h.controlIn(overlay, "pf-tool-calls");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN the press starts on a control inside the modal and the pointer
    // is dragged onto the backdrop before releasing (the reported bug),
    press(overlay, { down: checkbox, up: overlay });

    // THEN nothing closes:
    assert.equal(closes, 0, "a drag that starts inside the modal is not a backdrop click");
});

test("installBackdropClose_pressStartsOnBackdropEndsOnControl_neverCallsClose", async () => {
    // GIVEN the helper installed on a fresh overlay:
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    const saveButton = h.controlIn(overlay, "pe-btn-save");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN the press starts on the backdrop but the pointer is dragged into
    // the modal box before releasing (the mirror case),
    press(overlay, { down: overlay, up: saveButton });

    // THEN nothing closes:
    assert.equal(closes, 0, "a press that ends inside the modal is not a backdrop click");
});

test("installBackdropClose_pressReleasedOutsideWindow_thenBackdropClick_closesOnTheSecondPressOnly", async () => {
    // GIVEN the helper installed on a fresh overlay:
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN a backdrop press is released off-window (no mouseup is delivered
    // at all) and the NEXT press is a normal backdrop click,
    press(overlay, { down: overlay, up: undefined });
    press(overlay, { down: overlay, up: overlay });

    // THEN exactly one close happened — the interrupted press alone must not
    // arm the close, and no suppression may stick to the next press:
    assert.equal(closes, 1, "one close, from the completed press only");
});

test("installBackdropClose_dragFromControlThenBackdropClick_stillCloses", async () => {
    // GIVEN the helper installed on a fresh overlay:
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    const colorInput = h.controlIn(overlay, "pf-avatar-color");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN a drag off a control is (correctly) ignored and the user then
    // clicks the backdrop properly,
    press(overlay, { down: colorInput, up: overlay });
    press(overlay, { down: overlay, up: overlay });

    // THEN the real backdrop click still closes the modal:
    assert.equal(closes, 1, "an ignored drag must not disarm later backdrop clicks");
});

/* ==========================================================================
    The real modals — all six share the one helper
    ========================================================================== */

test("harnessSelfCheck_naiveClickOnOverlayHandler_isFooledByAControlToBackdropDrag", () => {
    // GIVEN the PRE-FIX handler shape, installed by hand on a fresh overlay
    // (this test pins the harness itself: it must reproduce the browser's
    // click retargeting, otherwise every drag test above is vacuous),
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    const checkbox = h.controlIn(overlay, "pf-tool-calls");
    let closes = 0;
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closes++;
    });

    // WHEN the pointer goes down on the checkbox and releases on the backdrop,
    press(overlay, { down: checkbox, up: overlay });

    // THEN the naive handler really is fooled — proof that the drag tests
    // below can actually detect this bug:
    assert.equal(closes, 1, "the harness must synthesise the click on the common ancestor");
});

for (const { id, owner } of MODALS) {
    test(`backdropClick_${id}_pressAndReleaseOnBackdrop_closesTheModal`, async () => {
        // GIVEN the modal owned by ${owner} is open:
        const h = createModalHarness();
        const overlay = h.elementById(id);
        overlay.classList.remove("hidden");

        // WHEN the backdrop is pressed and released on itself,
        press(overlay, { down: overlay, up: overlay });
        await h.settle();

        // THEN the modal is hidden:
        assert.ok(overlay.classList.contains("hidden"), `${id} must close on a backdrop click`);
    });

    test(`backdropDrag_${id}_pressOnControlDragOntoBackdrop_keepsTheModalOpen`, async () => {
        // GIVEN the modal owned by ${owner} is open (the New Persona form
        // scenario the user reported):
        const h = createModalHarness();
        const overlay = h.elementById(id);
        overlay.classList.remove("hidden");
        const control = h.controlIn(overlay, "some-checkbox-or-color-input");

        // WHEN the pointer goes down on a control inside the form, drags
        // left onto the backdrop and releases there,
        press(overlay, { down: control, up: overlay });
        await h.settle();

        // THEN the modal — and the half-filled form inside it — survives:
        assert.ok(
            !overlay.classList.contains("hidden"),
            `${id} must NOT close when a press that started inside the modal ends on the backdrop`,
        );
    });

    test(`backdropDrag_${id}_pressOnBackdropDragIntoModalBox_keepsTheModalOpen`, async () => {
        // GIVEN the modal owned by ${owner} is open:
        const h = createModalHarness();
        const overlay = h.elementById(id);
        overlay.classList.remove("hidden");
        const insideModal = h.controlIn(overlay, "some-element-inside-the-modal-box");

        // WHEN the pointer goes down on the backdrop and releases inside the
        // modal box,
        press(overlay, { down: overlay, up: insideModal });
        await h.settle();

        // THEN nothing closes:
        assert.ok(
            !overlay.classList.contains("hidden"),
            `${id} must NOT close when a press that started on the backdrop ends inside the modal`,
        );
    });
}

/* ==========================================================================
    Poison guard — the old click-based pattern must not come back
    ========================================================================== */

test("modalModules_noneOfThemReRegisterTheOldClickOnOverlayBackdropHandler", () => {
    // GIVEN the modules that own a backdrop-closable modal,
    // WHEN their source is scanned for the pre-fix pattern — a `click`
    // handler on an overlay element. That is the exact shape the browser
    // fooled by a control-to-backdrop drag: click is dispatched on the
    // common ancestor of mousedown/mouseup, so `e.target === overlay`
    // cannot tell a backdrop click from a drag that merely ENDED there.
    for (const file of ["persona.js", "chatrooms.js", "settings.js", "gen-settings.js"]) {
        const src = fs.readFileSync(path.join(STATIC_DIR, file), "utf8");
        const offenders = src
            .split("\n")
            .map((line, i) => ({ line, n: i + 1 }))
            .filter(({ line }) => /Overlay\.addEventListener\(\s*["']click["']/.test(line));

        // THEN none of them registers it:
        assert.deepEqual(
            offenders,
            [],
            `${file}: backdrop close must go through installBackdropClose(); found line(s): ` +
                offenders.map((o) => `${o.n}: ${o.line.trim()}`).join(" | "),
        );
    }
});
