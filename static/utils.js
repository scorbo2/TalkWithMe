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
 * Fill an avatar element with the persona's avatar image, falling back to
 * the persona's first initial on a colored bubble when no image is set.
 * Shared by the sidebar and the Persona Editor list so the two render
 * personas identically and can never drift apart.
 *
 * `avatar_image` on the summary/detail payloads is only a presence flag;
 * the file itself is served by the dedicated avatar endpoint. The flag can
 * be stale (file deleted behind our back), hence the onerror fallback to
 * the initial.
 *
 * @param {HTMLElement} avatarEl - Round bubble container (gets the persona's
 *        avatar_color as its background, for the initial fallback).
 * @param {object} persona - Persona with `name`, `avatar_color`, `avatar_image`.
 */
function renderPersonaAvatar(avatarEl, persona) {
    avatarEl.style.backgroundColor = persona.avatar_color;

    if (persona.avatar_image) {
        const img = document.createElement("img");
        img.src = `/api/personas/${encodeURIComponent(persona.name)}/avatar`;
        img.alt = persona.name;
        img.onerror = () => {
            // Flag said yes, endpoint said no — degrade to the initial.
            avatarEl.innerHTML = "";
            avatarEl.textContent = persona.name.charAt(0).toUpperCase();
        };
        avatarEl.appendChild(img);
    } else {
        avatarEl.textContent = persona.name.charAt(0).toUpperCase();
    }
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

/**
 * Copy text to the system clipboard, with a fallback for insecure origins.
 *
 * `navigator.clipboard` only exists in a secure context (HTTPS, or the
 * browser's "localhost" exception) — it is `undefined` when the app is
 * reached over plain HTTP by IP or hostname, exactly the LAN gotcha that
 * made `crypto.randomUUID()` unusable (see generateMessageId() above, and
 * the #145 "send button throws over http" fix). So: use the Clipboard API
 * when the browser offers it, otherwise fall back to a hidden textarea +
 * `document.execCommand("copy")`, which works in insecure contexts too.
 *
 * Resolves to a boolean instead of rejecting: the caller has no recovery
 * path other than telling the user, so it gets a yes/no it can paint from.
 *
 * @param {string} text - The text to put on the clipboard.
 * @returns {Promise<boolean>} true when the browser reports the copy landed.
 */
async function copyTextToClipboard(text) {
    const clipboard = typeof navigator !== "undefined" ? navigator.clipboard : null;
    if (clipboard && typeof clipboard.writeText === "function") {
        try {
            await clipboard.writeText(text);
            return true;
        } catch (err) {
            // Permission denied (or the browser simply refused the write) is
            // not fatal: the legacy path below frequently still succeeds.
            console.warn("Clipboard API write failed, trying legacy copy:", err);
        }
    }
    return legacyCopyToClipboard(text);
}

/**
 * execCommand("copy") fallback for browsers with no Clipboard API.
 *
 * The scratch textarea must be *off-screen*, not `display: none` / not
 * `hidden`: an element that is not rendered cannot be selected, and the
 * whole trick is a selection followed by a copy command.
 */
function legacyCopyToClipboard(text) {
    if (typeof document === "undefined" || typeof document.execCommand !== "function") {
        console.warn("This browser exposes no clipboard API; cannot copy text.");
        return false;
    }

    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.setAttribute("readonly", "");   // avoids the mobile keyboard popping up
    scratch.style.position = "fixed";
    scratch.style.top = "-1000px";
    scratch.style.opacity = "0";
    document.body.appendChild(scratch);
    scratch.select();

    let copied = false;
    try {
        copied = document.execCommand("copy");
    } catch (err) {
        console.warn("Legacy clipboard copy failed:", err);
    }
    scratch.remove();
    return copied;
}

/**
 * Close a modal when its dark backdrop is clicked — but only when the
 * whole press happened on the backdrop.
 *
 * Why not the obvious `overlay.addEventListener("click", ...)` with an
 * `e.target === overlay` test: the browser fires `click` on the nearest
 * common ancestor of the mousedown target and the mouseup target. Press
 * a checkbox (or a colour picker, slider, button, file input) inside the
 * modal, drag left onto the backdrop, release — mousedown target: the
 * control, mouseup target: the overlay, so the common ancestor IS the
 * overlay, `e.target === overlay` looks exactly like a backdrop click,
 * and the modal vanishes mid-drag with the half-filled form inside it.
 * Text inputs dodge that synthesis because there the drag is a text
 * selection gesture and no click is generated at all — which is what made
 * the bug look like a browser gremlin: only the non-text controls
 * triggered it.
 *
 * So the decision is made from the press pair, never from the synthetic
 * click: close only when the pointer went DOWN and UP on the backdrop
 * itself. Presses that start inside the modal still bubble here (the
 * overlay is an ancestor of the modal box), which re-arms the flag on
 * every press — so a press released outside the window, where no mouseup
 * is delivered at all, can never leave a stale "close me" behind.
 *
 * @param {HTMLElement} overlay - The full-viewport .modal-overlay element.
 * @param {function} closeFn - Called when a genuine backdrop click completes.
 */
function installBackdropClose(overlay, closeFn) {
    let pressStartedOnBackdrop = false;

    overlay.addEventListener("mousedown", (e) => {
        pressStartedOnBackdrop = e.target === overlay;
    });

    overlay.addEventListener("mouseup", (e) => {
        const clickCompletedOnBackdrop = pressStartedOnBackdrop && e.target === overlay;
        pressStartedOnBackdrop = false;
        if (clickCompletedOnBackdrop) closeFn();
    });
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
