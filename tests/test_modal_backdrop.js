/**
 * test_modal_backdrop.js — Regression tests for closing the app's modals
 * by clicking the dark backdrop (installBackdropClose() in static/utils.js,
 * wired up in persona.js / chatrooms.js / settings.js / gen-settings.js).
 *
 * Which modals those are is DERIVED from templates/index.html at run time (the
 * id of every class="modal-overlay" element, each owner resolved from the
 * installBackdropClose() call that wires it) rather than hand-listed here. A
 * hand-written table — mirrored with a hand-written COUNT in AGENTS.md and the
 * feature doc — is exactly how "six modals" outlived the seventh modal, and a
 * list nobody compares to the markup can only ever go stale. A new modal is
 * therefore picked up by every per-modal test below automatically, while a
 * modal added WITHOUT its backdrop wiring fails loudly, naming its element id.
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
 * Issue #172 is the follow-up that rewrite introduced: `mousedown`/`mouseup`
 * fire once PER MOUSE BUTTON, so a button-blind press pair made a
 * middle-click or a right-click on the backdrop a valid "backdrop click"
 * and closed the modal the same way. The old click-on-overlay handler had
 * never had that bug (a browser synthesises a `click` for the MAIN button
 * only), so `press()` below models the button as well: the activation event
 * it dispatches depends on which button was pressed.
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
const TEMPLATE_FILE = path.join(__dirname, "..", "templates", "index.html");
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
        // Test helper: which event types this element has listeners for.
        listenerTypes() {
            return [...listeners.keys()];
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
 * Value of `name` inside a start tag's attribute list, or null when absent.
 * Both quoting styles are understood and attribute ORDER is irrelevant, so a
 * tag that spells `class` before `id` is still found.
 */
function attributeValue(attrs, name) {
    const match =
        new RegExp(`(?:^|\\s)${name}\\s*=\\s*"([^"]*)"`).exec(attrs) ||
        new RegExp(`(?:^|\\s)${name}\\s*=\\s*'([^']*)'`).exec(attrs);
    return match ? match[1] : null;
}

/**
 * The id of every `.modal-overlay` element in `html`, in document order — the
 * authoritative list of modals the app can close from its backdrop.
 */
function modalOverlayIdsInTemplate(html) {
    const ids = [];
    for (const [, attrs] of html.matchAll(/<[a-zA-Z][a-zA-Z0-9]*\b([^>]*)>/g)) {
        const classes = attributeValue(attrs, "class");
        if (classes === null) continue;
        // A class TOKEN, not a substring: `modal-overlay-extra` is not an overlay.
        if (!classes.split(/\s+/).includes("modal-overlay")) continue;
        const id = attributeValue(attrs, "id");
        if (id !== null) ids.push(id);
    }
    return ids;
}

/** Every .js file in static/, keyed by file name — read once, reused below. */
function readStaticSources() {
    const sources = {};
    for (const file of fs.readdirSync(STATIC_DIR).sort()) {
        if (file.endsWith(".js")) sources[file] = fs.readFileSync(path.join(STATIC_DIR, file), "utf8");
    }
    return sources;
}

const STATIC_SOURCES = readStaticSources();

/**
 * The module-level const state.js binds to an element id
 * (`const peConfirmOverlay = document.getElementById("pe-confirm-overlay")`).
 * That is the name the module that closes the modal passes around.
 */
function elementVarNameOf(id, stateSource) {
    const escapedId = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const decl = new RegExp(
        `(?:const|let|var)\\s+(\\w+)\\s*=\\s*document\\.getElementById\\(\\s*["']${escapedId}["']\\s*\\)`,
    ).exec(stateSource);
    return decl ? decl[1] : null;
}

/**
 * Which static module(s) hand that element to installBackdropClose().
 *
 * Line comments are skipped deliberately: a commented-out call still reads as
 * wiring to a naive substring match, and that exact fooling was observed while
 * writing this guard (an unwired modal reported its owner correctly and only
 * the listener check below noticed the truth).
 */
function modulesWiringOverlay(varName) {
    if (!varName) return [];
    const call = new RegExp(`installBackdropClose\\(\\s*${varName}\\s*,`);
    const commentedOut = /^\s*(\/\/|\*|\/\*)/;
    return Object.keys(STATIC_SOURCES).filter((file) =>
        STATIC_SOURCES[file].split("\n").some((line) => !commentedOut.test(line) && call.test(line)),
    );
}

