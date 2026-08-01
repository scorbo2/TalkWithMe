/**
 * utils.js — Pure utility functions shared across modules.
 */

/** Scroll the message panel to the bottom, deferred to next frame. */
function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
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
