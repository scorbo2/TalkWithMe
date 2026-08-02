/**
 * persistence.js — Chat history persistence helpers.
 *
 * Handles loading persisted chat history from the server and uploading
 * audio files for messages.
 */

/* ==========================================================================
    History loading
    ========================================================================== */

/**
 * Load persisted chat history for a room from the server.
 * Returns the response body (includes messages with IDs and audio file refs).
 */
async function loadPersistedHistory(roomName) {
    try {
        const resp = await fetch(`/api/session/load-room/${encodeURIComponent(roomName)}`);
        if (!resp.ok) {
            console.warn(`Failed to load history for room '${roomName}': HTTP ${resp.status}`);
            return { room: roomName, messages: [] };
        }
        return await resp.json();
    } catch (err) {
        console.error("Failed to load persisted history:", err);
        return { room: roomName, messages: [] };
    }
}

/* ==========================================================================
    Audio upload
    ========================================================================== */

/**
 * Upload an audio file (base64) for a persisted message.
 *
 * @param {string} roomName - The chat room this audio belongs to.
 * @param {string} messageId - The UUID of the message this audio is for.
 * @param {string} audioBase64 - Base64-encoded audio data.
 * @param {string} [mimeType] - Optional MIME type of the audio.
 */
async function uploadAudio(roomName, messageId, audioBase64, mimeType) {
    try {
        const params = new URLSearchParams({ room: roomName });
        const resp = await fetch(`/api/persist/audio?${params}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                message_id: messageId,
                audio_base64: audioBase64,
                mime_type: mimeType || undefined,
            }),
        });
        if (!resp.ok) {
            console.warn(`Audio upload failed for message ${messageId}: HTTP ${resp.status}`);
            return null;
        }
        return await resp.json();
    } catch (err) {
        console.error("Failed to upload audio:", err);
        return null;
    }
}

/**
 * Upload audio from a Blob (e.g., a MediaRecorder recording).
 * Converts the blob to base64 before uploading.
 *
 * @param {string} roomName - The chat room this audio belongs to.
 * @param {string} messageId - The UUID of the message this audio is for.
 * @param {Blob} blob - The audio blob.
 * @param {string} mimeType - The MIME type of the blob.
 */
async function uploadAudioBlob(roomName, messageId, blob, mimeType) {
    const base64 = await blobToBase64(blob);
    // blobToBase64 returns a data URL; strip the prefix
    const raw = base64.split(",")[1];
    return uploadAudio(roomName, messageId, raw, mimeType);
}

/**
 * Read a Blob as a base64 data URL.
 */
function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
    });
}

/* ==========================================================================
    Audio playback
    ========================================================================== */

/**
 * Get the URL for a persisted audio file.
 */
function getAudioUrl(roomName, filename) {
    return `/api/persist/audio/${encodeURIComponent(roomName)}/${encodeURIComponent(filename)}`;
}