/**
 * Every modal the app can close via its backdrop: the element id plus the
 * module that wires it.
 *
 * This table used to be hand-written here (and mirrored, with a count, in
 * AGENTS.md and docs/feature_modal_backdrop_close.md) — which is exactly how
 * "six modals" outlived the arrival of a seventh: nothing ever compared the
 * list to the markup, so it could only go stale. Deriving it from
 * templates/index.html means a new modal shows up in every per-modal test
 * below the moment it lands in the page, and an UNWIRED one fails loudly
 * instead of quietly enlarging the untested surface.
 */
const MODALS = modalOverlayIdsInTemplate(fs.readFileSync(TEMPLATE_FILE, "utf8")).map((id) => ({
    id,
    owner:
        modulesWiringOverlay(elementVarNameOf(id, STATIC_SOURCES["state.js"] || "")).join(", ") ||
        "(not wired)",
}));

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

/** Which event a completed press activates, per mouse button (UI Events:
 *  `click` is main-button only; middle is `auxclick`, right is `contextmenu`
 *  and never a click at all). */
const ACTIVATION_EVENT_OF_BUTTON = { 0: "click", 1: "auxclick", 2: "contextmenu" };

/**
 * Drive one full press on `overlayEl`: mouse down on `down`, drag, up on
 * `up` (undefined = released outside the window, so no mouseup is
 * delivered), followed by the activation event the browser synthesises
 * afterwards (`click` for the default left button).
 *
 * The synthesis is the whole point of this harness: a real browser does
 * NOT fire that click on either endpoint — it fires it on the nearest
 * common ancestor of the two. Skipping it would leave the pre-fix code
 * looking innocent, because the buggy handler only ever listened to
 * `click`. Inner presses reach the overlay's listeners by bubbling, so
 * every event here is dispatched on the overlay with the right `target`.
 *
 * `button` is modelled because issue #172 is about it: 0 main, 1 auxiliary
 * (middle), 2 secondary (right). A stub that left `button` undefined would
 * make the fixed helper refuse EVERY press, which fails the backdrop-click
 * tests loudly rather than quietly.
 */
