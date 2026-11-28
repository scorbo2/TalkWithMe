/**
 * test_copy_button.js — Regression tests for the per-message copy button
 * (issue #158; static/chat.js + static/utils.js + static/style.css).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_copy_button.js
 *
 * Invariants under test:
 *
 * 1. Copy and delete sit in ONE row: both buttons are children of a single
 *    .message-actions wrapper, which is the last child of the row's content
 *    container (.user-message-content for user rows, .bubble-content for
 *    persona rows). The wrapper is load-bearing — both containers are column
 *    flex boxes, so two bare buttons stack vertically, which is the one thing
 *    the issue explicitly asked for ("the same row as the delete button").
 * 2. Every render path gets the buttons: live user bubble, live (streaming)
 *    assistant row, and both persisted-history renderers.
 * 3. The copy button is live immediately, even on a streaming assistant row
 *    where the delete button is still disabled.
 * 4. Clicking copy puts exactly the bubble's text on the clipboard — the
 *    persona name, the tool chips and the audio replay buttons all live
 *    outside .bubble and must not leak into the copy.
 * 5. A copy that cannot happen is visible: the button shows ✅/❌ and then
 *    reverts. A clipboard that is unavailable (plain HTTP on the LAN) must
 *    not be silently indistinguishable from success.
 * 6. An empty bubble is never copied: an assistant row's loading placeholder
 *    IS a .bubble (class "bubble loading-bubble"), so "there is a bubble"
 *    does not mean "there is text" — copying that would wipe whatever the
 *    user had on their clipboard for no reason.
 * 7. Insecure origins still work: with navigator.clipboard undefined the copy
 *    goes through the execCommand fallback — the same LAN gotcha that made
 *    crypto.randomUUID() unusable (#145, see test_message_id.js).
 * 8. The audio replay pipeline survives the wrapper: addAudioButtonTo*
 *    injects .message-audio as a direct child of the content container,
 *    ABOVE the action row. Anchoring insertBefore() on the delete button
 *    itself (now nested inside .message-actions) throws NotFoundError, and
 *    both callers swallow it into a "failed to persist audio" warning —
 *    which is how a copy button once killed every replay button on live
 *    rows. This suite exists partly because that regression is invisible to
 *    the other harnesses: their DOM stubs' insertBefore() silently accepts a
 *    reference node that is not a child. Ours throws, like real browsers do.
 *
 * How it works: the browser scripts share globals (no ES modules), so each
 * test evaluates state.js + utils.js + chat.js in a fresh vm.Context against
 * a minimal DOM stub with a small real selector engine (class + attribute
 * selectors, descendant scan) and strict parent/child bookkeeping.
 *
 * NOTE: this file is intentionally NOT part of the pytest suite (which must
 * run with nothing but Python installed). Run it alongside:
 *     python3 -m pytest
 *     node tests/test_copy_button.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "static");
// Load order mirrors templates/index.html: state declares the globals,
// utils provides the clipboard helper, chat provides the buttons.
const APP_SCRIPTS = ["state.js", "utils.js", "chat.js"];

const COPY_ICON = "\u{1F4CB}";
const COPIED_ICON = "\u{2705}";
const FAILED_ICON = "\u{274C}";
const COPY_FEEDBACK_MS = 1500;

/* ==========================================================================
    DOM stub — deliberately stricter than the other harnesses
    ========================================================================== */

/** A DOMException-shaped Error, matching what browsers throw for insertBefore. */
function domError(name, message) {
    const err = new Error(message);
    err.name = name;
    return err;
}

/**
 * Minimal HTMLElement stand-in: classList/className in sync, dataset,
 * parent-tracked children, textContent that aggregates descendants, and a
 * querySelector that understands ".class", ".class.class" and
 * "[data-foo=\"bar\"]" via a descendant scan.
 */
