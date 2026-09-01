/**
 * test_persona_form.js — Regression tests for the persona editor form
 * (static/persona.js).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_persona_form.js
 *
 * The invariant under test: submitPersonaForm() must only send the
 * remove_avatar_image / remove_reference_audio flags after an EXPLICIT
 * "Remove" click. The original bug derived those flags from server-side
 * file presence (peAvatarOnServer / peAudioOnServer), which silently
 * deleted a persona's avatar and reference audio on every plain
 * text-field save. The same explicit-click rule covers clear_memories:
 * it is sent only after an explicit "Clear saved memories" click, and
 * memory_size is ALWAYS sent (the update endpoint requires it — an
 * omitted value must not silently reset the persona's memory budget).
 *
 * How it works: persona.js and its dependencies are browser scripts that
 * share globals (no ES modules), so each test evaluates them in a fresh
 * vm.Context against a minimal DOM stub. The real openPersonaForm(),
 * Remove-button listeners, file-input change handlers and
 * submitPersonaForm() are exercised; fetch() is stubbed to capture the
 * multipart FormData the app would have sent to the server.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which
 * must run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_persona_form.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html: utils before state before persona.
const APP_SCRIPTS = ["utils.js", "state.js", "persona.js"];

/* ==========================================================================
    DOM / browser stubs
    ========================================================================== */

/**
 * Minimal stand-in for an HTMLElement. Implements only what persona.js
 * touches: classList, event listeners (with a dispatch helper for tests),
 * appendChild/children, focus, and a value setter that clears `files` on
 * file inputs (mimicking the real DOM, where value = "" resets the
 * selection).
 */
function makeFakeElement(id) {
    const listeners = new Map(); // event type -> [listener, ...]
    const classSet = new Set();
    let innerHtml = "";
    let value = "";

    const el = {
        id,
        checked: false,
        textContent: "",
        files: [],
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
            el.children.push(child);
            return child;
        },
    };

    Object.defineProperty(el, "value", {
        get: () => value,
        set: (v) => {
            value = v;
            if (v === "") el.files.length = 0;
        },
        enumerable: true,
    });

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

/** Minimal fetch Response stand-in: the app only reads .ok and .json(). */
function jsonResponse(payload) {
    return { ok: true, status: 200, json: async () => payload };
}

/**
 * Build the sandbox for one test: fresh DOM elements, a fetch stub that
 * records every call (url, method, body) and answers with canned JSON.
 */
