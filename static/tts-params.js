/**
 * tts-params.js — Dynamic TTS parameter section of the Servers modal.
 *
 * Renders the connected TTS engine's /capabilities document (proxied by
 * the backend at GET /api/tts/capabilities) into generic input widgets —
 * zero per-engine UI code (TTS generification plan T8/T9, per
 * tts-serve/docs/01-server-generification.md §4.3).
 *
 * Structure:
 *   - pure core (DOM-free, unit-tested in tests/test_tts_settings.js):
 *       selectTtsParams(doc)          -> {renderable, advanced, error}
 *       widgetFor(spec)               -> widget spec per the T9 table
 *       collectTtsParamValues(el)     -> {name: value} from a rendered form
 *       validateTtsParamValues(vals, doc) -> error string | null
 *   - thin DOM builders:
 *       renderTtsParameters(doc, container, savedValues)
 *       renderTtsInfo(doc, container, note)
 *
 * "Empty = let the engine decide" is the universal meaning of a blank
 * field: an absent key in the collected object means the parameter is not
 * sent, and the engine applies its own default (plan T9).
 */

/* ==========================================================================
   Constants
   ========================================================================== */

/** Highest capabilities schema_version this app knows how to render (T10). */
const TTS_SUPPORTED_SCHEMA_VERSION = 2;

/**
 * App-managed request fields (plan T4): supplied from the chat request and
 * the persona, never sourced from — or rendered as — user settings. The
 * engine may still advertise them in its document; we simply never build
 * widgets for them (defense against a stale hand-edited settings.yaml).
 */
const TTS_APP_MANAGED_FIELDS = ["text", "audio_base64", "reference_text", "language"];

/* ==========================================================================
   Pure core — doc selection, widget rules, collection, validation
   ========================================================================== */

/**
 * Decide which parameter specs to render for a capabilities document (T8/T10).
 *
 * @param {object|null} doc - The /capabilities document (or null).
 * @returns {{renderable: object[], advanced: object[], error: string|null}}
 *   `renderable` are the engine-declared, non-advanced specs; `advanced` the
 *   specs rendered inside the collapsed Advanced disclosure. `error` is set
 *   (and both lists empty) when the document cannot be rendered: missing/
 *   malformed, or a schema_version this app does not support (version gate,
 *   T10 — synthesis still works with text + reference data only).
 */
function selectTtsParams(doc) {
    const result = { renderable: [], advanced: [], error: null };

    if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
        result.error = "TTS capabilities document missing or malformed — parameter options unavailable.";
        return result;
    }
    if (typeof doc.schema_version !== "number") {
        result.error = "TTS capabilities document has no schema_version — parameter options unavailable.";
        return result;
    }
    // Version gate (T10): any schema version other than the one this app
    // was built against is "unrecognized" per tts-serve §3.5 — fall back to
    // minimal mode with a notice instead of guessing at a foreign contract.
    if (doc.schema_version > TTS_SUPPORTED_SCHEMA_VERSION) {
        result.error =
            `TTS server speaks capabilities schema v${doc.schema_version} — ` +
            `this app supports up to v${TTS_SUPPORTED_SCHEMA_VERSION}.`;
        return result;
    }
    if (doc.schema_version < TTS_SUPPORTED_SCHEMA_VERSION) {
        result.error =
            `TTS server speaks capabilities schema v${doc.schema_version} — ` +
            `this app requires v${TTS_SUPPORTED_SCHEMA_VERSION}.`;
        return result;
    }

    const params = Array.isArray(doc.parameters) ? doc.parameters : [];
    for (const spec of params) {
        if (!spec || typeof spec !== "object" || typeof spec.name !== "string") continue;
        // App-managed fields are never rendered as settings (plan T4).
        if (TTS_APP_MANAGED_FIELDS.includes(spec.name)) continue;
        (spec.advanced === true ? result.advanced : result.renderable).push(spec);
    }
    return result;
}

