/**
 * test_tts_settings.js — Regression tests for the dynamic TTS parameter
 * section (static/tts-params.js + the TTS wiring in static/settings.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_tts_settings.js
 *
 * What it locks in (TTS generification, plan M4, per
 * docs/feature_TTS_generification.md and tts-serve's
 * docs/01-server-generification.md §4.3):
 *   - the T9 widget table: which input a parameter spec becomes, including
 *     the "null default => not a slider" rule and the integer-span rule;
 *   - app-managed fields (text / audio_base64 / reference_text / language)
 *     are never rendered, no matter what the document advertises;
 *   - "blank = let the engine decide": blank optional fields are omitted
 *     from the collected object, while checkboxes, sliders and
 *     selects-with-default are always present;
 *   - the T10 version gate: a foreign schema_version renders a notice,
 *     not a guessed-at form;
 *   - client-side validation mirrors the backend T7 rules (type, bounds,
 *     enum) so a bad value is rejected before any request goes out;
 *   - the modal wiring: the section renders from the live
 *     /api/tts/capabilities document (backend proxy), every "nothing to
 *     show" state explains itself, an engine switch refetches and says so,
 *     and a save with no document passes the saved parameters through
 *     instead of silently wiping them.
 *
 * How it works: the frontend scripts are browser globals (no ES modules),
 * so each test evaluates utils.js + state.js + tts-params.js + settings.js
 * in a fresh vm.Context against a minimal DOM stub — the same technique as
 * test_persona_form.js. fetch() is stubbed to answer GET /api/settings,
 * GET /api/tts/capabilities and PUT /api/settings from the real engine
 * snapshots in tests/fixtures/ (copied from tts-serve's test suite).
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_persona_form.js
 *     node tests/test_tts_settings.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
const FIXTURES_DIR = path.join(__dirname, "fixtures");
// Load order mirrors templates/index.html: utils, state, tts-params, settings.
const APP_SCRIPTS = ["utils.js", "state.js", "tts-params.js", "settings.js"];

/* ==========================================================================
    DOM / browser stubs
    ========================================================================== */

/**
 * Array-like, NON-Array child list — mirrors the browser's HTMLCollection:
 * iterable (for...of), indexable, has .length, but Array.isArray() ===
 * false. Code under test must therefore iterate children, never gate on
 * Array.isArray() — this is exactly what the findTtsParamRows wipe bug did
 * (real browsers silently collected ZERO values on every save).
 */
function makeChildList() {
    const backing = [];
    const list = {
        get length() { return backing.length; },
        set length(v) { backing.length = v; }, // innerHTML="" clear path
        push(item) { backing.push(item); return backing.length; },
        [Symbol.iterator]() { return backing[Symbol.iterator](); },
    };
    return new Proxy(list, {
        get(target, prop) {
            if (prop in target) return target[prop];
            const i = Number(prop);
            if (Number.isInteger(i) && i >= 0 && i < backing.length) return backing[i];
            return undefined;
        },
        set(target, prop, value) {
            const i = Number(prop);
            if (Number.isInteger(i) && i >= 0 && i < backing.length) {
                backing[i] = value;
                return true;
            }
            target[prop] = value;
            return true;
        },
    });
}

/**
 * Minimal stand-in for an HTMLElement. Implements only what
 * tts-params.js / settings.js touch: classList, event listeners (with a
 * dispatch helper for tests), appendChild/children, innerHTML="" clearing
 * child nodes, and the plain value/checked/min/max/step/title properties.
 * Rows also carry a plain `widgetEl` property in the real app — plain
 * objects here behave identically (that is why collection is structural).
 */
function makeElement(tag) {
    const listeners = new Map(); // event type -> [listener, ...]
    const classSet = new Set();

    const el = {
        tagName: String(tag).toUpperCase(),
        id: "",
        type: "",
        value: "",
        checked: false,
        open: false,
        textContent: "",
        title: "",
        min: "",
        max: "",
        step: "",
        className: "",
        dataset: {},
        children: makeChildList(),
        style: {},
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
        setAttribute() {},
        classList: {
            add: (c) => classSet.add(c),
            remove: (c) => classSet.delete(c),
            contains: (c) => classSet.has(c),
        },
    };

    Object.defineProperty(el, "innerHTML", {
        get: () => "",
        // Real DOM clears child nodes when innerHTML is set to "".
        set: (v) => {
            if (v === "") el.children.length = 0;
        },
        enumerable: true,
    });

    return el;
}