function createFormHarness() {
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
        const u = String(url);
        const method = (options && options.method) || "GET";
        fetchStub.calls.push({ url: u, method, body: options ? options.body : undefined });
        if (method === "GET" && u.endsWith("/detail")) {
            return jsonResponse(fetchStub.state.detail || {});
        }
        if (method === "GET" && u === "/api/personas") {
            return jsonResponse(fetchStub.state.personas);
        }
        return jsonResponse({});
    };
    fetchStub.calls = [];
    fetchStub.state = {
        detail: {},   // persona detail fixture for GET /api/personas/<name>/detail
        personas: [], // fixture for GET /api/personas (post-submit refresh)
    };

    const sandbox = {
        console,
        document: documentStub,
        FormData, // Node's undici FormData; .get()/.has() work from here
        // persona.js only ever creates/revokes object URLs for previews.
        URL: {
            createObjectURL: () => "blob:fake",
            revokeObjectURL: () => {},
        },
        fetch: fetchStub,
        requestAnimationFrame: () => 0,
        // Defined in app.js (not loaded here); submitPersonaForm awaits it.
        loadPersonas: async () => {},
        // Defined by the browser; playPersonaReferenceAudio would use it.
        Audio: class {
            constructor() {
                this.onended = null;
                this.onerror = null;
            }
            play() {
                return Promise.resolve();
            }
            pause() {}
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
        fetchStub,
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

/** Persona-detail fixture matching PersonaDetailResponse in app/models.py. */
function detailFixture({ avatarOnServer = true, audioOnServer = true, memorySize = 8192 } = {}) {
    return {
        name: "Al",
        description: "A test persona",
        system_prompt: "You are Al, a test persona.",
        router_hints: "witty test persona",
        avatar_color: "#123456",
        avatar_image: avatarOnServer,
        reference_audio: audioOnServer,
        reference_audio_transcript: "hello there",
        reference_audio_language: "en",
        allow_tool_calls: false,
        tts_capable: audioOnServer,
        memory_size: memorySize,
    };
}

/**
 * Assert the form is not showing an error. Both openPersonaForm() and
 * submitPersonaForm() swallow failures into peFormError, which would
 * otherwise mask a broken stub and let tests pass on missing requests.
 */
function assertNoFormError(h) {
    const errEl = h.elementById("pe-form-error");
    assert.ok(
        errEl.classList.contains("hidden"),
        `form error shown: ${errEl.textContent}`,
    );
    assert.equal(errEl.textContent, "");
}

/**
 * Open the editor in edit mode for "Al" through the real openPersonaForm()
 * path (fetch detail -> fill fields -> set server-presence flags).
 */
async function openEditForm(h, { avatarOnServer = true, audioOnServer = true, memorySize = 8192 } = {}) {
    h.fetchStub.state.detail = detailFixture({ avatarOnServer, audioOnServer, memorySize });
    await h.sandbox.openPersonaForm("Al");
    assertNoFormError(h);
}

/** Capture of the persona PUT/POST the last submitPersonaForm() issued. */
function mutationCall(h) {
    return h.fetchStub.calls.find(
        (c) => c.method === "PUT" && c.url.startsWith("/api/personas/"),
    ) || h.fetchStub.calls.find((c) => c.method === "POST" && c.url === "/api/personas");
}

/** A browser-like File for the file inputs (undici accepts real Files). */
function makeFile(name, type) {
    return new File([`fake bytes for ${name}`], name, { type });
}

/* ==========================================================================
    Tests
    ========================================================================== */

test("submitPersonaForm_editingPersonaWithServerFiles_noFileInteraction_sendsNoRemoveFlags", async () => {
    // GIVEN a persona that has both an avatar and reference audio on the
    // server, with the editor open in edit mode:
    const h = createFormHarness();
    await openEditForm(h);
    assert.equal(h.get("peAvatarOnServer"), true, "server-presence flag must be set from detail");
    assert.equal(h.get("peAudioOnServer"), true);

    // WHEN the user saves without selecting any file and without clicking
    // any Remove button (a plain text edit):
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);

    // THEN the request goes out, carries the text fields, and does NOT
    // mark either file for removal (the regression: both remove_* flags
    // used to be "true" here, deleting the files on disk).
    const call = mutationCall(h);
    assert.ok(call, "expected a PUT /api/personas/Al request");
    assert.equal(call.url, "/api/personas/Al");
    const form = call.body;
    assert.equal(form.get("name"), "Al");
    assert.equal(form.get("system_prompt"), "You are Al, a test persona.");
    assert.equal(form.get("avatar_image"), null);
    assert.equal(form.get("reference_audio"), null);
    assert.equal(form.has("remove_avatar_image"), false);
    assert.equal(form.has("remove_reference_audio"), false);
});

test("submitPersonaForm_avatarRemoveClicked_sendsOnlyRemoveAvatarFlag", async () => {
    // GIVEN the editor open for a persona with avatar + audio on the server:
    const h = createFormHarness();
    await openEditForm(h);

    // WHEN the user explicitly clicks the avatar Remove button:
    h.elementById("pf-avatar-remove").dispatch("click", {});
    assert.equal(h.get("peAvatarRemoveRequested"), true);

    // THEN only the avatar removal flag is sent; reference audio is kept:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);
    const form = mutationCall(h).body;
    assert.equal(form.get("remove_avatar_image"), "true");
    assert.equal(form.has("remove_reference_audio"), false);
    assert.equal(form.get("reference_audio"), null);
});

test("submitPersonaForm_audioRemoveClicked_sendsOnlyRemoveAudioFlag", async () => {
    // GIVEN the editor open for a persona with avatar + audio on the server:
    const h = createFormHarness();
    await openEditForm(h);

    // WHEN the user explicitly clicks the reference audio Remove button:
    h.elementById("pf-audio-remove").dispatch("click", {});
    assert.equal(h.get("peAudioRemoveRequested"), true);

    // THEN only the audio removal flag is sent; the avatar is kept:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);
    const form = mutationCall(h).body;
    assert.equal(form.get("remove_reference_audio"), "true");
    assert.equal(form.has("remove_avatar_image"), false);
    assert.equal(form.get("avatar_image"), null);
});

test("submitPersonaForm_newAvatarFileSelected_sendsFileWithoutRemoveFlag", async () => {
    // GIVEN the editor open for a persona with an avatar on the server:
    const h = createFormHarness();
    await openEditForm(h);

    // WHEN the user selects a replacement avatar file:
    h.elementById("pf-avatar-image").files = [makeFile("new.png", "image/png")];
    h.elementById("pf-avatar-image").dispatch("change", {});

    // THEN the new file is uploaded and no removal flag is sent:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);
    const form = mutationCall(h).body;
    const uploaded = form.get("avatar_image");
    assert.ok(uploaded, "expected an avatar_image file entry");
    assert.equal(uploaded.name, "new.png");
    assert.equal(form.has("remove_avatar_image"), false);
    assert.equal(form.has("remove_reference_audio"), false);
});

test("submitPersonaForm_audioRemoveClickedThenNewFileSelected_sendsFileWithoutRemoveFlag", async () => {
    // GIVEN the editor open for a persona with audio on the server, and the
    // user has clicked Remove (pending removal):
    const h = createFormHarness();
    await openEditForm(h);
    h.elementById("pf-audio-remove").dispatch("click", {});
    assert.equal(h.get("peAudioRemoveRequested"), true);

    // WHEN the user changes their mind and selects a new audio file (the
    // selection must cancel the pending removal):
    h.elementById("pf-reference-audio").files = [makeFile("fresh.wav", "audio/wav")];
    h.elementById("pf-reference-audio").dispatch("change", {});
    assert.equal(h.get("peAudioRemoveRequested"), false);

    // THEN the new file is uploaded and no removal flag is sent:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);
    const form = mutationCall(h).body;
    const uploaded = form.get("reference_audio");
    assert.ok(uploaded, "expected a reference_audio file entry");
    assert.equal(uploaded.name, "fresh.wav");
    assert.equal(form.has("remove_reference_audio"), false);
    assert.equal(form.has("remove_avatar_image"), false);
});

test("submitPersonaForm_newPersona_noServerFiles_sendsNoRemoveFlags", async () => {
    // GIVEN the editor open for a brand-new persona (nothing on the server):
    const h = createFormHarness();
    await h.sandbox.openPersonaForm(null);
    assert.equal(h.get("peAvatarOnServer"), false);
    assert.equal(h.get("peAudioOnServer"), false);
    assert.equal(h.get("peAvatarRemoveRequested"), false);
    assert.equal(h.get("peAudioRemoveRequested"), false);
    // Fill the required fields the way the user would.
    h.elementById("pf-name").value = "New Guy";
    h.elementById("pf-system-prompt").value = "You are New Guy.";
    h.elementById("pf-router-hints").value = "fresh test persona";
    h.elementById("pf-reference-audio-language").value = "en";

    // WHEN the user saves without selecting files:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);

    // THEN a POST goes out with no files and no removal flags:
    const call = mutationCall(h);
    assert.ok(call, "expected a POST /api/personas request");
    assert.equal(call.url, "/api/personas");
    const form = call.body;
    assert.equal(form.get("name"), "New Guy");
    assert.equal(form.has("remove_avatar_image"), false);
    assert.equal(form.has("remove_reference_audio"), false);
});

test("openPersonaForm_reopenedAfterPendingRemoval_resetsRemoveRequestAndPreviews", async () => {
    // GIVEN the editor open for a persona with an avatar on the server, and
    // the user has clicked Remove (pending removal, nothing saved yet):
    const h = createFormHarness();
    await openEditForm(h);
    h.elementById("pf-avatar-remove").dispatch("click", {});
    assert.equal(h.get("peAvatarRemoveRequested"), true);

    // WHEN the user cancels and reopens the form (the file is still on the
    // server — nothing was saved):
    await h.sandbox.openPersonaForm("Al");

    // THEN the pending removal is discarded and the preview shows the
    // server's avatar again:
    assert.equal(h.get("peAvatarRemoveRequested"), false);
    const preview = h.elementById("pf-avatar-preview");
    const img = preview.children.find((c) => c.tagName === "img");
    assert.ok(img, "expected the server avatar to be previewed after reopen");
    assert.equal(img.src, "/api/personas/Al/avatar");
});

test("renderPersonaAvatarPreview_removeClicked_showsPostSaveState", async () => {
    // GIVEN the editor open for a persona with avatar + audio on the server
    // (preview shows the server files, controls visible):
    const h = createFormHarness();
    await openEditForm(h);
    let preview = h.elementById("pf-avatar-preview");
    let img = preview.children.find((c) => c.tagName === "img");
    assert.ok(img, "expected the server avatar preview before Remove");
    assert.ok(!h.elementById("pf-avatar-remove").classList.contains("hidden"));
    assert.equal(h.elementById("pf-audio-status").textContent, "Current file on server");

    // WHEN the user clicks Remove on both files:
    h.elementById("pf-avatar-remove").dispatch("click", {});
    h.elementById("pf-audio-remove").dispatch("click", {});

    // THEN the UI shows the post-save state (no files) instead of lying
    // that the server files are still in effect:
    preview = h.elementById("pf-avatar-preview");
    assert.equal(preview.children.filter((c) => c.tagName === "img").length, 0);
    assert.equal(preview.textContent, "A", "expected the initial fallback in the preview");
    assert.ok(h.elementById("pf-avatar-remove").classList.contains("hidden"));
    assert.equal(h.elementById("pf-audio-status").textContent, "None");
    assert.ok(h.elementById("pf-audio-remove").classList.contains("hidden"));
    assert.ok(h.elementById("pf-audio-play").classList.contains("hidden"));
});

/* ==========================================================================
    Memory-size / clear-memories field
    ========================================================================== */

test("openPersonaForm_newPersona_memorySizeDefaultsTo8192_clearButtonHidden", async () => {
    // GIVEN the editor opened for a brand-new persona:
    const h = createFormHarness();
    await h.sandbox.openPersonaForm(null);

    // THEN the budget field starts at the server-side default and the
    // "Clear saved memories" button is hidden (nothing to clear yet):
    assert.equal(h.elementById("pf-memory-size").value, 8192);
    assert.ok(h.elementById("pf-memories-clear").classList.contains("hidden"));
});

test("openPersonaForm_editingPersona_prefillsMemorySizeFromDetail_clearButtonShown", async () => {
    // GIVEN a persona whose server-side budget is 4096, editor in edit mode:
    const h = createFormHarness();
    await openEditForm(h, { memorySize: 4096 });

    // THEN the field is prefilled with the existing budget and the clear
    // button is available:
    assert.equal(h.elementById("pf-memory-size").value, 4096);
    assert.ok(!h.elementById("pf-memories-clear").classList.contains("hidden"));
});

test("openPersonaForm_editingLegacyPersonaWithoutMemorySizeKey_defaultsTo8192", async () => {
    // GIVEN a legacy persona whose detail predates the field (no memory_size
    // key at all — a JSON key absent, not null):
    const h = createFormHarness();
    const detail = detailFixture();
    delete detail.memory_size;
    h.fetchStub.state.detail = detail;
    await h.sandbox.openPersonaForm("Al");
    assertNoFormError(h);

    // THEN the frontend falls back to the default budget:
    assert.equal(h.elementById("pf-memory-size").value, 8192);
});

test("submitPersonaForm_editingPersona_alwaysSendsMemorySize_noClearClick_noClearFlag", async () => {
    // GIVEN the editor open for a persona with budget 4096, untouched field:
    const h = createFormHarness();
    await openEditForm(h, { memorySize: 4096 });

    // WHEN the user saves with a plain text edit (no Clear click):
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);

    // THEN memory_size is ALWAYS sent (the update endpoint requires it),
    // and clear_memories is NOT (no explicit click):
    const form = mutationCall(h).body;
    assert.equal(form.get("memory_size"), "4096");
    assert.equal(form.has("clear_memories"), false);
});

