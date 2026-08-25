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