/** Minimal fetch Response stand-in: the app only reads .ok and .json(). */
function jsonResponse(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

/**
 * Build the sandbox for one test: fresh DOM elements, a fetch stub that
 * records every call (url, method, parsed JSON body) and answers the
 * settings/capabilities endpoints from mutable `state`.
 */
function createSettingsHarness(initial) {
    const elements = new Map();
    const elementById = (id) => {
        if (!elements.has(id)) elements.set(id, makeElement(id));
        return elements.get(id);
    };

    const documentStub = {
        getElementById: elementById,
        createElement: (tag) => makeElement(tag),
        // A real Text node has no `children` property at all.
        createTextNode: (text) => ({
            nodeType: 3,
            textContent: String(text),
        }),
        querySelector: () => null,
        querySelectorAll: () => [],
    };

    const fetchStub = async (url, options = {}) => {
        const u = String(url);
        const method = (options && options.method) || "GET";
        let body;
        if (options && typeof options.body === "string") {
            try {
                body = JSON.parse(options.body);
            } catch {
                body = options.body;
            }
        }
        fetchStub.calls.push({ url: u, method, body });

        const state = fetchStub.state;
        if (method === "GET" && u === "/api/settings") {
            return jsonResponse(state.settings);
        }
        if (method === "GET" && u.startsWith("/api/tts/capabilities")) {
            // Optional ?base_url= probe (the reconnect button): answered
            // from state.capabilitiesByUrl; a probe URL with no entry is a
            // dead server (503), like the real endpoint's behaviour.
            const qIdx = u.indexOf("?");
            const probeUrl =
                qIdx === -1 ? null : new URLSearchParams(u.slice(qIdx + 1)).get("base_url");
            // The real backend scheme-validates the probe BEFORE any
            // network I/O and answers 422 — mirror that contract:
            if (probeUrl && !/^https?:\/\//.test(probeUrl)) {
                return {
                    ok: false,
                    status: 422,
                    json: async () => ({ detail: "base_url must start with http:// or https://" }),
                };
            }
            const target = probeUrl
                ? state.capabilitiesByUrl.get(probeUrl) || { doc: null, status: 503 }
                : {
                      doc: state.capabilities,
                      status: state.capabilitiesStatus,
                      throw: state.capabilitiesThrow,
                  };
            if (target.throw) throw new Error("ECONNREFUSED");
            // A seeded entry without an explicit status is a healthy server.
            const status = target.status ?? 200;
            if (status !== 200) {
                return {
                    ok: false,
                    status,
                    json: async () => ({ detail: "unavailable" }),
                };
            }
            return jsonResponse(target.doc);
        }
        if (method === "PUT" && u === "/api/settings") {
            // Mirror the real endpoint: llm/tts/stt are full replacements,
            // general merges over the current values (partial update).
            // submitSettings reads `general` back from the response, and
            // the NEXT GET /api/settings must see what this save stored —
            // that is how the save->reopen round trip is testable.
            if (body && typeof body === "object" && !Array.isArray(body)) {
                if (body.llm) state.settings.llm = body.llm;
                if (body.tts) state.settings.tts = body.tts;
                if (body.stt) state.settings.stt = body.stt;
                if (body.general) Object.assign(state.settings.general, body.general);
            }
            return jsonResponse(state.settings);
        }
        return jsonResponse({});
    };
    fetchStub.calls = [];
    fetchStub.state = {
        settings: (initial && initial.settings) || settingsFixture(),
        capabilities: (initial && initial.capabilities) || loadFixture("omnivoice"),
        capabilitiesStatus: (initial && initial.capabilitiesStatus) || 200,
        capabilitiesThrow: false,
        // base_url -> {doc, status?, throw?} for ?base_url= probe requests.
        capabilitiesByUrl: new Map(),
    };

    const sandbox = {
        console,
        document: documentStub,
        fetch: fetchStub,
        requestAnimationFrame: () => 0,
        // Defined in app.js (not loaded here); submitSettings awaits both.
        checkTTSHealth: async () => {},
        checkSTTHealth: async () => {},
    };

    vm.createContext(sandbox);
    for (const file of APP_SCRIPTS) {
        const code = fs.readFileSync(path.join(STATIC_DIR, file), "utf8");
        vm.runInContext(code, sandbox, { filename: file });
    }

    return {
        sandbox,
        document: documentStub,
        elementById,
        fetchStub,
        /** Evaluate an expression inside the app context (reaches let/const globals). */
        get(expression) {
            return vm.runInContext(`(() => (${expression}))()`, sandbox);
        },
    };
}

/* ==========================================================================
    Fixtures & test helpers
    ========================================================================== */

/** One of the real tts-serve capability snapshots (test oracles). */
function loadFixture(name) {
    return JSON.parse(
        fs.readFileSync(path.join(FIXTURES_DIR, `${name}_capabilities.json`), "utf8"),
    );
}

/** Full GET /api/settings response shape (app/models.py SettingsResponse). */
function settingsFixture(overrides = {}) {
    return {
        llm: { base_url: "http://llm.local:8080", model: "test-model", max_tokens: 1024, temperature: 0.8 },
        tts: {
            enabled: true,
            base_url: "http://localhost:8000",
            timeout: 120,
            streaming: true,
            parameters: { num_steps: 16 },
        },
        stt: { enabled: false, base_url: "", timeout: 30 },
        general: {
            max_persona_replies: 1,
            persona_name_mentions: true,
            max_turns_for_context: 6,
            show_tool_calls: false,
            enable_persona_memories: true,
        },
        ...overrides,
    };
}

/** One synthetic parameter spec; override what the test needs. */
function spec(overrides = {}) {
    return {
        name: "param",
        type: "number",
        required: false,
        default: null,
        description: null,
        min: null,
        max: null,
        step: null,
        enum: null,
        advanced: false,
        ...overrides,
    };
}

/** A minimal v2 capabilities document around the given specs. */
function docWith(...specs) {
    return {
        schema_version: 2,
        engine: "synthetic",
        model: "synthetic/model",
        device: "cpu",
        sample_rate: 24000,
        watermarked: false,
        parameters: specs,
    };
}

/** Render a doc into a fresh container through the real renderer. */
function renderDoc(h, doc, savedValues = {}) {
    const container = h.document.createElement("div");
    h.sandbox.renderTtsParameters(doc, container, savedValues);
    return container;
}

/** Independent structural walk: every node carrying data-tts-param. */
function rows(container) {
    const out = [];
    const walk = (n) => {
        if (!n || typeof n !== "object") return;
        if (n.dataset && n.dataset.ttsParam) out.push(n);
        for (const child of n.children || []) walk(child);
    };
    walk(container);
    return out;
}

function rowByName(container, name) {
    return rows(container).find((r) => r.dataset.ttsParam === name);
}

function kindOf(container, name) {
    const row = rowByName(container, name);
    return row ? row.dataset.ttsKind : null;
}

/** Independent class search (the test's own oracle, not the app's). */
function findClass(node, cls) {
    const out = [];
    const walk = (n) => {
        if (!n || typeof n !== "object") return;
        if (typeof n.className === "string" && n.className.split(/\s+/).includes(cls)) {
            out.push(n);
        }
        for (const child of n.children || []) walk(child);
    };
    walk(node);
    return out;
}

/** Concatenated text of a node and all descendants. */
function textOf(node) {
    const parts = [];
    const walk = (n) => {
        if (!n || typeof n !== "object") return;
        if (n.textContent) parts.push(String(n.textContent));
        for (const child of n.children || []) walk(child);
    };
    walk(node);
    return parts.join("");
}

/** The last PUT /api/settings issued (parsed body), or undefined. */
function putCall(h) {
    return h.fetchStub.calls.find((c) => c.method === "PUT" && c.url === "/api/settings");
}

function capabilitiesCalls(h) {
    // Includes ?base_url= probe calls (the reconnect button / URL change).
    return h.fetchStub.calls.filter((c) => c.url.startsWith("/api/tts/capabilities"));
}

/** Flush the microtask queue (async event handlers are fire-and-forget). */
function settle() {
    return new Promise((resolve) => setImmediate(resolve));
}

/**
 * Re-materialize a value that came out of the vm realm. deepStrictEqual
 * compares prototypes, and vm-realm objects/arrays carry the context's
 * Object/Array prototypes, not the host's — a JSON round-trip rebuilds
 * them as plain host values.
 */
function fromVm(value) {
    return JSON.parse(JSON.stringify(value));
}

/*
 * The contract under test, stated as test oracles (mirrors plan T4 and
 * the T9 table in tts-serve/docs/01-server-generification.md §4.3).
 */
const APP_MANAGED_FIELDS = ["text", "audio_base64", "reference_text", "language"];

const EXPECTED_SPLITS = {
    chatterbox: { renderable: 4, advanced: 3 },
    omnivoice: { renderable: 3, advanced: 1 },
    qwen3: { renderable: 2, advanced: 3 },
    dots: { renderable: 5, advanced: 0 },
};

const ADVANCED_NAMES = {
    chatterbox: ["repetition_penalty", "min_p", "top_p"],
    omnivoice: ["denoise"],
    qwen3: ["x_vector_only_mode", "top_p", "repetition_penalty"],
    dots: [],
};

const EXPECTED_WIDGET_KINDS = {
    chatterbox: {
        seed: "number", // integer, span 999 > 100, no step
        exaggeration: "slider",
        cfg_weight: "slider",
        temperature: "slider",
        repetition_penalty: "slider",
        min_p: "slider",
        top_p: "slider",
    },
    omnivoice: {
        seed: "number",
        num_steps: "slider",
        guidance_scale: "slider",
        denoise: "checkbox",
    },
    qwen3: {
        seed: "number",
        x_vector_only_mode: "checkbox",
        temperature: "number", // null default -> blankable, NOT a slider
        top_p: "number",
        repetition_penalty: "number",
    },
    dots: {
        seed: "number",
        num_steps: "slider",
        guidance_scale: "slider",
        speaker_scale: "slider",
        ode_method: "select",
    },
};

/* ==========================================================================
    selectTtsParams — document selection & version gate (T8/T10)
    ========================================================================== */

test("selectTtsParams_allFourFixtures_splitsRenderableAndAdvanced_appManagedFieldsNeverListed", () => {
    const h = createSettingsHarness();
    for (const name of Object.keys(EXPECTED_SPLITS)) {
        const doc = loadFixture(name);
        const { renderable, advanced, error } = h.sandbox.selectTtsParams(doc);

        assert.equal(error, null, `${name}: unexpected error`);
        assert.equal(renderable.length, EXPECTED_SPLITS[name].renderable, `${name}: renderable count`);
        assert.equal(advanced.length, EXPECTED_SPLITS[name].advanced, `${name}: advanced count`);
        assert.deepEqual(
            fromVm(advanced.map((s) => s.name)).sort(),
            [...ADVANCED_NAMES[name]].sort(),
            `${name}: advanced names`,
        );
        const allNames = [...renderable, ...advanced].map((s) => s.name);
        for (const managed of APP_MANAGED_FIELDS) {
            assert.ok(
                !allNames.includes(managed),
                `${name}: app-managed '${managed}' must never be rendered`,
            );
        }
    }
});

test("selectTtsParams_missingOrMalformedDoc_returnsErrorAndNoLists", () => {
    const h = createSettingsHarness();
    for (const bad of [null, undefined, "nope", 42, [], { parameters: [] }, { schema_version: "2" }]) {
        const { renderable, advanced, error } = h.sandbox.selectTtsParams(bad);
        assert.ok(error, `expected an error for ${JSON.stringify(bad)}`);
        assert.equal(renderable.length, 0);
        assert.equal(advanced.length, 0);
    }
});

test("selectTtsParams_schemaVersionTooNew_orTooOld_fallsBackToMinimalModeNamingTheVersion", () => {
    const h = createSettingsHarness();
    const base = loadFixture("omnivoice");

    const newer = h.sandbox.selectTtsParams({ ...base, schema_version: 3 });
    assert.match(newer.error, /v3/);
    assert.match(newer.error, /supports up to v2/);

    const older = h.sandbox.selectTtsParams({ ...base, schema_version: 1 });
    assert.match(older.error, /v1/);
    assert.match(older.error, /requires v2/);
});

test("selectTtsParams_malformedParameterEntries_areSkippedNotFatal", () => {
    const h = createSettingsHarness();
    const doc = docWith(
        null,
        42,
        { name: 7, type: "boolean" },
        { type: "boolean", default: false },
        spec({ name: "good_param", type: "boolean" }),
    );
    const { renderable, advanced, error } = h.sandbox.selectTtsParams(doc);
    assert.equal(error, null);
    assert.deepEqual(fromVm(renderable.map((s) => s.name)), ["good_param"]);
    assert.equal(advanced.length, 0);
});

/* ==========================================================================
    widgetFor — the T9 widget table
    ========================================================================== */

test("widgetFor_boolean_isCheckbox", () => {
    const h = createSettingsHarness();
    assert.deepEqual(fromVm(h.sandbox.widgetFor(spec({ type: "boolean" }))), { kind: "checkbox" });
});

test("widgetFor_stringWithEnum_isSelect_blankOnlyWhenDefaultIsNull", () => {
    const h = createSettingsHarness();
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "string", enum: ["a", "b"], default: "a" }))),
        { kind: "select", options: ["a", "b"], blank: false },
    );
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "string", enum: ["a", "b"], default: null }))),
        { kind: "select", options: ["a", "b"], blank: true },
    );
});