/**
 * Map a parameter spec to its widget per the T9 table (final, testable):
 *
 *   boolean                              -> checkbox (always sent)
 *   string + enum                        -> select (leading blank option
 *                                            when default is null)
 *   string, no enum                      -> text input (empty = not sent)
 *   number + min+max+step + non-null
 *                                         default -> range slider (always sent)
 *   integer + min+max+step               -> range slider (always sent)
 *   integer + min+max, no step           -> slider if span <= 100, else number
 *   any other numeric shape              -> number input (empty = not sent)
 *   unrecognized type                    -> raw-JSON escape hatch (T9/§3.5)
 *
 * @returns {{kind: string, options?: string[], blank?: boolean}}
 */
function widgetFor(spec) {
    const type = spec ? spec.type : null;

    if (type === "boolean") {
        return { kind: "checkbox" };
    }

    if (type === "string") {
        if (Array.isArray(spec.enum) && spec.enum.length > 0) {
            // blank: true -> the select gets a leading "— not set —" option
            // (the parameter's default is null, so "not set" is a legal value).
            return { kind: "select", options: spec.enum, blank: spec.default == null };
        }
        return { kind: "text" };
    }

    if (type === "number") {
        // A slider needs a full range to work on AND a non-null default to
        // sit at: Qwen3's temperature/top_p have bounds+step but a null
        // default, so they must be blankable number inputs, not sliders.
        const sliderReady = spec.min != null && spec.max != null && spec.step != null && spec.default != null;
        return sliderReady ? { kind: "slider" } : { kind: "number" };
    }

    if (type === "integer") {
        if (spec.min != null && spec.max != null) {
            if (spec.step != null) return { kind: "slider" };
            // No step: a slider is only usable on small ranges — beyond a
            // span of 100, dragging is misery and a number input is honest.
            return spec.max - spec.min <= 100 ? { kind: "slider" } : { kind: "number" };
        }
        return { kind: "number" };
    }

    // Unrecognized type: raw-JSON escape hatch (forward-compat rule,
    // tts-serve §3.5). The user types a JSON value; the server's 422 is
    // the backstop for anything the JSON accepts but the engine rejects.
    return { kind: "json" };
}

/**
 * Collect parameter values from a rendered parameter container.
 *
 * The renderer marks each row with `dataset.ttsParam` (the settings key),
 * `dataset.ttsKind` (the widget kind), and a plain `widgetEl` property on
 * the row pointing at the value-bearing input — so collection is a
 * structural walk that works identically in a browser and in the Node
 * test harness (no querySelector needed).
 *
 * @returns {object} name -> value. Blank text/number/json fields are
 *   omitted (absent key = "let the engine decide"); checkboxes and
 *   sliders/selects-with-default are always present.
 */
function collectTtsParamValues(container) {
    const values = {};
    for (const row of findTtsParamRows(container)) {
        const name = row.dataset ? row.dataset.ttsParam : null;
        const widget = row.widgetEl;
        if (!name || !widget) continue;
        const value = readTtsWidgetValue(row.dataset ? row.dataset.ttsKind : null, widget);
        if (value !== undefined) values[name] = value;
    }
    return values;
}

/** Recursively collect all rows that carry a data-tts-param attribute. */
function findTtsParamRows(node) {
    const rows = [];
    const walk = (n) => {
        if (!n || typeof n !== "object") return;
        if (n.dataset && n.dataset.ttsParam) rows.push(n);
        // In a real browser element.children is an HTMLCollection — iterable
        // and array-like, but Array.isArray() === false. Gating the descent
        // on Array.isArray() here (the old code) never left the top
        // container, so collection silently returned {} and every save
        // wiped tts.parameters. for...of covers HTMLCollection AND the
        // Node test harness alike.
        if (n.children) {
            for (const child of n.children) walk(child);
        }
    };
    walk(node);
    return rows;
}

/**
 * Read one widget's value, coerced to the JSON type the engine expects.
 * Returns undefined for "blank / not set" (the key is then omitted).
 *
 * A json-hatch value that fails JSON.parse is returned as the raw string
 * on purpose: validateTtsParamValues turns it into a readable error, and
 * a failed save means it is never sent.
 */