test("submitPersonaForm_memorySizeFieldEdited_sendsEditedValue", async () => {
    // GIVEN the editor open with the default budget prefilled:
    const h = createFormHarness();
    await openEditForm(h, { memorySize: 8192 });

    // WHEN the user changes the budget field:
    h.elementById("pf-memory-size").value = "2048";
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);

    // THEN the edited value goes out:
    const form = mutationCall(h).body;
    assert.equal(form.get("memory_size"), "2048");
});

test("submitPersonaForm_newPersona_sendsDefaultMemorySize8192", async () => {
    // GIVEN the editor open for a brand-new persona, required fields filled:
    const h = createFormHarness();
    await h.sandbox.openPersonaForm(null);
    h.elementById("pf-name").value = "New Guy";
    h.elementById("pf-system-prompt").value = "You are New Guy.";
    h.elementById("pf-router-hints").value = "fresh test persona";
    h.elementById("pf-reference-audio-language").value = "en";

    // WHEN the user saves:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);

    // THEN the POST carries the default budget and no clear flag:
    const form = mutationCall(h).body;
    assert.equal(form.get("memory_size"), "8192");
    assert.equal(form.has("clear_memories"), false);
});

test("submitPersonaForm_clearMemoriesClicked_sendsClearFlagWithMemorySize", async () => {
    // GIVEN the editor open in edit mode:
    const h = createFormHarness();
    await openEditForm(h);

    // WHEN the user explicitly clicks "Clear saved memories":
    h.elementById("pf-memories-clear").dispatch("click", {});
    assert.equal(h.get("peMemoriesClearRequested"), true);

    // THEN the request carries clear_memories=true alongside memory_size:
    await h.sandbox.submitPersonaForm({ preventDefault() {} });
    assertNoFormError(h);
    const form = mutationCall(h).body;
    assert.equal(form.get("clear_memories"), "true");
    assert.equal(form.get("memory_size"), "8192");
});

