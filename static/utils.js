/**
 * utils.js — Pure utility functions shared across modules.
 */

/** Scroll the message panel to the bottom, deferred to next frame. */
function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    });
}

/**
 * Extract a human-readable message from a FastAPI error response body.
 *
 * `detail` is a plain string for HTTPException, but a 422 validation
 * error returns an *array* of {loc, msg, type} objects — passing that
 * array to textContent renders as "[object Object]".
 *
 * @param {object} errBody - Parsed JSON error body (may be empty).
 * @param {number} status - HTTP status code, used for the fallback message.
 * @returns {string} Human-readable error message.
 */
function extractApiErrorMessage(errBody, status) {
    const detail = errBody ? errBody.detail : undefined;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
        return detail
            .map((e) => {
                // loc looks like ["body", "description"] or ["path", "name"] —
                // drop the transport-level segment and keep the field name.
                const field = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
                return e.msg ? (field ? `${field}: ${e.msg}` : e.msg) : "unknown error";
            })
            .join("; ");
    }
    return `Error ${status}`;
}

/**
 * Case-insensitive alphabetical comparator for personas (anything with a
 * `name` property). Pair with Array.prototype.sort, e.g.
 * `[...personas].sort(comparePersonasByName)`.
 *
 * `sensitivity: "base"` makes the comparison case-insensitive ("alice"
 * before "Bob"), matching how chat room names are sorted in chatrooms.js.
 */
function comparePersonasByName(a, b) {
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
}

/**
 * Generate a v4 UUID for a message ID.
 *
 * `crypto.randomUUID()` only exists in secure contexts (HTTPS, or the
 * browser's "localhost" exception) — it's `undefined` when the app is
 * reached over plain HTTP by IP or hostname (e.g. from another machine on
 * the LAN with `--host 0.0.0.0`). Falls back to `crypto.getRandomValues()`,
 * which carries no such restriction.
 */
function generateMessageId() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        return crypto.randomUUID();
    }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Escape HTML special characters to prevent XSS in dynamically rendered text. */
function escapeHtml(str) {
    if (typeof str !== 'string') return str;

    return str.replace(/[&<>"']/g, match => {
        return {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        }[match];
    });
}

/**
 * Expressive speech: the vocal events the TTS engine performs. Keep in sync
 * with VOCAL_EVENTS in app/services/expressive.py (tests/test_expressive.py
 * compares the two lists).
 */
const VOCAL_EVENTS = [
    "laugh", "cough", "clears throat", "sigh",
    "laughs", "chuckle", "giggle", "snicker", "scoff", "snort",
    "gasp", "groan", "grunt", "sniff", "sob", "cry", "scream", "yawn", "sneeze", "hiccup",
    "hum", "hmm", "uh", "um", "tsk",
    "inhale", "exhale", "deep breath", "breathes heavily", "pause",
    "whisper", "whispers", "shouts", "mumbles",
];

/** The event a whole bracket part names, inflections included (sighing -> sigh). */
function eventForm(part) {
    // An event's own name wins over another event's inflection ("laughs").
    if (VOCAL_EVENTS.includes(part)) return part;
    for (const event of VOCAL_EVENTS) {
        const forms = [event, event + "s", event + "es", event + "ing", event + "ed"];
        if (event.endsWith("e")) forms.push(event.slice(0, -1) + "ing", event.slice(0, -1) + "ed");
        if (forms.includes(part)) return event;
    }
    return null;
}

/**
 * {sigh} -> (sigh), {cough, sigh} -> (cough) (sigh): braces holding only
 * sounds are sounds, never a direction. Mirrors the curly-brace rule of
 * _bracket_events() in app/services/expressive.py.
 */
function eventsOutOfBraces(text) {
    return text.replace(/\{([^{}]*)\}/g, (tag, inner) => {
        const parts = inner.trim().toLowerCase()
            .split(/\s*(?:,|;|\/|\band\b|\bthen\b)\s*/)
            .filter(Boolean);
        const events = parts.map(eventForm);
        if (!parts.length || events.some(e => !e)) return tag;
        return events.map(e => `(${e})`).join(" ");
    });
}

/**
 * Expressive speech: the chat-bubble view of a reply. Removes {direction}
 * tags, including a still-unclosed one at the end of a streaming reply (so
 * a half-received tag never flashes up), and tidies the spaces they leave.
 * Vocal events like (laugh) stay visible: they read like stage directions.
 */
function stripDirectionTags(text) {
    return eventsOutOfBraces(text)
        .replace(/\{[^{}]*\}/g, " ")
        .replace(/\{[^{}]*$/, "")
        .replace(/[ \t]{2,}/g, " ")
        .replace(/ +([,.;:!?])/g, "$1")
        .replace(/^ +/gm, "");
}

/**
 * Expressive speech: the direction tags in one sentence.
 * Returns { first, last } (null when absent): `first` directs this
 * sentence, `last` carries on to the following ones.
 */
function findDirectionTags(text) {
    const tags = [...eventsOutOfBraces(text).matchAll(/\{([^{}]*)\}/g)]
        .map(m => m[1].trim())
        .filter(Boolean);
    return { first: tags[0] ?? null, last: tags[tags.length - 1] ?? null };
}