function readTtsWidgetValue(kind, widget) {
    switch (kind) {
        case "checkbox":
            return widget.checked === true; // always present
        case "select":
            return widget.value === "" ? undefined : widget.value;
        case "slider":
            return parseFloat(widget.value); // always present
        case "number": {
            const raw = String(widget.value).trim();
            if (raw === "") return undefined;
            const num = Number(raw);
            return Number.isNaN(num) ? undefined : num;
        }
        case "text": {
            const raw = String(widget.value).trim();
            return raw === "" ? undefined : raw;
        }
        case "json": {
            const raw = String(widget.value).trim();
            if (raw === "") return undefined;
            try {
                return JSON.parse(raw);
            } catch {
                return raw; // invalid JSON -> validation error downstream
            }
        }
        default:
            return undefined;
    }
}

/**
 * Validate collected parameter values against a capabilities document.
 * Mirrors the server-side check (app/services/tts_client.py, plan T7) so
 * the user gets an immediate message instead of a 422 round-trip:
 * unknown names, type conformance, min/max bounds, enum membership.
 *
 * With no document (server unreachable, TTS inactive) there is nothing to
 * check against — return null and let the server's own 422 be the backstop.
 *
 * @returns {string|null} A single message naming every offending
 *   parameter, or null when everything is acceptable.
 */
function validateTtsParamValues(values, doc) {
    if (!values || typeof values !== "object") return null;
    const entries = Object.entries(values);
    if (entries.length === 0) return null;
    if (!doc || typeof doc !== "object" || !Array.isArray(doc.parameters)) {
        return null; // no doc -> nothing to validate against
    }

    const specs = {};
    for (const spec of doc.parameters) {
        if (spec && spec.name) specs[spec.name] = spec;
    }

    const errors = [];
    for (const [name, value] of entries) {
        if (value === null || value === undefined) continue; // absent = not set
        const spec = specs[name];
        if (!spec) {
            errors.push(`unknown TTS parameter '${name}'`);
            continue;
        }
        errors.push(...ttsParamValueErrors(name, value, spec));
    }
    return errors.length > 0 ? errors.join("; ") : null;
}

/** Per-spec type + bounds + enum checks (mirrors the backend's T7 rules). */
function ttsParamValueErrors(name, value, spec) {
    const type = spec.type;

    if (type === "boolean") {
        return typeof value === "boolean" ? [] : [`TTS parameter '${name}' must be a boolean`];
    }

    if (type === "integer") {
        if (typeof value !== "number" || !Number.isInteger(value)) {
            return [`TTS parameter '${name}' must be an integer, got ${value}`];
        }
        return ttsParamBoundsErrors(name, value, spec);
    }

    if (type === "number") {
        if (typeof value !== "number") {
            return [`TTS parameter '${name}' must be a number, got ${value}`];
        }
        return ttsParamBoundsErrors(name, value, spec);
    }

    if (type === "string") {
        if (typeof value !== "string") {
            return [`TTS parameter '${name}' must be a string`];
        }
        if (Array.isArray(spec.enum) && spec.enum.length > 0 && !spec.enum.includes(value)) {
            return [`TTS parameter '${name}' must be one of ${spec.enum.join(", ")}, got '${value}'`];
        }
        return [];
    }

    // Unrecognized spec type: only an unparseable escape-hatch string is a
    // client-side error; anything else is the server's 422's problem.
    if (typeof value === "string") {
        try {
            JSON.parse(value);
        } catch {
            return [`TTS parameter '${name}' must be a valid JSON value`];
        }
    }
    return [];
}

/** min/max bound checks (mirrors the backend's _bounds_errors). */
function ttsParamBoundsErrors(name, value, spec) {
    const errors = [];
    if (typeof spec.min === "number" && value < spec.min) {
        errors.push(`TTS parameter '${name}' must be >= ${spec.min}, got ${value}`);
    }
    if (typeof spec.max === "number" && value > spec.max) {
        errors.push(`TTS parameter '${name}' must be <= ${spec.max}, got ${value}`);
    }
    return errors;
}

/* ==========================================================================
   DOM builders
   ========================================================================== */

/**
 * Render the dynamic parameter section into `container`.
 *
 * Clears the container first (the modal is the only consumer), then lays
 * out one row per renderable spec and, when present, a collapsed
 * <details> "Advanced" disclosure for the advanced specs. A version-gate
 * or malformed document renders a single notice line and no inputs (T10).
 *
 * @param {object|null} doc - The capabilities document (null = clear).
 * @param {object} container - The #sf-tts-params element.
 * @param {object} savedValues - The server's current tts.parameters;
 *   pre-fills the widgets for whichever names the document advertises.
 */