function makeElement(tag) {
    const classes = new Set();
    const attrs = new Map();
    const handlers = new Map();

    const el = {
        tagName: tag,
        children: [],
        parent: null,
        dataset: {},
        style: {},
        title: "",
        disabled: false,
        scrollTop: 0,
        scrollHeight: 0,
        _ownText: "",

        addEventListener(type, fn) { handlers.set(type, fn); },
        /** Test helper: fire a listener the way a user interaction would. */
        click() {
            const fn = handlers.get("click");
            if (!fn) throw new Error(`no click handler on <${tag}>`);
            return fn();
        },
        setAttribute(name, value) { attrs.set(name, String(value)); },
        getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
        setAttributeNS() {},
        appendChild(child) {
            child.parent = el;
            el.children.push(child);
            return child;
        },
        insertBefore(child, ref) {
            // Two spec cases, spelled out because the other chat.js harnesses
            // blur them (they append for *any* unfound reference, which
            // happens to cover the null case but silently tolerates the
            // second one): a null reference means append — insertBefore(node,
            // null) is appendChild — and a non-null reference must be a CHILD
            // of this node, or the browser throws NotFoundError.
            if (ref === null) {
                child.parent = el;
                el.children.push(child);
                return child;
            }
            if (ref.parent !== el) {
                throw domError(
                    "NotFoundError",
                    "Failed to execute 'insertBefore' on 'Node': The node before " +
                    "which the new node is to be inserted is not a child of this node.",
                );
            }
            el.children.splice(el.children.indexOf(ref), 0, child);
            child.parent = el;
            return child;
        },
        replaceWith(next) {
            if (!el.parent) return next;
            next.parent = el.parent;
            el.parent.children[el.parent.children.indexOf(el)] = next;
            el.parent = null;
            return next;
        },
        remove() {
            if (!el.parent) return;
            el.parent.children.splice(el.parent.children.indexOf(el), 1);
            el.parent = null;
        },
        querySelector(sel) { return descendants(el, sel)[0] || null; },
        querySelectorAll(sel) { return descendants(el, sel); },
        select() { el.selected = true; },
        focus() {},
    };

    Object.defineProperty(el, "className", {
        get: () => [...classes].join(" "),
        set: (value) => {
            classes.clear();
            for (const c of String(value).split(/\s+/)) if (c) classes.add(c);
        },
    });
    el.classList = {
        add: (c) => classes.add(c),
        remove: (c) => classes.delete(c),
        contains: (c) => classes.has(c),
        toggle: (c, force) => {
            const want = force === undefined ? !classes.has(c) : !!force;
            if (want) classes.add(c); else classes.delete(c);
            return want;
        },
    };
    Object.defineProperty(el, "textContent", {
        get: () => (el.children.length ? el.children.map((c) => c.textContent).join("") : el._ownText),
        set: (value) => { el.children = []; el._ownText = String(value); },
    });
    // innerHTML is only ever used to blank an element or drop in a static
    // snippet, so the stub models it as "markup in, text out" (tags dropped,
    // text nodes kept). That matters: chat.js's loading placeholder is
    // innerHTML = "<span></span>...", whose textContent in a real browser is
    // "" — a stub that kept the markup as text would let an empty bubble look
    // non-empty and hide the "don't copy an empty string" rule.
    Object.defineProperty(el, "innerHTML", {
        get: () => el._ownText,
        set: (value) => {
            el.children = [];
            el._ownText = String(value).replace(/<[^>]*>/g, "");
        },
    });

    return el;
}

/** ".message-row.user[data-message-id=\"x\"]" -> matcher. */
function parseSelector(selector) {
    const tokens = selector.match(/\.[\w-]+|\[[^\]]+\]/g);
    if (!tokens || tokens.join("") !== selector) {
        throw new Error(`the DOM stub has no selector engine for: ${selector}`);
    }
    const classes = [];
    const attrs = [];
    for (const token of tokens) {
        if (token.startsWith(".")) {
            classes.push(token.slice(1));
        } else {
            const m = token.slice(1, -1).match(/^([^=]+)(?:="([^"]*)")?$/);
            const name = m[1];
            const dataKey = name.startsWith("data-")
                ? name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())
                : null;
            attrs.push({ name, dataKey, value: m[2] });
        }
    }
    return { classes, attrs };
}

function matches(el, parsed) {
    for (const cls of parsed.classes) if (!el.classList.contains(cls)) return false;
    for (const attr of parsed.attrs) {
        const actual = (attr.dataKey !== undefined && el.dataset[attr.dataKey] !== undefined)
            ? el.dataset[attr.dataKey]
            : el.getAttribute(attr.name);
        if (attr.value === undefined) {
            if (actual === null || actual === undefined) return false;
        } else if (actual !== attr.value) {
            return false;
        }
    }
    return true;
}