test("widgetFor_stringWithoutEnum_isText", () => {
    const h = createSettingsHarness();
    assert.deepEqual(fromVm(h.sandbox.widgetFor(spec({ type: "string" }))), { kind: "text" });
});

test("widgetFor_numberWithRangeStepAndNonNullDefault_isSlider", () => {
    const h = createSettingsHarness();
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "number", min: 0, max: 10, step: 1, default: 5 }))),
        { kind: "slider" },
    );
});

test("widgetFor_numberWithNullDefaultOrMissingStep_isBlankableNumberInput", () => {
    // Qwen3's temperature/top_p have bounds+step but a null default: they
    // must stay blankable ("let the engine decide"), not sliders with no
    // legal resting position.
    const h = createSettingsHarness();
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "number", min: 0, max: 2, step: 0.05, default: null }))),
        { kind: "number" },
    );
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "number", min: 0, max: 2, default: 1 }))),
        { kind: "number" },
    );
});

test("widgetFor_integerWithRangeAndStep_isSlider", () => {
    const h = createSettingsHarness();
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "integer", min: 4, max: 128, step: 4, default: 32 }))),
        { kind: "slider" },
    );
});

test("widgetFor_integerWithoutStep_sliderOnlyOnSmallSpans", () => {
    const h = createSettingsHarness();
    // span 99 <= 100 -> a slider is usable
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "integer", min: 1, max: 100, default: null }))),
        { kind: "slider" },
    );
    // span 999 > 100 -> a number input is honest (this is every fixture's `seed`)
    assert.deepEqual(
        fromVm(h.sandbox.widgetFor(spec({ type: "integer", min: 1, max: 1000, default: null }))),
        { kind: "number" },
    );
});

test("widgetFor_unrecognizedType_isRawJsonEscapeHatch", () => {
    const h = createSettingsHarness();
    assert.deepEqual(fromVm(h.sandbox.widgetFor(spec({ type: "object" }))), { kind: "json" });
});

test("widgetFor_allFourFixtures_matchTheT9OracleTable", () => {
    const h = createSettingsHarness();
    for (const name of Object.keys(EXPECTED_WIDGET_KINDS)) {
        const doc = loadFixture(name);
        for (const specName of Object.keys(EXPECTED_WIDGET_KINDS[name])) {
            const specEntry = doc.parameters.find((p) => p && p.name === specName);
            assert.ok(specEntry, `${name}: fixture missing ${specName}`);
            assert.equal(
                h.sandbox.widgetFor(specEntry).kind,
                EXPECTED_WIDGET_KINDS[name][specName],
                `${name}.${specName}: widget kind`,
            );
        }
    }
});