function renderTtsParameters(doc, container, savedValues) {
    container.innerHTML = "";
    if (!doc) return; // nothing to render — the info line explains why

    const { renderable, advanced, error } = selectTtsParams(doc);
    if (error) {
        container.appendChild(makeTtsNotice(error));
        return;
    }

    const saved = savedValues && typeof savedValues === "object" ? savedValues : {};
    const hasRows = renderable.length > 0 || advanced.length > 0;

    if (renderable.length > 0) {
        container.appendChild(buildTtsParamList(renderable, saved));
    }
    if (advanced.length > 0) {
        const details = document.createElement("details");
        details.className = "tts-advanced";
        const summary = document.createElement("summary");
        summary.textContent = `Advanced (${advanced.length})`;
        details.appendChild(summary);
        details.appendChild(buildTtsParamList(advanced, saved));
        container.appendChild(details);
    }
    if (!hasRows) {
        container.appendChild(makeTtsNotice("This engine exposes no user-configurable parameters."));
    }
}

/** A stacked list of parameter rows. */
function buildTtsParamList(specs, saved) {
    const list = document.createElement("div");
    list.className = "tts-param-list";
    for (const spec of specs) list.appendChild(buildTtsParamRow(spec, saved));
    return list;
}

/** One labelled row: label (+ bounds hint, description tooltip) + widget. */
function buildTtsParamRow(spec, saved) {
    const widget = widgetFor(spec);

    const row = document.createElement("div");
    row.className = "form-row tts-param-row";
    row.dataset.ttsParam = spec.name;
    row.dataset.ttsKind = widget.kind;

    const label = document.createElement("label");
    label.setAttribute("for", `tts-param-${spec.name}`);
    if (spec.description) label.title = spec.description;
    label.textContent = spec.name;
    const hint = ttsParamHint(spec);
    if (hint) {
        const hintEl = document.createElement("span");
        hintEl.className = "field-hint";
        hintEl.textContent = `(${hint})`;
        label.appendChild(document.createTextNode(" "));
        label.appendChild(hintEl);
    }
    row.appendChild(label);

    const built = buildTtsWidget(spec, widget, saved);
    // `element` is what goes into the document (for a slider: its wrapper
    // div); a plain object would make a real DOM throw on appendChild.
    row.appendChild(built.element);
    // Plain property (not a DOM attribute): the collection walk reads it
    // directly, which behaves the same in the browser and in Node.
    row.widgetEl = built.widget;
    return row;
}

/** Bounds/step hint for the label, e.g. "(0.0–2.0, step 0.05)". */
function ttsParamHint(spec) {
    const parts = [];
    if (typeof spec.min === "number" && typeof spec.max === "number") {
        parts.push(`${spec.min}–${spec.max}`);
    } else if (typeof spec.min === "number") {
        parts.push(`min ${spec.min}`);
    } else if (typeof spec.max === "number") {
        parts.push(`max ${spec.max}`);
    }
    if (typeof spec.step === "number") parts.push(`step ${spec.step}`);
    return parts.join(", ");
}

/**
 * Build the value-bearing widget for a spec.
 *
 * @returns {{element: object, widget: object}} `element` is what gets
 *   appended to the row (for sliders: the wrapper div holding the range
 *   input plus its live readout); `widget` is the value-bearing element
 *   that collectTtsParamValues reads from.
 */