function descendants(root, selector) {
    const parsed = parseSelector(selector);
    const found = [];
    (function walk(node) {
        for (const child of node.children) {
            if (matches(child, parsed)) found.push(child);
            walk(child);
        }
    })(root);
    return found;
}

/** Test helper: the direct children of `el` as class-name strings. */
function childClasses(el) {
    return el.children.map((c) => c.className || c.tagName);
}

/* ==========================================================================
    Harness
    ========================================================================== */

/**
 * Build one test's sandbox: fresh DOM, a clipboard whose behaviour the test
 * chooses, and stubs for everything the loaded scripts touch at runtime
 * (fetch, requestAnimationFrame, atob).
 *
 * @param {object} [options]
 * @param {object|null} [options.clipboard] - navigator.clipboard contents;
 *        null/undefined models an insecure context (no Clipboard API).
 * @param {Function|null} [options.execCommand] - document.execCommand stub;
 *        null means the browser has no legacy copy path either.
 */
function createHarness(options = {}) {
    const byId = new Map();
    const body = makeElement("body");
    const copyAttempts = [];
    const created = [];

    const documentStub = {
        body,
        getElementById(id) {
            if (!byId.has(id)) byId.set(id, makeElement(`div#${id}`));
            return byId.get(id);
        },
        createElement(tag) {
            const el = makeElement(tag);
            created.push(el);
            return el;
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        execCommand: options.execCommand === undefined
            ? (cmd) => { copyAttempts.push(cmd); return true; }
            : options.execCommand,
    };

    const fetchCalls = [];
    const fetchStub = async (url, opts = {}) => {
        fetchCalls.push({ url, opts });
        return { ok: true, status: 200, json: async () => ({}) };
    };

    const sandbox = vm.createContext({
        document: documentStub,
        navigator: { clipboard: options.clipboard },
        console,
        setTimeout, clearTimeout,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        fetch: fetchStub,
        atob: (s) => Buffer.from(s, "base64").toString("binary"),
        window: {},
    });

    for (const name of APP_SCRIPTS) {
        vm.runInContext(fs.readFileSync(path.join(STATIC_DIR, name), "utf8"), sandbox, { filename: name });
    }

    return {
        sandbox,
        document: documentStub,
        body,
        created,
        messagesEl: vm.runInContext("messagesEl", sandbox),
        fetchCalls,
        copyAttempts,
        exec: (code) => vm.runInContext(code, sandbox),
    };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Seed the shared `personas` global (a top-level `let` in state.js, so it
 *  must be assigned *inside* the context, not as a sandbox property). */
function seedPersonas(h, list) {
    h.exec(`personas = ${JSON.stringify(list)}; currentChatRoom = "test room";`);
}

/** A live user row, as sendMessage() renders it. */
function addUserRow(h, text, id) {
    h.exec(`appendUserBubble(${JSON.stringify(text)}, ${JSON.stringify(id)});`);
    return h.messagesEl.children[h.messagesEl.children.length - 1];
}

/** A streaming assistant row: created, set up, and stamped with an ID. */
function addAssistantRow(h, id, personaName = "Alex") {
    seedPersonas(h, [{ name: personaName, avatar_color: "#ff0000", avatar_image: false }]);
    h.exec(`
        const row = createAssistantBubble(${JSON.stringify(personaName)});
        messagesEl.appendChild(row);
        setupAssistantBubble(row, { name: ${JSON.stringify(personaName)}, avatar_color: "#ff0000" });
        row.dataset.messageId = ${JSON.stringify(id)};
        row.querySelector(".bubble").textContent = "the reply text";
    `);
    return h.messagesEl.children[h.messagesEl.children.length - 1];
}

/** The content container of a row (.user-message-content or .bubble-content). */
function contentOf(row) {
    return row.querySelector(".user-message-content") || row.querySelector(".bubble-content");
}

/** The row's copy button. */
function copyBtnOf(row) { return row.querySelector(".message-copy-btn"); }
function deleteBtnOf(row) { return row.querySelector(".message-delete-btn"); }

/** A clipboard that records what was written and succeeds. */
function recordingClipboard(calls, impl) {
    return {
        writeText: (text) => {
            calls.push(text);
            return impl ? impl(text) : Promise.resolve();
        },
    };
}

/* ==========================================================================
    0. Harness sanity check
    ========================================================================== */

test("the DOM stub is spec-faithful: insertBefore(node, null) appends, a non-child reference throws", () => {
    // The stub exists to catch parent/child mistakes in app code, so it has
    // to get the spec cases right itself — a stub that mis-orders on the
    // null-ref form would hide a real bug or invent a fake one.
    // GIVEN a parent with two children, plus a stranger that is not one:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const parent = h.document.createElement("div");
    const first = h.document.createElement("span");
    const second = h.document.createElement("span");
    const stranger = h.document.createElement("section");
    const newcomer = h.document.createElement("i");
    parent.appendChild(first);
    parent.appendChild(second);

    // WHEN inserting with a null reference,
    // THEN it appends: insertBefore(node, null) === appendChild.
    parent.insertBefore(newcomer, null);
    assert.deepEqual(parent.children, [first, second, newcomer]);

    // WHEN inserting before a node that is not a child of the parent,
    // THEN it throws NotFoundError, exactly like a browser does.
    assert.throws(
        () => parent.insertBefore(h.document.createElement("em"), stranger),
        { name: "NotFoundError" },
    );
    assert.deepEqual(parent.children, [first, second, newcomer]);
});

/* ==========================================================================
    1. The action row layout
    ========================================================================== */

test("addMessageActionButtons puts copy and delete in one .message-actions row, last in the content container", () => {
    // GIVEN a live user row:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addUserRow(h, "hello", "u-1");
    const content = contentOf(row);

    // WHEN the action buttons are rendered,
    const actions = content.querySelector(".message-actions");

    // THEN both buttons are siblings inside one wrapper (the content
    // containers are column flex boxes: without the wrapper the issue's
    // "same row as the delete button" requirement is not met),
    assert.ok(actions, "expected a .message-actions wrapper");
    assert.deepEqual(childClasses(actions).slice(0, 2), ["message-copy-btn", "message-delete-btn"]);
    assert.equal(copyBtnOf(row).parent, actions);
    assert.equal(deleteBtnOf(row).parent, actions);

    // AND the wrapper is the last child, so audio stays above the buttons:
    assert.deepEqual(childClasses(content), ["bubble", "message-actions"]);
    assert.equal(actions.parent, content);
});

test("every render path gets exactly one copy button next to its delete button", () => {
    // GIVEN each of the four row renderers:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    seedPersonas(h, [{ name: "Alex", avatar_color: "#ff0000", avatar_image: false }]);
    h.exec(`
        appendUserBubble("live user", "u-1");
        const live = createAssistantBubble("Alex");
        messagesEl.appendChild(live);
        appendPersistedUserBubble({ id: "u-2", text: "persisted user", audio: [] }, "test room");
        appendPersistedAssistantBubble({ id: "a-2", sender: "Alex", text: "persisted reply", audio: [] }, "test room");
    `);

    // THEN no renderer was forgotten, and none rendered a stray second copy.
    assert.equal(h.messagesEl.children.length, 4);
    for (const row of h.messagesEl.children) {
        assert.equal(row.querySelectorAll(".message-actions").length, 1, `one action row in .${row.className}`);
        assert.equal(row.querySelectorAll(".message-copy-btn").length, 1, `one copy button in .${row.className}`);
        assert.equal(row.querySelectorAll(".message-delete-btn").length, 1, `one delete button in .${row.className}`);
    }
});

test("a streaming assistant row has an enabled copy button and a disabled delete button", () => {
    // GIVEN an assistant row created before the reply is persisted:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addAssistantRow(h, "a-1");

    // WHEN the row is created,
    // THEN delete waits for "done" (nothing on disk yet) but copy is usable:
    // half a reply is still worth copying.
    assert.equal(deleteBtnOf(row).disabled, true);
    assert.equal(copyBtnOf(row).disabled, false);
});

test("renaming the button factory did not drop the delete button's handler", async () => {
    // GIVEN a persisted user row:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addUserRow(h, "doomed", "u-del");

    // WHEN its delete button is clicked,
    await deleteBtnOf(row).click();
    await tick();

    // THEN the same DELETE request as before, and the row is gone.
    assert.deepEqual(h.fetchCalls.map((c) => [c.url, c.opts.method]),
        [["/api/persist/message/default/u-del", "DELETE"]]);
    assert.equal(h.messagesEl.children.includes(row), false);
});

/* ==========================================================================
    2. What ends up on the clipboard
    ========================================================================== */

test("clicking copy writes exactly the bubble text to the clipboard", async () => {
    // GIVEN a persona reply whose row also carries a name, a tool chip and
    // an audio replay button (all outside .bubble):
    const written = [];
    const h = createHarness({ clipboard: recordingClipboard(written) });
    const row = addAssistantRow(h, "a-clip");
    // Hand-build a chip row and an audio container around the bubble: none
    // of them may leak into the copy.
    h.sandbox._row = row;
    h.exec(`
        const contentEl = _row.querySelector(".bubble-content");
        const chipRow = document.createElement("div");
        chipRow.className = "tool-chips";
        const chip = document.createElement("span");
        chip.className = "tool-chip";
        chip.textContent = "🔧 get_time";
        contentEl.insertBefore(chipRow, contentEl.querySelector(".bubble"));

        const audio = document.createElement("div");
        audio.className = "message-audio";
        const playBtn = document.createElement("button");
        playBtn.className = "audio-play-btn";
        playBtn.textContent = "🔁";
        audio.appendChild(playBtn);
        contentEl.insertBefore(audio, contentEl.querySelector(".message-actions"));
    `);

    // WHEN the copy button is clicked,
    await copyBtnOf(row).click();

    // THEN only the bubble text was written — no persona name, no chip,
    // no replay glyph:
    assert.deepEqual(written, ["the reply text"]);
});

test("clicking copy on a row whose stream errored copies the visible text including the error trailer", async () => {
    // GIVEN the SSE error path appended its marker to the bubble (chat.js
    // appends "[Error: ...]" to bubble.textContent, so it is on screen):
    const written = [];
    const h = createHarness({ clipboard: recordingClipboard(written) });
    const row = addAssistantRow(h, "a-err");
    h.sandbox._row = row;
    h.exec(`
        const bubble = _row.querySelector(".bubble");
        bubble.textContent += "\\n\\n[Error: boom]";
    `);

    // WHEN the copy button is clicked,
    await copyBtnOf(row).click();

    // THEN what the user sees is what lands on the clipboard (deliberate:
    // hiding the trailer would make the copy disagree with the screen).
    assert.deepEqual(written, ["the reply text\n\n[Error: boom]"]);
});

test("clicking copy on a loading placeholder must not write an empty string, and must say why", async () => {
    // GIVEN an assistant row whose first token has not landed: its bubble is
    // the loading placeholder (class "bubble loading-bubble"), so a .bubble
    // exists but its text is empty.
    const written = [];
    const h = createHarness({ clipboard: recordingClipboard(written) });
    h.exec(`
        const row = createAssistantBubble("Alex");
        messagesEl.appendChild(row);
    `);
    const row = h.messagesEl.children[h.messagesEl.children.length - 1];

    // WHEN the copy button is clicked,
    await copyBtnOf(row).click();

    // THEN the clipboard was never touched (writing "" would wipe whatever
    // the user had copied) and the button explains the non-action.
    assert.deepEqual(written, []);
    assert.equal(copyBtnOf(row).textContent, FAILED_ICON);
    assert.match(copyBtnOf(row).getAttribute("aria-label"), /nothing to copy/i);
});

test("clicking copy on a row with no bubble at all gives feedback instead of doing nothing", async () => {
    // GIVEN a row whose bubble is gone (deleted / room switched away while
    // the click was in flight):
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addAssistantRow(h, "a-gone");
    h.sandbox._row = row;
    h.exec(`_row.querySelector(".bubble").remove();`);

    // WHEN the copy button is clicked,
    await copyBtnOf(row).click();

    // THEN the button says so instead of silently shrugging.
    assert.equal(copyBtnOf(row).textContent, FAILED_ICON);
});

/* ==========================================================================
    3. Feedback: the Clipboard API confirms nothing on screen
    ========================================================================== */

test("a successful copy shows ✅ and reverts to the clipboard icon afterwards", async () => {
    // GIVEN a working Clipboard API:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addUserRow(h, "copy me", "u-ok");
    const btn = copyBtnOf(row);
    assert.equal(btn.textContent, COPY_ICON);

    // WHEN the copy succeeds,
    await btn.click();

    // THEN the button confirms visibly — glyph, tooltip and accessible name.
    assert.equal(btn.textContent, COPIED_ICON);
    assert.equal(btn.title, "Copied!");
    assert.equal(btn.getAttribute("aria-label"), "Copied!");

    // AND it reverts on its own.
    await sleep(COPY_FEEDBACK_MS + 200);
    assert.equal(btn.textContent, COPY_ICON);
    assert.equal(btn.getAttribute("aria-label"), "Copy text");
});

test("a second click within the feedback window is not reverted early by the first click's timer", async () => {
    // GIVEN a working Clipboard API:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addUserRow(h, "copy me twice", "u-twice");
    const btn = copyBtnOf(row);

    // WHEN the button is clicked twice, the second click *before* the first
    // click's revert timer would have fired,
    await btn.click();
    await sleep(50);
    await btn.click();
    await sleep(COPY_FEEDBACK_MS - 200);

    // THEN the confirmation is still showing: without clearTimeout the older
    // timer would have restored 📋 while the newer copy was still confirmed.
    assert.equal(btn.textContent, COPIED_ICON);

    // AND it does eventually revert (once).
    await sleep(400);
    assert.equal(btn.textContent, COPY_ICON);
});

test("a refused clipboard write shows ❌ instead of pretending nothing happened", async () => {
    // GIVEN a Clipboard API that rejects (permission denied) and a legacy
    // path that also fails:
    const h = createHarness({
        clipboard: recordingClipboard([], () => Promise.reject(new Error("denied"))),
        execCommand: () => false,
    });
    const row = addUserRow(h, "uncooperative", "u-fail");

    // WHEN the copy is attempted,
    await copyBtnOf(row).click();

    // THEN the failure is visible.
    assert.equal(copyBtnOf(row).textContent, FAILED_ICON);
    assert.match(copyBtnOf(row).getAttribute("aria-label"), /failed/i);
});

/* ==========================================================================
    4. Insecure origins (plain HTTP over the LAN) still copy
    ========================================================================== */

test("with no navigator.clipboard (plain HTTP on the LAN) the copy uses the execCommand fallback", async () => {
    // GIVEN the insecure-context shape: navigator exists, clipboard does not:
    const h = createHarness({ clipboard: undefined });
    const row = addUserRow(h, "over http", "u-lan");

    // WHEN the copy button is clicked,
    await copyBtnOf(row).click();

    // THEN the legacy path ran with the right text in a scratch field...
    assert.deepEqual(h.copyAttempts, ["copy"]);
    const scratch = h.created.filter((el) => el.tagName === "textarea").pop();
    assert.ok(scratch, "expected the fallback to create a scratch textarea");
    assert.equal(scratch.value, "over http");
    assert.equal(scratch.selected, true, "the scratch field must be selected before copying");

    // ...and it was cleaned up again, and the user sees success.
    assert.equal(h.body.children.includes(scratch), false, "the scratch textarea must not be left in the DOM");
    assert.equal(copyBtnOf(row).textContent, COPIED_ICON);
});

test("a browser with neither a Clipboard API nor execCommand reports failure instead of throwing", async () => {
    // GIVEN no clipboard path at all:
    const h = createHarness({ clipboard: undefined, execCommand: null });
    const row = addUserRow(h, "hopeless", "u-none");

    // WHEN the copy button is clicked,
    await copyBtnOf(row).click();

    // THEN no exception escaped the handler and the button says so.
    assert.equal(copyBtnOf(row).textContent, FAILED_ICON);
});

test("no frontend file touches navigator.clipboard outside copyTextToClipboard()", () => {
    // The whole point of the fallback is that callers cannot bypass it — the
    // same shape as the crypto.randomUUID() guard in test_message_id.js.
    const offenders = [];
    for (const name of fs.readdirSync(STATIC_DIR).filter((f) => f.endsWith(".js"))) {
        const lines = fs.readFileSync(path.join(STATIC_DIR, name), "utf8").split("\n");
        lines.forEach((line, i) => {
            if (/^\s*(\*|\/\*)/.test(line)) return;      // JSDoc / block comments
            const code = line.replace(/\/\/.*$/, "");
            if (!/navigator\.clipboard/.test(code)) return;
            if (name === "utils.js" && /^const clipboard = .*navigator\.clipboard/.test(code.trim())) return;
            offenders.push(`static/${name}:${i + 1}: ${line.trim()}`);
        });
    }
    assert.deepEqual(offenders, [], "route clipboard use through copyTextToClipboard():\n" + offenders.join("\n"));
});

/* ==========================================================================
    5. The audio replay pipeline still works with the wrapper
    ========================================================================== */

test("addAudioButtonToAssistantMessage inserts .message-audio above the action row, as a direct child", () => {
    // GIVEN a live assistant row (so .message-actions already exists, with
    // the buttons nested inside it):
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addAssistantRow(h, "a-audio");
    const content = contentOf(row);

    // WHEN the first TTS replay button is injected,
    h.exec(`addAudioButtonToAssistantMessage("a-audio", "a-audio_1.wav");`);

    // THEN the audio container is a direct child of .bubble-content —
    // anchoring insertBefore on the (nested) delete button throws
    // NotFoundError, and tts.js swallows it as "failed to persist audio",
    // leaving the row with no replay button at all.
    const audio = content.querySelector(".message-audio");
    assert.ok(audio, ".message-audio was not created");
    assert.equal(audio.parent, content);
    assert.deepEqual(childClasses(content), ["bubble-name", "bubble", "message-audio", "message-actions"]);
    assert.equal(audio.children.length, 1);
    assert.equal(audio.children[0].className, "audio-play-btn");
});

test("addAudioButtonToUserMessage inserts .message-audio above the action row, as a direct child", () => {
    // GIVEN a live user row:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addUserRow(h, "spoken into the mic", "u-audio");
    const wrapper = contentOf(row);

    // WHEN the STT replay button is injected,
    h.exec(`addAudioButtonToUserMessage("u-audio", "u-audio_1.wav");`);

    // THEN it landed inside the wrapper, before the buttons:
    const audio = wrapper.querySelector(".message-audio");
    assert.ok(audio, ".message-audio was not created");
    assert.equal(audio.parent, wrapper);
    assert.deepEqual(childClasses(wrapper), ["bubble", "message-audio", "message-actions"]);
});

test("sentences of one streaming reply share a single audio container above the buttons", () => {
    // GIVEN a live assistant row:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    const row = addAssistantRow(h, "a-stream");
    const content = contentOf(row);

    // WHEN three sentences' worth of audio is injected (streaming mode),
    h.exec(`
        addAudioButtonToAssistantMessage("a-stream", "a-stream_1.wav");
        addAudioButtonToAssistantMessage("a-stream", "a-stream_2.wav");
        addAudioButtonToAssistantMessage("a-stream", "a-stream_3.wav");
    `);

    // THEN one container holds all three buttons, still above the action row:
    assert.equal(content.querySelectorAll(".message-audio").length, 1);
    assert.deepEqual(childClasses(content), ["bubble-name", "bubble", "message-audio", "message-actions"]);
    assert.equal(content.querySelector(".message-audio").children.length, 3);
});

test("a row without an action wrapper still gets its audio container appended (anchor fallback)", () => {
    // GIVEN a content container with no buttons at all:
    const h = createHarness({ clipboard: recordingClipboard([]) });
    h.exec(`
        const row = document.createElement("div");
        row.className = "message-row assistant";
        row.dataset.messageId = "a-noactions";
        const content = document.createElement("div");
        content.className = "bubble-content";
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        bubble.textContent = "no buttons here";
        content.appendChild(bubble);
        row.appendChild(content);
        messagesEl.appendChild(row);
        addAudioButtonToAssistantMessage("a-noactions", "a-noactions_1.wav");
    `);
    const content = contentOf(h.messagesEl.children[h.messagesEl.children.length - 1]);

    // THEN the audio container is simply the last child:
    assert.deepEqual(childClasses(content), ["bubble", "message-audio"]);
});