/* ==========================================================================
    collectTtsParamValues — "blank = let the engine decide"
    ========================================================================== */

test("collectTtsParamValues_untouchedForm_alwaysSentPresent_blanksOmitted", () => {
    const h = createSettingsHarness();
    const doc = docWith(
        spec({ name: "flag", type: "boolean", default: false }),
        spec({ name: "picks", type: "string", enum: ["x", "y"], default: "x" }),
        spec({ name: "free_num", type: "number" }),
        spec({ name: "free_text", type: "string" }),
        spec({ name: "dial", type: "number", min: 0, max: 10, step: 1, default: 5 }),
    );
    const container = renderDoc(h, doc);

    const values = h.sandbox.collectTtsParamValues(container);

    // checkbox + select-with-default + slider are always present; the
    // blank number/text fields are absent (the engine decides).
    assert.deepEqual(fromVm(values), { flag: false, picks: "x", dial: 5 });
});

test("collectTtsParamValues_editedWidgets_valuesCoercedToSpecTypes", () => {
    const h = createSettingsHarness();
    const doc = docWith(
        spec({ name: "flag", type: "boolean", default: true }),
        spec({ name: "picks", type: "string", enum: ["x", "y"], default: "x" }),
        spec({ name: "free_num", type: "number" }),
        spec({ name: "free_text", type: "string" }),
        spec({ name: "dial", type: "number", min: 0, max: 10, step: 1, default: 5 }),
    );
    const container = renderDoc(h, doc);

    rowByName(container, "flag").widgetEl.checked = false;
    rowByName(container, "picks").widgetEl.value = "y";
    rowByName(container, "free_num").widgetEl.value = "1.5";
    rowByName(container, "free_text").widgetEl.value = "  padded  ";
    rowByName(container, "dial").widgetEl.value = "8";

    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), {
        flag: false,
        picks: "y",
        free_num: 1.5,
        free_text: "padded",
        dial: 8,
    });
});

test("collectTtsParamValues_blankableSelect_emptyValueOmitted_chosenValueSent", () => {
    const h = createSettingsHarness();
    const doc = docWith(spec({ name: "solver", type: "string", enum: ["a", "b"], default: null }));
    const container = renderDoc(h, doc);
    const widget = rowByName(container, "solver").widgetEl;
    assert.equal(widget.value, "", "blankable select must start on the blank option");

    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), {});

    widget.value = "b";
    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), { solver: "b" });
});

test("collectTtsParamValues_jsonHatch_parsesValidJson_invalidJsonReturnedRawForValidation", () => {
    const h = createSettingsHarness();
    const doc = docWith(spec({ name: "blob", type: "object" }));
    const container = renderDoc(h, doc);
    const widget = rowByName(container, "blob").widgetEl;

    widget.value = '{"k": 1}';
    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), { blob: { k: 1 } });

    widget.value = "definitely not json";
    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), {
        blob: "definitely not json",
    });
});

test("collectTtsParamValues_numberInputNonIntegerValue_passesThroughForValidation", () => {
    const h = createSettingsHarness();
    const doc = docWith(spec({ name: "count", type: "integer", min: 1, max: 1000, default: null }));
    const container = renderDoc(h, doc);

    rowByName(container, "count").widgetEl.value = "3.7";

    // Collection does not silently coerce — validation names the offense.
    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), { count: 3.7 });
});

test("collectTtsParamValues_emptyContainer_returnsEmptyObject", () => {
    const h = createSettingsHarness();
    assert.deepEqual(
        fromVm(h.sandbox.collectTtsParamValues(h.document.createElement("div"))),
        {},
    );
});

test("collectTtsParamValues_browserStyleNonArrayChildren_stillFindsNestedRows", () => {
    // The bug this locks in: in a real DOM, element.children is an
    // HTMLCollection — iterable and array-like, but Array.isArray() ===
    // false. The old findTtsParamRows gated its descent on
    // Array.isArray(children), so it never left the top container and every
    // save collected {} (silently wiping tts.parameters). The harness stub
    // mirrors the browser (see makeChildList); this test states the
    // invariant outright, and the nested row is at depth 2 like the real
    // render (container -> param list -> row).
    const h = createSettingsHarness();
    const doc = docWith(
        spec({ name: "dial", type: "number", min: 0, max: 10, step: 1, default: 5 }),
        spec({ name: "flag", type: "boolean", default: false }),
    );
    const container = renderDoc(h, doc);

    assert.ok(
        !Array.isArray(container.children),
        "harness must mirror the browser: children is NOT an Array",
    );

    const values = h.sandbox.collectTtsParamValues(container);
    assert.deepEqual(fromVm(values), { dial: 5, flag: false });
});

/* ==========================================================================
    validateTtsParamValues — mirrors the backend T7 rules
    ========================================================================== */

test("validateTtsParamValues_noValuesOrNoDoc_returnsNull", () => {
    const h = createSettingsHarness();
    const doc = loadFixture("omnivoice");
    assert.equal(h.sandbox.validateTtsParamValues({}, doc), null);
    assert.equal(h.sandbox.validateTtsParamValues(null, doc), null);
    // No document (server unreachable) -> nothing to check against; the
    // server's own 422 is the backstop.
    assert.equal(h.sandbox.validateTtsParamValues({ num_steps: 3 }, null), null);
    assert.equal(h.sandbox.validateTtsParamValues({}, null), null);
});

test("validateTtsParamValues_unknownParameter_namedInError", () => {
    const h = createSettingsHarness();
    const error = h.sandbox.validateTtsParamValues({ bogus: 1 }, loadFixture("omnivoice"));
    assert.match(error, /unknown TTS parameter 'bogus'/);
});

test("validateTtsParamValues_outOfBounds_reportsMinAndMax", () => {
    const h = createSettingsHarness();
    const doc = loadFixture("omnivoice"); // num_steps: 4..128
    assert.match(h.sandbox.validateTtsParamValues({ num_steps: 3 }, doc), /must be >= 4, got 3/);
    assert.match(h.sandbox.validateTtsParamValues({ num_steps: 129 }, doc), /must be <= 128, got 129/);
});

test("validateTtsParamValues_wrongType_eachOffenseNamed", () => {
    const h = createSettingsHarness();
    const doc = loadFixture("dots");
    assert.match(h.sandbox.validateTtsParamValues({ seed: 1.5 }, doc), /'seed' must be an integer/);
    assert.match(h.sandbox.validateTtsParamValues({ seed: "7" }, doc), /'seed' must be an integer/);
    assert.match(h.sandbox.validateTtsParamValues({ ode_method: 42 }, doc), /'ode_method' must be a string/);
    assert.match(
        h.sandbox.validateTtsParamValues({ ode_method: "verlet" }, doc),
        /must be one of euler, midpoint, rk4/,
    );
    assert.match(
        h.sandbox.validateTtsParamValues({ denoise: 1 }, loadFixture("omnivoice")),
        /'denoise' must be a boolean/,
    );
});

