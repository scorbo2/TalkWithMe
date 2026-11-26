/**
 * test_voice_badge.js — the sidebar's 🔇 "this persona will be silent" badge
 * (personaVoiceBlock() in static/utils.js, renderPersonaList() in
 * static/persona.js, refreshPersonaSidebar() in static/chatrooms.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_voice_badge.js
 *
 * Issue #164 phase 2. The badge has TWO independent reasons, and the whole
 * point of this suite is that they stay independent:
 *
 *   1. the PERSONA axis — it has no reference-audio clip (GET /api/personas:
 *      has_reference_audio). Always silent, on every engine.
 *   2. the ENGINE axis — the connected engine demands a transcript of the clip
 *      and this persona has none (/api/tts/health: reference_text_required +
 *      has_reference_transcript). Silent HERE, speakable if you switch engines.
 *
 * Invariants locked in:
 *   - a persona with a clip and no transcript is NOT badged while the engine
 *     does not demand one (badging it would be the issue-#164 bug wearing a
 *     different hat: permanent 🔇 for a perfectly usable voice);
 *   - it IS badged once the engine demands one, and the tooltip says which half
 *     to go fix;
 *   - the engine fact reaches the sidebar through refreshPersonaSidebar()
 *     because init() renders personas BEFORE /api/tts/health resolves — and
 *     that repaint must not re-post the room's active personas to the session
 *     (that is applyChatRoomFilter()'s job, not a repaint's);
 *   - the room filter and the remove-button rule survive the repaint, because
 *     both functions derive the visible list from the one helper.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which must
 * run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_voice_badge.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html: utils, then state, then features.
const APP_SCRIPTS = ["utils.js", "state.js", "persona.js", "chatrooms.js"];

/* ==========================================================================
    DOM / browser stubs (same minimal HTML-element stand-in the other persona
    suites use: enough for classList, children, dataset, and listeners)
    ========================================================================== */

function makeFakeElement(id) {
    const listeners = new Map();
    const classSet = new Set();
    let innerHtml = "";

    const el = {
        id,
        checked: false,
        textContent: "",
        title: "",
        value: "",
        files: [],
        style: {},
        dataset: {},
        children: [],
        className: "",
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        focus() {},
        appendChild(child) {
            el.children.push(child);
            return child;
        },
        // Test helper: persona.js's renderPersonaAvatar() installs onerror.
        dispatch(type, event) {
            for (const fn of listeners.get(type) || []) fn(event);
        },
    };

    Object.defineProperty(el, "innerHTML", {
        get: () => innerHtml,
        set: (v) => {
            innerHtml = v;
            if (v === "") el.children.length = 0; // real DOM clears children
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

    const fetchStub = async (url, options = {}) => {
        fetchStub.calls.push({
            url: String(url),
            method: (options && options.method) || "GET",
        });
        return { ok: true, status: 200, json: async () => ({}) };
    };
    fetchStub.calls = [];

    const sandbox = {
        console,
        document: documentStub,
        fetch: fetchStub,
        URL: { createObjectURL: () => "blob:fake", revokeObjectURL: () => {} },
        requestAnimationFrame: () => 0,
        FormData,
        Audio: class {
            play() { return Promise.resolve(); }
            pause() {}
        },
    };

    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, file), "utf8"), sandbox, {
            filename: file,
        });
    }

    /**
     * Seed the shared globals. state.js declares them with `let`, which are
     * NOT properties of the vm context object — they can only be assigned
     * from inside the context.
     */
    function seed({ personas = [], room = "default", roomPersonas = {}, engineRequiresTranscript = false }) {
        sandbox.__seed = { personas, room, roomPersonas, engineRequiresTranscript };
        vm.runInContext(
            `personas = __seed.personas;
             currentChatRoom = __seed.room;
             roomPersonas = __seed.roomPersonas;
             ttsReferenceTextRequired = __seed.engineRequiresTranscript;
             selectedPersona = null;`,
            sandbox,
            { filename: "harness-seed.js" },
        );
    }

    /**
     * Every rendered card, as { name, badge }: `badge` is the mute indicator
     * element (with its .title) or null.
     */
    function cards() {
        const personaListEl = elementById("persona-list");
        return personaListEl.children
            .filter((child) => child.className === "persona-card")
            .map((card) => {
                const avatar = card.children.find((c) => c.className === "persona-avatar");
                const badge = avatar
                    ? (avatar.children || []).find((c) => c.className === "mute-indicator")
                    : undefined;
                return { name: card.dataset.name, badge: badge || null };
            });
    }

    const badged = () => cards().filter((c) => c.badge).map((c) => c.name);

    return {
        sandbox,
        elementById,
        fetchStub,
        seed,
        cards,
        badged,
        run(statement) {
            return vm.runInContext(statement, sandbox);
        },
        get(expression) {
            return vm.runInContext(`(() => (${expression}))()`, sandbox);
        },
    };
}