function press(overlayEl, { down, up, button = 0 }) {
    overlayEl.dispatch("mousedown", { target: down, button });
    if (up === undefined) return;
    overlayEl.dispatch("mouseup", { target: up, button });

    const clickTarget = nearestCommonAncestor(down, up);
    if (clickTarget) {
        overlayEl.dispatch(ACTIVATION_EVENT_OF_BUTTON[button] || "click", {
            target: clickTarget,
            button,
        });
    }
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

/* --------------------------------------------------------------------------
    Issue #172 — the press pair is a MAIN (left) button press pair
    -------------------------------------------------------------------------- */

test("installBackdropClose_middlePressPairOnBackdrop_neverCallsClose", () => {
    // GIVEN the helper installed on a fresh overlay,
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN the MIDDLE button is pressed and released on the backdrop (button
    // 1: a completed press pair, and on X11 a primary-selection paste on top
    // of it),
    press(overlay, { down: overlay, up: overlay, button: 1 });

    // THEN the modal survives:
    assert.equal(closes, 0, "a middle-click on the backdrop must never close the modal");
});

test("installBackdropClose_rightPressPairOnBackdrop_neverCallsClose", () => {
    // GIVEN the helper installed on a fresh overlay,
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN the RIGHT button is pressed and released on the backdrop (button
    // 2, which also opens the native context menu over the page),
    press(overlay, { down: overlay, up: overlay, button: 2 });

    // THEN the modal survives — the user was after a context menu, not after
    // a way to lose the half-filled form:
    assert.equal(closes, 0, "a right-click on the backdrop must never close the modal");
});

test("installBackdropClose_nonMainPressesOnBackdrop_thenLeftBackdropClick_closesOnTheLeftPressOnly", () => {
    // GIVEN the helper installed on a fresh overlay,
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN a middle and a right press pair land on the backdrop and the user
    // then clicks the backdrop properly,
    press(overlay, { down: overlay, up: overlay, button: 1 });
    press(overlay, { down: overlay, up: overlay, button: 2 });
    press(overlay, { down: overlay, up: overlay, button: 0 });

    // THEN exactly one close happened: a non-main press is ignored, and being
    // ignored must not disarm the NEXT press either:
    assert.equal(closes, 1, "one close, from the left-button press only");
});

test("installBackdropClose_offWindowLeftPress_thenRightPress_thenReleaseInside_neverCallsClose", () => {
    // GIVEN the helper installed on a fresh overlay. The pre-#172 flag is
    // re-derived on EVERY mousedown, which is what stops a press released
    // off-window leaving a stale "close me" armed; the button gate must not
    // quietly narrow that re-arming to left presses only.
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    const checkbox = h.controlIn(overlay, "pf-tool-calls");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN a left press starts on the backdrop and is released off-window,
    // then a right press happens on a control, then a left press starts on
    // the backdrop and releases inside the modal box,
    press(overlay, { down: overlay, up: undefined, button: 0 });
    press(overlay, { down: checkbox, up: undefined, button: 2 });
    press(overlay, { down: overlay, up: checkbox, button: 0 });

    // THEN nothing closes:
    assert.equal(closes, 0, "a stale backdrop arming must not survive a non-main press");
});

test("installBackdropClose_middleButtonReleasedFirstWhileLeftStillHeld_neverCallsClose", () => {
    // GIVEN the helper installed on a fresh overlay. This is the multi-button
    // gymnastics the RELEASE-side button gate exists for.
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    h.sandbox.installBackdropClose(overlay, () => closes++);

    // WHEN the middle button goes down on the backdrop, the left button goes
    // down there too, and the MIDDLE one is released first (a mouseup whose
    // button is 1 with a left pair nominally in flight),
    overlay.dispatch("mousedown", { target: overlay, button: 1 });
    overlay.dispatch("mousedown", { target: overlay, button: 0 });
    overlay.dispatch("mouseup", { target: overlay, button: 1 });

    // THEN nothing has closed:
    assert.equal(closes, 0, "releasing a button that never pressed the backdrop must not close");

    // AND the eventual left release does not close either — that press
    // sequence began as a middle press, and losing it is the safe side of the
    // ambiguity:
    overlay.dispatch("mouseup", { target: overlay, button: 0 });
    assert.equal(closes, 0, "the interleaved press must not close on either release");
});

/* ==========================================================================
    The real modals — every one of them shares the one helper
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

test("harnessSelfCheck_buttonBlindPressPairHandler_isFooledByANonMainPressOnTheBackdrop", () => {
    // GIVEN the pre-#172 shape of the helper — the press pair, but with no
    // button check — installed by hand. This is the self-check the fix needs:
    // if press() ever stopped modelling `button`, the #172 tests above would
    // read as green against a helper that ignores every press, which is a
    // different bug that nobody would notice.
    const h = createModalHarness();
    const overlay = h.elementById("some-overlay");
    let closes = 0;
    let armed = false;
    overlay.addEventListener("mousedown", (e) => {
        armed = e.target === overlay;
    });
    overlay.addEventListener("mouseup", (e) => {
        const completed = armed && e.target === overlay;
        armed = false;
        if (completed) closes++;
    });

    // WHEN a middle and a right press pair land on the backdrop,
    press(overlay, { down: overlay, up: overlay, button: 1 });
    press(overlay, { down: overlay, up: overlay, button: 2 });

    // THEN this handler really is fooled twice — proof that the tests above
    // can detect the bug at all:
    assert.equal(closes, 2, "the harness must deliver non-main button presses to the listeners");
});

/* --------------------------------------------------------------------------
    The derived modal list itself. A scraper that quietly returns nothing (or
    nothing but a subset) would leave every per-modal loop below registering
    zero tests — green, and covering less than it claims. These pin the list
    to the markup it is supposed to come from.
    -------------------------------------------------------------------------- */

test("templateScraper_withAttributeOrderAndQuotingVariations_findsEveryOverlayAndNoLookalike", () => {
    // GIVEN markup covering each shape the scraper must survive, plus the
    // near-misses it must not mistake for an overlay,
    const sample = [
        '<div id="a-overlay" class="modal-overlay hidden" role="dialog">',
        '<div class="hidden modal-overlay" id="b-overlay">',
        "<div id='c-overlay' class='modal-overlay'>",
        '<div id="not-an-overlay" class="modal-overlay-extra">',
        '<div id="also-not-an-overlay" class="modal-box">',
        '<div class="modal-overlay"><!-- no id: nothing here to test --></div>',
    ].join("\n");

    // WHEN it is scanned,

    // THEN exactly the three real overlays come back, in document order:
    assert.deepEqual(modalOverlayIdsInTemplate(sample), ["a-overlay", "b-overlay", "c-overlay"]);
});

test("derivedModalList_matchesEveryOverlayMentionTemplatesIndexHtmlContains", () => {
    // GIVEN the real templates/index.html,
    const html = fs.readFileSync(TEMPLATE_FILE, "utf8");

    // WHEN the derived list is compared with every mention of the overlay
    // class anywhere in the file,

    // THEN the parser saw them all. A mismatch means the markup grew a shape
    // this scraper does not understand (an unquoted attribute, a `>` inside
    // an attribute value) — it does NOT mean a modal lost its wiring:
    const mentions = (html.match(/modal-overlay/g) || []).length;
    assert.ok(
        MODALS.length > 0,
        "no modal at all was derived from templates/index.html: the scraper is broken " +
            "and every per-modal test below is vacuous",
    );
    assert.equal(
        MODALS.length,
        mentions,
        `templates/index.html mentions the overlay class ${mentions} time(s), but ${MODALS.length} ` +
            `overlay element(s) parsed (${MODALS.map((m) => m.id).join(", ") || "none"}) — ` +
            "the template scraper needs updating",
    );
});

test("modalList_eachModalIsWiredBySomeStaticModuleThatThisHarnessLoads", () => {
    // GIVEN the modal list derived above, an `owner` of "(not wired)" means no
    // module in static/ passes that element to installBackdropClose(): the
    // modal was added to the page but never given backdrop handling.
    // WHEN each modal is checked, THEN it is wired, and by a module the
    // sandbox below actually evaluates (otherwise the per-modal tests would
    // be asserting against a modal nobody had wired yet):
    for (const { id, owner } of MODALS) {
        assert.notEqual(owner, "(not wired)", `${id}: no module in static/ calls installBackdropClose() for it`);
        for (const module of owner.split(", ")) {
            assert.ok(
                APP_SCRIPTS.includes(module),
                `${id} is wired by ${module}, which createModalHarness() does not load — add it to APP_SCRIPTS`,
            );
        }
    }
});

test("backdropListeners_everyModalOverlayGetsThePressPairAndNoClickHandler", async () => {
    // GIVEN the real modules have run far enough for their backdrop wiring to
    // have executed (utils.js + state.js + the modal modules, plus the
    // chatrooms setup app.js performs at startup),
    const h = createModalHarness();

    // WHEN each overlay derived from the template is inspected for the
    // listeners it ended up with,

    // THEN it carries exactly what installBackdropClose() installs — the press
    // pair — and NOT the pre-fix `click`-on-overlay handler. This is the
    // behavioural twin of the grep guard at the bottom of the file: it catches
    // a hand-rolled close handler that only differs in spelling:
    for (const { id, owner } of MODALS) {
        const types = h.elementById(id).listenerTypes();
        assert.ok(
            types.includes("mousedown"),
            `${id} (owner: ${owner}) has no mousedown listener — backdrop close is not installed`,
        );
        assert.ok(
            types.includes("mouseup"),
            `${id} (owner: ${owner}) has no mouseup listener — backdrop close is not installed`,
        );
        assert.ok(
            !types.includes("click"),
            `${id} has a click listener — the pre-fix backdrop handler is back`,
        );
        // ...and no "other activation event" variant of it either: a close
        // decided from auxclick/contextmenu is the same #172 bug in a
        // different hat, and pointer events would reintroduce the pairing
        // question under new names.
        for (const forbidden of ["auxclick", "contextmenu", "pointerdown", "pointerup"]) {
            assert.ok(
                !types.includes(forbidden),
                `${id} has a ${forbidden} listener — backdrop close is the mousedown/mouseup pair only`,
            );
        }
    }
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

    test(`backdropButton_${id}_middleAndRightPressPairs_neverCloseTheModal`, async () => {
        // GIVEN the modal owned by ${owner} is open:
        const h = createModalHarness();
        const overlay = h.elementById(id);
        overlay.classList.remove("hidden");

        // WHEN the backdrop is clicked with the middle and then the right
        // button (issue #172),
        press(overlay, { down: overlay, up: overlay, button: 1 });
        await h.settle();
        press(overlay, { down: overlay, up: overlay, button: 2 });
        await h.settle();

        // THEN the modal is still open:
        assert.ok(
            !overlay.classList.contains("hidden"),
            `${id} must NOT close on a middle- or right-click of the backdrop`,
        );

        // AND a plain left click still closes it — the guard must not have
        // disarmed this modal's backdrop for good:
        press(overlay, { down: overlay, up: overlay, button: 0 });
        await h.settle();
        assert.ok(
            overlay.classList.contains("hidden"),
            `${id} must still close on a left-click of the backdrop`,
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
    // GIVEN every module in static/ — not merely the ones that own a modal
    // today, because the pattern comes back the day someone adds a modal in
    // a module this test had never heard of,
    // WHEN their source is scanned for the pre-fix pattern — a `click`
    // handler on an overlay element. That is the exact shape the browser
    // fooled by a control-to-backdrop drag: click is dispatched on the
    // common ancestor of mousedown/mouseup, so `e.target === overlay`
    // cannot tell a backdrop click from a drag that merely ENDED there.
    for (const [file, src] of Object.entries(STATIC_SOURCES)) {
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