test("validateTtsParamValues_multipleOffenders_allNamedInSingleMessage", () => {
    const h = createSettingsHarness();
    const doc = loadFixture("omnivoice");
    const error = h.sandbox.validateTtsParamValues({ num_steps: 3, seed: 0 }, doc);
    assert.match(error, /'num_steps'/);
    assert.match(error, /'seed'/);
});

test("validateTtsParamValues_jsonHatchValue_invalidJsonIsError_validJsonPasses", () => {
    const h = createSettingsHarness();
    const doc = docWith(spec({ name: "blob", type: "object" }));
    assert.match(
        h.sandbox.validateTtsParamValues({ blob: "not json" }, doc),
        /'blob' must be a valid JSON value/,
    );
    assert.equal(h.sandbox.validateTtsParamValues({ blob: { k: 1 } }, doc), null);
    assert.equal(h.sandbox.validateTtsParamValues({ blob: '"quoted"' }, doc), null);
});

/* ==========================================================================
    renderTtsParameters — DOM builder
    ========================================================================== */

test("renderTtsParameters_nullDoc_clearsPreviouslyRenderedRows", () => {
    const h = createSettingsHarness();
    const container = h.document.createElement("div");
    h.sandbox.renderTtsParameters(loadFixture("omnivoice"), container, {});
    assert.ok(rows(container).length > 0, "expected rows before the clear");

    h.sandbox.renderTtsParameters(null, container, {});

    assert.equal(container.children.length, 0);
});

test("renderTtsParameters_allFourFixtures_rowCountsKindsAndAppManagedExclusionMatch", () => {
    const h = createSettingsHarness();
    for (const name of Object.keys(EXPECTED_SPLITS)) {
        const container = renderDoc(h, loadFixture(name));
        const allRows = rows(container);
        assert.equal(
            allRows.length,
            EXPECTED_SPLITS[name].renderable + EXPECTED_SPLITS[name].advanced,
            `${name}: row count`,
        );
        for (const [specName, kind] of Object.entries(EXPECTED_WIDGET_KINDS[name])) {
            assert.equal(kindOf(container, specName), kind, `${name}.${specName}: kind`);
        }
        for (const managed of APP_MANAGED_FIELDS) {
            assert.equal(
                rowByName(container, managed),
                undefined,
                `${name}: '${managed}' must have no row`,
            );
        }
    }
});

test("renderTtsParameters_advancedSpecs_liveInAClosedDetailsDisclosure", () => {
    const h = createSettingsHarness();
    const container = renderDoc(h, loadFixture("omnivoice")); // denoise is the only advanced spec

    const details = findClass(container, "tts-advanced");
    assert.equal(details.length, 1);
    assert.equal(details[0].open, false, "Advanced must start collapsed");
    const summary = [...details[0].children].find((c) => c.tagName === "SUMMARY");
    assert.ok(summary, "expected a <summary> element");
    assert.equal(summary.textContent, "Advanced (1)");
    // The row is inside the disclosure but still collectable by name.
    assert.equal(kindOf(container, "denoise"), "checkbox");
});

test("renderTtsParameters_versionGatedDoc_noticeInsteadOfWidgets", () => {
    const h = createSettingsHarness();
    const doc = { ...loadFixture("omnivoice"), schema_version: 3 };
    const container = renderDoc(h, doc);

    assert.equal(container.children.length, 1);
    assert.equal(container.children[0].className, "tts-notice");
    assert.match(container.children[0].textContent, /v3/);
    assert.equal(rows(container).length, 0);
});

test("renderTtsParameters_engineWithOnlyAppManagedParams_explainsThereIsNothingToRender", () => {
    const h = createSettingsHarness();
    const doc = docWith(
        spec({ name: "text", type: "string" }),
        spec({ name: "audio_base64", type: "string" }),
    );
    const container = renderDoc(h, doc);

    assert.equal(container.children.length, 1);
    assert.match(container.children[0].textContent, /no user-configurable parameters/);
    assert.equal(rows(container).length, 0);
});

test("renderTtsParameters_savedValues_preFillWidgets_docDefaultsFillTheRest", () => {
    const h = createSettingsHarness();
    const container = renderDoc(h, loadFixture("omnivoice"), {
        num_steps: 16,
        denoise: false,
        seed: 42,
    });

    assert.equal(rowByName(container, "num_steps").widgetEl.value, "16", "saved slider value");
    assert.equal(rowByName(container, "denoise").widgetEl.checked, false, "saved checkbox value");
    assert.equal(rowByName(container, "seed").widgetEl.value, "42", "saved number value");
    assert.equal(
        rowByName(container, "guidance_scale").widgetEl.value,
        "2",
        "unsaved slider rests at the doc default",
    );
});

test("renderTtsParameters_savedValueFromAnotherEngine_isNotRevived", () => {
    const h = createSettingsHarness();
    // cfg_weight only exists on Chatterbox; num_steps exists on both.
    const container = renderDoc(h, loadFixture("omnivoice"), { cfg_weight: 0.3, num_steps: 16 });

    assert.equal(rowByName(container, "cfg_weight"), undefined, "stale name must have no row");
    assert.deepEqual(fromVm(h.sandbox.collectTtsParamValues(container)), {
        num_steps: 16,
        guidance_scale: 2,
        denoise: true,
    });
});

test("renderTtsParameters_nullDefaultNumberFields_renderBlankNotAtEngineDefault", () => {
    const h = createSettingsHarness();
    const container = renderDoc(h, loadFixture("qwen3"));

    assert.equal(rowByName(container, "temperature").widgetEl.value, "");
    assert.equal(rowByName(container, "top_p").widgetEl.value, "");
    assert.equal(rowByName(container, "seed").widgetEl.value, "");
});

test("renderTtsParameters_selectWidget_listsEveryEnumOptionAndPreselectsDefault", () => {
    const h = createSettingsHarness();
    const container = renderDoc(h, loadFixture("dots"));
    const widget = rowByName(container, "ode_method").widgetEl;

    assert.equal(widget.tagName, "SELECT");
    assert.deepEqual(fromVm([...widget.children].map((o) => o.value)), ["euler", "midpoint", "rk4"]);
    assert.equal(widget.value, "euler", "non-null default preselects, no blank option");
});

test("renderTtsParameters_sliderReadout_followsTheSliderValue", () => {
    const h = createSettingsHarness();
    const container = renderDoc(h, loadFixture("omnivoice"));
    const row = rowByName(container, "num_steps");
    const wrap = [...row.children].find((c) => c.className === "tts-slider-row");
    assert.ok(wrap, "slider row must wrap the range input");
    const [slider, readout] = wrap.children;
    assert.equal(readout.textContent, "32", "readout starts at the doc default");

    slider.value = "48";
    slider.dispatch("input", {});

    assert.equal(readout.textContent, "48");
});