/** Persona-summary fixtures, shaped like PersonaResponse in app/models.py. */
function personaSummary(name, { clip = true, transcript = true } = {}) {
    return {
        name,
        description: "",
        avatar_color: "#888888",
        avatar_image: false,
        tts_capable: clip,
        has_reference_audio: clip,
        has_reference_transcript: transcript,
    };
}

/* ==========================================================================
    The decision helper (utils.js)
    ========================================================================== */

test("personaVoiceBlock_withClipAndNoEngineRequirement_returnsNull", () => {
    const h = createHarness();

    const block = h.get(
        `personaVoiceBlock(${JSON.stringify(personaSummary("Luna"))}, false)`,
    );

    assert.equal(block, null, "a clip and a happy engine is nothing to warn about");
});

test("personaVoiceBlock_withNoClip_returnsTheMissingClipReason_evenWhenTheEngineIsHappy", () => {
    const h = createHarness();

    const block = h.get(
        `personaVoiceBlock(${JSON.stringify(personaSummary("Alex", { clip: false }))}, false)`,
    );

    assert.ok(block, "no clip is silent on every engine");
    assert.ok(block.includes("reference audio"), block);
    assert.ok(block.includes("Persona editor"), "the tooltip must say where to fix it");
});

test("personaVoiceBlock_withClipButNoTranscript_isBlockedOnlyWhenTheEngineRequiresOne", () => {
    const h = createHarness();
    const fixture = JSON.stringify(personaSummary("Bob", { transcript: false }));

    // THEN the same persona is unblocked on LuxTTS (no reference_text field)
    // and blocked on Breeze Blue (reference_text required) — the persona data
    // never changes; only the engine answer does.
    assert.equal(h.get(`personaVoiceBlock(${fixture}, false)`), null);
    const block = h.get(`personaVoiceBlock(${fixture}, true)`);
    assert.ok(block, "the engine demands the missing half");
    assert.ok(block.includes("transcript"), block);
    assert.ok(block.includes("Persona editor"), block);
});

test("personaVoiceBlock_withTranscriptButNoClip_reportsTheClip_notTheTranscript", () => {
    // The order is deliberate: a transcript of a clip that does not exist is
    // nothing to clone from, and "add a transcript" would be useless advice.
    const h = createHarness();
    const fixture = JSON.stringify({
        name: "Odd",
        description: "",
        avatar_color: "#888888",
        avatar_image: false,
        tts_capable: false,
        has_reference_audio: false,
        has_reference_transcript: true,
    });

    const block = h.get(`personaVoiceBlock(${fixture}, true)`);

    assert.ok(block.includes("reference audio"), block);
    assert.ok(!block.includes("transcript"), block);
});

/* ==========================================================================
    The sidebar rendering (persona.js)
    ========================================================================== */

test("renderPersonaList_engineDoesNotRequireTranscript_badgesOnlyThePersonaWithoutAClip", () => {
    const h = createHarness();
    h.seed({
        personas: [
            personaSummary("Alex", { clip: false }),
            personaSummary("Bob", { transcript: false }),
            personaSummary("Luna"),
        ],
        engineRequiresTranscript: false,
    });

    h.run("renderPersonaList()");

    assert.deepEqual(h.badged(), ["Alex"], "Bob speaks fine on this engine — that was issue #164");
});

test("renderPersonaList_engineRequiresTranscript_alsoBadgesThePersonaWithNoTranscript", () => {
    const h = createHarness();
    h.seed({
        personas: [
            personaSummary("Alex", { clip: false }),
            personaSummary("Bob", { transcript: false }),
            personaSummary("Luna"),
        ],
        engineRequiresTranscript: true,
    });

    h.run("renderPersonaList()");

    assert.deepEqual(h.badged(), ["Alex", "Bob"]);
});