function buildTtsWidget(spec, widget, saved) {
    const id = `tts-param-${spec.name}`;
    const savedValue = Object.prototype.hasOwnProperty.call(saved, spec.name) ? saved[spec.name] : null;

    switch (widget.kind) {
        case "checkbox": {
            const el = document.createElement("input");
            el.type = "checkbox";
            el.id = id;
            el.checked = savedValue !== null ? savedValue === true : spec.default === true;
            return { widget: el, element: el };
        }

        case "select": {
            const el = document.createElement("select");
            el.id = id;
            if (widget.blank) {
                const blank = document.createElement("option");
                blank.value = "";
                blank.textContent = "— not set —";
                el.appendChild(blank);
            }
            for (const option of widget.options) {
                const opt = document.createElement("option");
                opt.value = option;
                opt.textContent = option;
                el.appendChild(opt);
            }
            el.value = savedValue !== null ? String(savedValue) : (widget.blank ? "" : String(spec.default));
            return { widget: el, element: el };
        }

        case "slider": {
            const range = document.createElement("input");
            range.type = "range";
            range.id = id;
            range.min = String(spec.min);
            range.max = String(spec.max);
            range.step = String(spec.step != null ? spec.step : (spec.type === "integer" ? 1 : 0.1));
            const value = savedValue !== null ? parseFloat(savedValue) : parseFloat(spec.default);
            range.value = String(value);

            const readout = document.createElement("span");
            readout.className = "tts-slider-readout";
            readout.textContent = String(value);
            range.addEventListener("input", () => {
                readout.textContent = String(parseFloat(range.value));
            });

            const wrap = document.createElement("div");
            wrap.className = "tts-slider-row";
            wrap.appendChild(range);
            wrap.appendChild(readout);
            return { widget: range, element: wrap };
        }

        case "number": {
            const el = document.createElement("input");
            el.type = "number";
            el.id = id;
            if (typeof spec.min === "number") el.min = String(spec.min);
            if (typeof spec.max === "number") el.max = String(spec.max);
            if (typeof spec.step === "number") el.step = String(spec.step);
            el.value = savedValue !== null ? String(savedValue) : "";
            return { widget: el, element: el };
        }

        case "json": {
            // Escape hatch: the raw JSON text of the stored value.
            const el = document.createElement("input");
            el.type = "text";
            el.id = id;
            el.value = savedValue !== null ? JSON.stringify(savedValue) : "";
            return { widget: el, element: el };
        }

        case "text":
        default: {
            const el = document.createElement("input");
            el.type = "text";
            el.id = id;
            el.value = savedValue !== null ? String(savedValue) : "";
            return { widget: el, element: el };
        }
    }
}

/**
 * Render the engine information block (plan T10): identity, sample rate,
 * and the watermark disclosure. Also the single place that writes the
 * "nothing to show" status lines (server unreachable, not configured),
 * via `note` with a null doc.
 *
 * @param {object|null} doc - The capabilities document (null = status line only).
 * @param {object} container - The #sf-tts-info element.
 * @param {string} [note] - Optional extra line (e.g. the engine-switch
 *   warning about discarded unsaved parameter edits).
 */
function renderTtsInfo(doc, container, note) {
    container.innerHTML = "";

    if (doc && typeof doc === "object") {
        const block = document.createElement("div");
        block.className = "tts-info-block";
        appendTtsInfoRow(block, "Engine", doc.engine);
        appendTtsInfoRow(block, "Model", doc.model);
        appendTtsInfoRow(block, "Device", doc.device);
        if (typeof doc.sample_rate === "number") {
            appendTtsInfoRow(block, "Sample rate", `${doc.sample_rate} Hz`);
        }
        // Responsible-AI disclosure (tts-serve §7.5): the end user should
        // know when the engine marks its output.
        if (doc.watermarked === true) {
            const warn = document.createElement("div");
            warn.className = "tts-info-warn";
            warn.textContent = "This engine applies a neural watermark to the audio it generates.";
            block.appendChild(warn);
        }
        container.appendChild(block);
    }

    if (note) container.appendChild(makeTtsInfoLine(note));
}

/** One key/value line inside the info block. */
function appendTtsInfoRow(parent, key, value) {
    const row = document.createElement("div");
    row.className = "tts-info-row";
    const k = document.createElement("span");
    k.className = "tts-info-key";
    k.textContent = key;
    const v = document.createElement("span");
    v.className = "tts-info-value";
    v.textContent = value === null || value === undefined ? "—" : String(value);
    row.appendChild(k);
    row.appendChild(v);
    parent.appendChild(row);
}

/** A plain status/notice line (version gate, unreachable server, …). */
function makeTtsInfoLine(text) {
    const line = document.createElement("div");
    line.className = "tts-info-line";
    line.textContent = text;
    return line;
}

/** A notice rendered inside the parameter container. */
function makeTtsNotice(text) {
    const notice = document.createElement("div");
    notice.className = "tts-notice";
    notice.textContent = text;
    return notice;
}