/* ==========================================================================
    renderTtsInfo — engine identity & status lines
    ========================================================================== */

test("renderTtsInfo_omnivoiceDoc_showsIdentityAndSampleRate_withoutWatermarkWarning", () => {
    const h = createSettingsHarness();
    const info = h.document.createElement("div");
    h.sandbox.renderTtsInfo(loadFixture("omnivoice"), info);

    const text = textOf(info);
    assert.ok(text.includes("omnivoice"), text);
    assert.ok(text.includes("k2-fsa/OmniVoice"), text);
    assert.ok(text.includes("cuda"), text);
    assert.ok(text.includes("24000 Hz"), text);
    assert.equal(findClass(info, "tts-info-warn").length, 0);
});

test("renderTtsInfo_watermarkedEngine_showsTheDisclosure", () => {
    const h = createSettingsHarness();
    const info = h.document.createElement("div");
    h.sandbox.renderTtsInfo(loadFixture("chatterbox"), info);

    const warns = findClass(info, "tts-info-warn");
    assert.equal(warns.length, 1);
    assert.equal(
        warns[0].textContent,
        "This engine applies a neural watermark to the audio it generates.",
    );
});

test("renderTtsInfo_nullDocWithNote_singleStatusLine", () => {
    const h = createSettingsHarness();
    const info = h.document.createElement("div");
    h.sandbox.renderTtsInfo(null, info, "TTS is not configured.");

    assert.equal(info.children.length, 1);
    assert.equal(info.children[0].className, "tts-info-line");
    assert.equal(info.children[0].textContent, "TTS is not configured.");
});

test("renderTtsInfo_docWithNote_noteSitsBelowTheInfoBlock", () => {
    const h = createSettingsHarness();
    const info = h.document.createElement("div");
    const note = "Engine switched — unsaved parameter edits were discarded.";
    h.sandbox.renderTtsInfo(loadFixture("omnivoice"), info, note);

    assert.equal(info.children.length, 2);
    assert.equal(info.children[0].className, "tts-info-block");
    assert.equal(info.children[1].className, "tts-info-line");
    assert.equal(info.children[1].textContent, note);
});

/* ==========================================================================
    settings.js — modal wiring (open/refresh/submit against the stub API)
    ========================================================================== */

test("openSettings_ttsEnabledAndReachable_rendersInfoBlockAndParameterRows", async () => {
    // GIVEN the default fixture state: TTS enabled at localhost:8000,
    // saved parameters {num_steps: 16}, capabilities doc = omnivoice:
    const h = createSettingsHarness();

    // WHEN the user opens the Servers modal:
    await h.sandbox.openSettings();

    // THEN the capabilities proxy was hit exactly once, the info block
    // shows the engine identity, and the saved value pre-fills a widget:
    assert.equal(capabilitiesCalls(h).length, 1);
    const infoText = textOf(h.elementById("sf-tts-info"));
    assert.ok(infoText.includes("omnivoice"), infoText);
    assert.ok(infoText.includes("k2-fsa/OmniVoice"), infoText);
    const container = h.elementById("sf-tts-params");
    assert.equal(kindOf(container, "num_steps"), "slider");
    assert.equal(rowByName(container, "num_steps").widgetEl.value, "16");
    assert.deepEqual(h.get("ttsCapabilitiesDoc"), h.fetchStub.state.capabilities);
});

test("openSettings_capabilitiesEndpoint503_statusLineAndNoWidgets", async () => {
    const h = createSettingsHarness();
    h.fetchStub.state.capabilitiesStatus = 503;

    await h.sandbox.openSettings();

    const info = h.elementById("sf-tts-info");
    assert.equal(info.children.length, 1);
    assert.equal(
        info.children[0].textContent,
        "TTS server not reachable — parameter options unavailable",
    );
    assert.equal(h.elementById("sf-tts-params").children.length, 0);
    assert.equal(h.get("ttsCapabilitiesDoc"), null);
});

test("openSettings_capabilitiesFetchThrows_statusLineAndNoWidgets", async () => {
    const h = createSettingsHarness();
    h.fetchStub.state.capabilitiesThrow = true;

    await h.sandbox.openSettings();

    const info = h.elementById("sf-tts-info");
    assert.equal(info.children.length, 1);
    assert.equal(
        info.children[0].textContent,
        "TTS server not reachable — parameter options unavailable",
    );
    assert.equal(h.elementById("sf-tts-params").children.length, 0);
});

test("openSettings_ttsDisabled_noCapabilitiesFetch_statusLine", async () => {
    const h = createSettingsHarness();
    h.fetchStub.state.settings.tts.enabled = false;

    await h.sandbox.openSettings();

    assert.equal(capabilitiesCalls(h).length, 0, "disabled TTS must not probe the server");
    assert.equal(h.elementById("sf-tts-info").children.length, 1);
    assert.equal(h.elementById("sf-tts-info").children[0].textContent, "TTS is not configured.");
    assert.equal(h.elementById("sf-tts-params").children.length, 0);
});

test("openSettings_blankTtsBaseUrl_noCapabilitiesFetch_statusLine", async () => {
    const h = createSettingsHarness();
    h.fetchStub.state.settings.tts.base_url = "   ";

    await h.sandbox.openSettings();

    assert.equal(capabilitiesCalls(h).length, 0);
    assert.equal(h.elementById("sf-tts-info").children.length, 1);
    assert.equal(
        h.elementById("sf-tts-info").children[0].textContent,
        "TTS Base URL is not set — parameter options unavailable.",
    );
});

test("baseUrlChange_refetchesCapabilities_showsSwitchNote_replacesStaleWidgets", async () => {
    const h = createSettingsHarness(); // omnivoice first
    await h.sandbox.openSettings();
    assert.equal(capabilitiesCalls(h).length, 1);
    assert.equal(kindOf(h.elementById("sf-tts-params"), "num_steps"), "slider");

    // WHEN the user points the Base URL at a different engine:
    h.fetchStub.state.capabilitiesByUrl.set("http://dots.local:8000", {
        doc: loadFixture("dots"),
    });
    h.elementById("sf-tts-base-url").value = "http://dots.local:8000";
    h.elementById("sf-tts-base-url").dispatch("change", {});
    await settle();

    // THEN the document was refetched — probing the NEW (unsaved) URL via
    // ?base_url=, since the backend's plain route only knows the SAVED one —
    // the switch is disclosed, and the parameter set is the new engine's
    // (stale widgets gone):
    assert.equal(capabilitiesCalls(h).length, 2, "URL change must refetch capabilities");
    const probeCall = capabilitiesCalls(h)[1];
    assert.ok(
        probeCall.url.startsWith("/api/tts/capabilities?base_url="),
        `URL change must probe the new url, got ${probeCall.url}`,
    );
    const infoLines = findClass(h.elementById("sf-tts-info"), "tts-info-line");
    assert.equal(infoLines.length, 1);
    assert.equal(
        infoLines[0].textContent,
        "Engine switched — unsaved parameter edits were discarded.",
    );
    const container = h.elementById("sf-tts-params");
    assert.equal(kindOf(container, "ode_method"), "select");
    assert.equal(kindOf(container, "guidance_scale"), "slider");
    assert.equal(rowByName(container, "denoise"), undefined, "omnivoice-only knob must be gone");
    assert.equal(findClass(container, "tts-advanced").length, 0, "dots has no advanced specs");
    // num_steps exists on both engines, so the saved value re-applies:
    assert.equal(rowByName(container, "num_steps").widgetEl.value, "16");
});

