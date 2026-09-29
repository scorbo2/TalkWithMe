/**
 * test_mobile_layout.js — the mobile drawer in static/mobile.js (issue #143).
 *
 * Run with plain Node (Node 20+, no npm packages, no network):
 *
 *     node tests/test_mobile_layout.js
 *
 * Invariants: the ☰ button is inserted first in the top bar and toggles
 * body.sidebar-open (with aria-expanded kept in sync); a backdrop click and
 * a chat-room change close the drawer; going back to a wide screen clears
 * it. The CSS itself (media query, dvh) is not testable here.
 *
 * NOTE: intentionally NOT part of the pytest suite.
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function makeElement(id) {
    const listeners = {};
    const classes = new Set();
    const attrs = {};
    return {
        id,
        children: [],
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
            toggle(c, force) {
                const want = force === undefined ? !classes.has(c) : !!force;
                if (want) classes.add(c); else classes.delete(c);
                return want;
            },
        },
        setAttribute: (k, v) => { attrs[k] = String(v); },
        getAttribute: (k) => attrs[k],
        addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
        fire: (type, event = {}) => (listeners[type] || []).forEach((fn) => fn(event)),
        insertBefore(child) { this.children.unshift(child); },
        appendChild(child) { this.children.push(child); },
        get firstChild() { return this.children[0] || null; },
    };
}

function load({ withRoomDropdown = true } = {}) {
    const byId = {
        topbar: makeElement("topbar"),
        sidebar: makeElement("sidebar"),
    };
    if (withRoomDropdown) byId["chat-room-dropdown"] = makeElement("chat-room-dropdown");
    const body = makeElement("body");
    let mediaListener = null;
    const sandbox = {
        document: {
            getElementById: (id) => byId[id] || null,
            createElement: (tag) => makeElement(`new-${tag}`),
            body,
        },
        window: {
            matchMedia: () => ({ addEventListener: (_t, fn) => { mediaListener = fn; } }),
        },
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "mobile.js"), "utf8"), sandbox);
    const toggle = byId.topbar.children[0];
    const backdrop = body.children[0];
    return { byId, body, toggle, backdrop, wideScreen: () => mediaListener({ matches: true }) };
}

test("the toggle is the first element of the top bar", () => {
    const h = load();
    assert.equal(h.toggle.id, "btn-sidebar-toggle");
    assert.equal(h.toggle.getAttribute("aria-controls"), "sidebar");
});

test("the toggle opens and closes the drawer", () => {
    const h = load();
    h.toggle.fire("click");
    assert.ok(h.body.classList.contains("sidebar-open"));
    assert.equal(h.toggle.getAttribute("aria-expanded"), "true");
    h.toggle.fire("click");
    assert.ok(!h.body.classList.contains("sidebar-open"));
    assert.equal(h.toggle.getAttribute("aria-expanded"), "false");
});

test("a backdrop click closes the drawer", () => {
    const h = load();
    h.toggle.fire("click");
    h.backdrop.fire("click");
    assert.ok(!h.body.classList.contains("sidebar-open"));
});

test("picking a chat room closes the drawer", () => {
    const h = load();
    h.toggle.fire("click");
    h.byId["chat-room-dropdown"].fire("change");
    assert.ok(!h.body.classList.contains("sidebar-open"));
});

test("a wide screen clears the drawer state", () => {
    const h = load();
    h.toggle.fire("click");
    h.wideScreen();
    assert.ok(!h.body.classList.contains("sidebar-open"));
});

test("missing top bar or sidebar: does nothing, no crash", () => {
    const sandbox = { document: { getElementById: () => null } };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "mobile.js"), "utf8"), sandbox);
});