test("openPersonaForm_reopenedAfterClearClick_resetsClearRequest", async () => {
    // GIVEN a pending (unsaved) Clear click:
    const h = createFormHarness();
    await openEditForm(h);
    h.elementById("pf-memories-clear").dispatch("click", {});
    assert.equal(h.get("peMemoriesClearRequested"), true);

    // WHEN the user cancels and reopens the form:
    await h.sandbox.openPersonaForm("Al");

    // THEN the pending clear is discarded:
    assert.equal(h.get("peMemoriesClearRequested"), false);
});

test("submitPersonaForm_invalidMemorySize_showsErrorAndSendsNoRequest", async () => {
    // The budget must be a whole number in [0, 16384]. Each invalid value
    // below must be rejected client-side BEFORE any request is issued —
    // a bad budget saved server-side would corrupt or wipe the memories.
    for (const bad of ["16385", "-1", "abc", ""]) {
        const h = createFormHarness();
        // GIVEN the editor open in edit mode:
        await openEditForm(h);

        // WHEN the user enters an invalid budget and saves:
        h.elementById("pf-memory-size").value = bad;
        await h.sandbox.submitPersonaForm({ preventDefault() {} });

        // THEN a form error is shown and no PUT/POST went out:
        const errEl = h.elementById("pe-form-error");
        assert.ok(!errEl.classList.contains("hidden"), `no error shown for ${JSON.stringify(bad)}`);
        assert.ok(errEl.textContent.includes("Memory size"), `unexpected error for ${JSON.stringify(bad)}: ${errEl.textContent}`);
        assert.equal(mutationCall(h), undefined, `request sent for invalid budget ${JSON.stringify(bad)}`);
    }
});