test("baseUrlChange_withNoPriorDoc_rendersNewDocWithoutSwitchNote", async () => {
    const h = createSettingsHarness();
    h.fetchStub.state.capabilitiesStatus = 503;
    await h.sandbox.openSettings(); // status line, no parameter form on screen

    // The change listener probes the field's url via ?base_url=:
    h.fetchStub.state.capabilitiesByUrl.set("http://localhost:8000", {
        doc: loadFixture("omnivoice"),
    });
    h.elementById("sf-tts-base-url").value = "http://localhost:8000";
    h.elementById("sf-tts-base-url").dispatch("change", {});
    await settle();

    // "Edits discarded" only makes sense if edits were on screen.
    const info = h.elementById("sf-tts-info");
    assert.equal(findClass(info, "tts-info-block").length, 1, "doc now rendered");
    const lines = findClass(info, "tts-info-line");
    assert.ok(
        !lines.some((l) => l.textContent.includes("Engine switched")),
        `unexpected switch note: ${lines.map((l) => l.textContent).join(" | ")}`,
    );
    assert.equal(kindOf(h.elementById("sf-tts-params"), "num_steps"), "slider");
});

test("ttsEnabledToggledOffMidSession_clearsWidgets_showsStatusLine", async () => {
    const h = createSettingsHarness();
    await h.sandbox.openSettings();
    assert.ok(rows(h.elementById("sf-tts-params")).length > 0);

    h.elementById("sf-tts-enabled").checked = false;
    h.elementById("sf-tts-enabled").dispatch("change", {});
    await settle();

    assert.equal(h.get("ttsCapabilitiesDoc"), null);
    assert.equal(h.elementById("sf-tts-params").children.length, 0);
    assert.equal(h.elementById("sf-tts-info").children.length, 1);
    assert.equal(h.elementById("sf-tts-info").children[0].textContent, "TTS is not configured.");
});

test("submitSettings_parameterEdits_goOutAsCollectedTypedValues", async () => {
    const h = createSettingsHarness();
    await h.sandbox.openSettings();

    // WHEN the user edits parameters and saves:
    const container = h.elementById("sf-tts-params");
    rowByName(container, "num_steps").widgetEl.value = "48";
    rowByName(container, "guidance_scale").widgetEl.value = "3.5";
    rowByName(container, "seed").widgetEl.value = "7";
    // denoise is left on its doc default (checked = true)
    h.elementById("settings-form").dispatch("submit", { preventDefault() {} });
    await settle();

    // THEN the PUT carries the collected values with engine-expected types:
    const call = putCall(h);
    assert.ok(call, "expected a PUT /api/settings");
    assert.deepEqual(call.body.tts.parameters, {
        num_steps: 48,
        guidance_scale: 3.5,
        seed: 7,
        denoise: true,
    });
    assert.equal(
        h.elementById("settings-overlay").classList.contains("hidden"),
        true,
        "modal closes on success",
    );
});

test("submitSettings_outOfBoundsValue_blockedClientSide_noRequest", async () => {
    const h = createSettingsHarness();
    await h.sandbox.openSettings();

    // num_steps is 4..128; a real browser would clamp the slider, but the
    // client-side check must catch a bypass (old widget, pasted value).
    rowByName(h.elementById("sf-tts-params"), "num_steps").widgetEl.value = "3";
    h.elementById("settings-form").dispatch("submit", { preventDefault() {} });
    await settle();

    assert.equal(putCall(h), undefined, "no PUT may go out for an invalid value");
    const err = h.elementById("settings-error");
    assert.equal(err.classList.contains("hidden"), false);
    assert.match(err.textContent, /'num_steps' must be >= 4/);
});

test("submitSettings_noCapabilitiesDoc_savedParametersPassedThroughNotWiped", async () => {
    // GIVEN the capabilities proxy is down (503): the section renders a
    // status line and no widgets, but the server still holds {num_steps: 16}:
    const h = createSettingsHarness();
    h.fetchStub.state.capabilitiesStatus = 503;
    await h.sandbox.openSettings();
    assert.equal(h.get("ttsCapabilitiesDoc"), null);

    // WHEN the user saves (an unrelated edit, e.g. the LLM model):
    h.elementById("settings-form").dispatch("submit", { preventDefault() {} });
    await settle();

    // THEN the saved parameters pass through untouched — sending {} here
    // would silently wipe them on every unrelated save:
    const call = putCall(h);
    assert.ok(call, "expected a PUT /api/settings");
    assert.deepEqual(call.body.tts.parameters, { num_steps: 16 });
});

test("openSettings_saveParameters_reopen_preservesSavedValues_notDocDefaults", async () => {
    // The user's bug report, end to end: open the modal, change
    // num_steps/guidance_scale, save, reopen — and find the values back.
    // Before the findTtsParamRows fix, collection returned {} in real
    // browsers, the save wiped tts.parameters, and the reopened modal
    // showed the engine's doc defaults (32 / 2) instead of the saved values.
    const h = createSettingsHarness(); // saved parameters: {num_steps: 16}
    await h.sandbox.openSettings();

    // WHEN the user edits the sliders and saves:
    const container = h.elementById("sf-tts-params");
    rowByName(container, "num_steps").widgetEl.value = "48";
    rowByName(container, "guidance_scale").widgetEl.value = "3.5";
    h.elementById("settings-form").dispatch("submit", { preventDefault() {} });
    await settle();

    // THEN the PUT carried the edited values and the backend persisted them:
    const call = putCall(h);
    assert.ok(call, "expected a PUT /api/settings");
    const sent = fromVm(call.body.tts.parameters);
    assert.equal(sent.num_steps, 48);
    assert.equal(sent.guidance_scale, 3.5);
    assert.deepEqual(fromVm(h.fetchStub.state.settings.tts.parameters), sent);

    // WHEN the user reopens the modal:
    await h.sandbox.openSettings();

    // THEN the widgets hold the SAVED values — not the doc defaults (32/2):
    const reopened = h.elementById("sf-tts-params");
    assert.equal(
        rowByName(reopened, "num_steps").widgetEl.value,
        "48",
        "saved num_steps must survive the round trip, not revert to the doc default",
    );
    assert.equal(
        rowByName(reopened, "guidance_scale").widgetEl.value,
        "3.5",
        "saved guidance_scale must survive the round trip, not revert to the doc default",
    );
});