test("renderPersonaList_badgeTooltip_namesThePersonaAndWhichHalfToFix", () => {
    const h = createHarness();
    h.seed({
        personas: [personaSummary("Alex", { clip: false }), personaSummary("Bob", { transcript: false })],
        engineRequiresTranscript: true,
    });

    h.run("renderPersonaList()");

    const byName = Object.fromEntries(h.cards().map((c) => [c.name, c.badge]));
    assert.ok(byName["Alex"].title.includes("Alex"), byName["Alex"].title);
    assert.ok(byName["Alex"].title.includes("reference audio"), byName["Alex"].title);
    assert.ok(byName["Bob"].title.includes("Bob"), byName["Bob"].title);
    assert.ok(byName["Bob"].title.includes("transcript"), byName["Bob"].title);
    // Two different reasons must not render the same mystery tooltip.
    assert.notEqual(byName["Alex"].title, byName["Bob"].title);
    assert.ok(!byName["Luna"], "only the blocked personas are rendered here");
});

/* ==========================================================================
    The repaint after the health answer (chatrooms.js + app.js ordering)
    ========================================================================== */

test("refreshPersonaSidebar_afterTheHealthAnswerArrives_addsTheBadgeWithoutTouchingTheSession", () => {
    // GIVEN the sidebar already rendered (init() awaits loadPersonas() before
    // checkTTSHealth()), with the engine answer still unknown,
    const h = createHarness();
    h.seed({ personas: [personaSummary("Bob", { transcript: false })] });
    h.run("renderPersonaList()");
    assert.deepEqual(h.badged(), [], "no engine knowledge yet, no badge");
    h.fetchStub.calls.length = 0;

    // WHEN /api/tts/health resolves saying the connected engine demands a
    // transcript, and the sidebar is repainted,
    h.run("ttsReferenceTextRequired = true; refreshPersonaSidebar();");

    // THEN the badge appears...
    assert.deepEqual(h.badged(), ["Bob"]);
    // ...and the repaint issued NO network traffic at all. applyChatRoomFilter()
    // would have re-POSTed the room's active personas to /api/session/personas
    // on what is only a repaint.
    assert.deepEqual(h.fetchStub.calls, [], "a repaint must not re-post session personas");
});

test("refreshPersonaSidebar_inANamedRoom_keepsTheRoomFilterAndTheRemoveButtons", () => {
    // GIVEN a named room holding one of two personas,
    const h = createHarness();
    h.seed({
        personas: [personaSummary("Anchored"), personaSummary("Drifting", { transcript: false })],
        room: "Lounge",
        roomPersonas: { Lounge: ["Anchored"] },
    });

    // WHEN the engine answer arrives and the sidebar is repainted,
    h.run("ttsReferenceTextRequired = true; refreshPersonaSidebar();");

    // THEN the room filter is unchanged (a repaint is not a room switch)...
    assert.deepEqual(h.cards().map((c) => c.name), ["Anchored"]);
    // ...and the remove-button rule (show them in a named room, never in
    // "default") still holds — the two entry points must not drift apart.
    const card = h.elementById("persona-list").children.find((c) => c.className === "persona-card");
    assert.ok(
        card.children.some((c) => c.className === "persona-remove-btn"),
        `expected the remove button in a named room, got: ${card.children.map((c) => c.className)}`,
    );
});

test("refreshPersonaSidebar_andApplyChatRoomFilter_deriveTheVisibleListFromTheSameHelper", () => {
    // A structural fence: if a second room-filter implementation is reintroduced,
    // these two functions stop sharing currentRoomPersonas() and the repaint can
    // silently show a different list than the room switch does.
    const chatrooms = fs.readFileSync(path.join(STATIC_DIR, "chatrooms.js"), "utf8");
    const helperUses = (chatrooms.match(/currentRoomPersonas\(\)/g) || []).length;
    assert.ok(
        helperUses >= 2,
        `currentRoomPersonas() must be used by both entry points, found ${helperUses} use(s)`,
    );
    const app = fs.readFileSync(path.join(STATIC_DIR, "app.js"), "utf8");
    assert.ok(
        /refreshPersonaSidebar\s*\(/.test(app),
        "checkTTSHealth() must repaint the sidebar: personas render before health resolves",
    );
    assert.ok(
        /ttsReferenceTextRequired\s*=\s*!!data\.reference_text_required/.test(app),
        "the health payload must feed the engine-axis global",
    );
});