test("capRefreshClick_probesFieldUrl_rendersNewEngine_withSwitchNote", async () => {
    // The feature request: a new engine is at a DIFFERENT, unsaved URL.
    // Clicking Refresh must probe THAT url (not the saved one) and re-render
    // in place — no save + reopen round trip.
    const h = createSettingsHarness(); // saved url: http://localhost:8000 (omnivoice)
    await h.sandbox.openSettings();
    assert.equal(kindOf(h.elementById("sf-tts-params"), "num_steps"), "slider");

    // WHEN the user types the new engine's url and clicks Refresh:
    h.fetchStub.state.capabilitiesByUrl.set("http://dots.local:8000", {
        doc: loadFixture("dots"),
    });
    h.elementById("sf-tts-base-url").value = "http://dots.local:8000";
    h.elementById("sf-tts-cap-refresh").dispatch("click", {});
    await settle();

    // THEN the request probed the field's url, the doc rendered in place,
    // and the switch was disclosed (saved params re-applied for shared names):
    const calls = capabilitiesCalls(h);
    assert.equal(calls.length, 2, "click must issue exactly one capabilities fetch");
    assert.ok(
        calls[1].url.startsWith("/api/tts/capabilities?base_url=http%3A%2F%2Fdots.local%3A8000"),
        `expected a probe of the field's url, got ${calls[1].url}`,
    );
    const container = h.elementById("sf-tts-params");
    assert.equal(kindOf(container, "ode_method"), "select", "dots widgets must be on screen");
    assert.equal(
        findClass(h.elementById("sf-tts-info"), "tts-info-line")[0].textContent,
        "Engine switched — unsaved parameter edits were discarded.",
    );
    assert.equal(rowByName(container, "num_steps").widgetEl.value, "16", "saved value re-applies");
});

test("capRefreshClick_sameUrl_keepsInFlightEdits_noSwitchNote", async () => {
    // A same-engine refetch (reconnect after a hiccup) must NOT clobber
    // parameter edits the user has not saved yet — and must not claim the
    // engine switched.
    const h = createSettingsHarness();
    await h.sandbox.openSettings();

    // WHEN the user edits a slider, then clicks Refresh on the SAME url:
    rowByName(h.elementById("sf-tts-params"), "num_steps").widgetEl.value = "48";
    h.fetchStub.state.capabilitiesByUrl.set("http://localhost:8000", {
        doc: loadFixture("omnivoice"),
    });
    h.elementById("sf-tts-cap-refresh").dispatch("click", {});
    await settle();

    // THEN the in-flight edit survives and no switch note is shown:
    assert.equal(
        rowByName(h.elementById("sf-tts-params"), "num_steps").widgetEl.value,
        "48",
        "same-URL refetch must keep in-flight edits, not re-fill from saved values",
    );
    const lines = findClass(h.elementById("sf-tts-info"), "tts-info-line");
    assert.ok(
        !lines.some((l) => l.textContent.includes("Engine switched")),
        `same-URL refetch is not an engine switch: ${lines.map((l) => l.textContent).join(" | ")}`,
    );
});

test("capRefreshClick_sameUrlSpelledWithTrailingSlash_keepsInFlightEdits_noSwitchNote", async () => {
    // The backend normalizes urls on save (clean_base_url strips trailing
    // slashes), so the SAVED url has no trailing slash — but the user may
    // type the same engine's url with one. A slash difference is not an
    // engine switch: no "switched" note, no discarding of in-flight edits.
    const h = createSettingsHarness(); // saved url: http://localhost:8000
    await h.sandbox.openSettings();

    // WHEN the user edits a slider and refreshes the same engine spelled
    // with a trailing slash:
    rowByName(h.elementById("sf-tts-params"), "num_steps").widgetEl.value = "48";
    h.fetchStub.state.capabilitiesByUrl.set("http://localhost:8000/", {
        doc: loadFixture("omnivoice"),
    });
    h.elementById("sf-tts-base-url").value = "http://localhost:8000/";
    h.elementById("sf-tts-cap-refresh").dispatch("click", {});
    await settle();

    // THEN the in-flight edit survives and no switch note is shown:
    assert.equal(
        rowByName(h.elementById("sf-tts-params"), "num_steps").widgetEl.value,
        "48",
        "a trailing-slash spelling of the same url is not an engine switch",
    );
    const lines = findClass(h.elementById("sf-tts-info"), "tts-info-line");
    assert.ok(
        !lines.some((l) => l.textContent.includes("Engine switched")),
        `trailing slash must not trigger the switch note: ${lines.map((l) => l.textContent).join(" | ")}`,
    );
});

test("capRefreshClick_schemelessUrl_showsSchemeError_notNotReachable", async () => {
    // A scheme-less typo (localhost:9000) must get the specific 422 message
    // — the user can fix the field right there — not the generic
    // "not reachable" line.
    const h = createSettingsHarness();
    await h.sandbox.openSettings();
    assert.ok(rows(h.elementById("sf-tts-params")).length > 0);

    // WHEN the user types a scheme-less url and clicks Refresh:
    h.elementById("sf-tts-base-url").value = "localhost:9000";
    h.elementById("sf-tts-cap-refresh").dispatch("click", {});
    await settle();

    // THEN the section is cleared and the scheme error is shown:
    assert.equal(h.get("ttsCapabilitiesDoc"), null);
    assert.equal(h.elementById("sf-tts-params").children.length, 0);
    assert.equal(
        h.elementById("sf-tts-info").children[0].textContent,
        "TTS Base URL must start with http:// or https://.",
    );
});

test("capRefreshClick_unreachableUrl_showsNotReachable", async () => {
    // A well-formed url with no live server behind it → the generic
    // "not reachable" status line (the 503 path).
    const h = createSettingsHarness();
    await h.sandbox.openSettings();

    // WHEN the user points at a dead (but well-formed) url and clicks Refresh:
    h.elementById("sf-tts-base-url").value = "http://dead.local:9000";
    h.elementById("sf-tts-cap-refresh").dispatch("click", {});
    await settle();

    // THEN the section is cleared with the not-reachable line:
    assert.equal(h.get("ttsCapabilitiesDoc"), null);
    assert.equal(h.elementById("sf-tts-params").children.length, 0);
    assert.equal(
        h.elementById("sf-tts-info").children[0].textContent,
        "TTS server not reachable — parameter options unavailable",
    );
});
