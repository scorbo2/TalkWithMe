/**
 * tts.js — Text-to-Speech: toggle, audio queues, streaming, playback.
 *
 * Supports two modes:
 *  - Non-streaming: enqueue full text after LLM finishes responding.
 *  - Streaming: split response into sentences, fetch and play in a pipeline.
 *
 * Audio persistence: each TTS fetch carries its own message ID so audio
 * is always associated with the correct message, even when the global
 * currentAssistantMessageId has been cleared by a subsequent message.
 */

/* ==========================================================================
   Toggle UI
   ========================================================================== */

function updateTTSToggleUI() {
    if (ttsEnabled) {
        ttsIcon.textContent = "\u{1F50A}";  // 🔊
        ttsIcon.classList.remove("muted");
    } else {
        ttsIcon.textContent = "\u{1F507}";  // 🔇
        ttsIcon.classList.add("muted");
    }
}

function toggleTTS() {
    if (!ttsAvailable) return;
    ttsEnabled = !ttsEnabled;
    updateTTSToggleUI();
}

/* ==========================================================================
   Non-streaming TTS (enqueue full text after LLM finishes)
   ========================================================================== */

function enqueueTTS(personaName, text) {
    // Capture the current assistant message ID so this audio request
    // knows which message it belongs to, even if the global ID changes
    // before the async fetch completes.
    audioQueue.push({ personaName, text, messageId: currentAssistantMessageId });
    processAudioQueue();
}

async function processAudioQueue() {
    if (isPlayingAudio || audioQueue.length === 0) return;
    isPlayingAudio = true;

    const item = audioQueue.shift();
    try {
        const audioBuffer = await fetchTTS(item.personaName, item.text, item.messageId);
        if (audioBuffer) {
            await playAudio(audioBuffer);
        }
    } catch (err) {
        console.warn("TTS playback error:", err);
    } finally {
        isPlayingAudio = false;
        setTimeout(() => processAudioQueue(), 100);
    }
}

/* ==========================================================================
   Streaming TTS (sentence-by-sentence: fetch and play are pipelined)
   ========================================================================== */

/**
 * Split accumulated text into complete sentences (ending with . ! ?)
 * Returns the sentences found and any remaining fragment without a terminal.
 */
function extractSentences(text) {
    const sentences = [];
    const regex = /[^.!?]*[.!?]+/g;
    let lastIndex = 0;
    let match;
    while ((match = regex.exec(text)) !== null) {
        const s = match[0].trim();
        if (s) sentences.push(s);
        lastIndex = regex.lastIndex;
    }
    return { sentences, remaining: text.slice(lastIndex) };
}

/**
 * Append a token to the sentence buffer and queue any newly complete sentences.
 */
function accumulateForTTS(token, personaName) {
    sentenceBuffer += token;
    const { sentences, remaining } = extractSentences(sentenceBuffer);
    sentenceBuffer = remaining;
    for (const sentence of sentences) {
        enqueueStreamingTTS(personaName, sentence);
    }
}

/** Push a sentence into the fetch queue and kick off the fetch pipeline. */
function enqueueStreamingTTS(personaName, text) {
    // In streaming mode, message_id isn't known until the "done" event.
    // Pass null here; backfillStreamingTTSMessageId() will fill it in when "done" fires.
    ttsRequestQueue.push({ personaName, text, messageId: null });
    processTTSRequests();
}

/**
 * Called by the "done" event handler to assign the now-known message ID to
 * all still-queued (not yet fetched) streaming TTS items for this persona,
 * and to store it so any currently in-flight fetches for this persona resolve
 * correctly via the per-persona map.
 */
function backfillStreamingTTSMessageId(personaName, messageId) {
    streamingTTSMessageIdByPersona.set(personaName, messageId);
    for (const item of ttsRequestQueue) {
        if (item.personaName === personaName && item.messageId === null) {
            item.messageId = messageId;
        }
    }
}

/**
 * Fetch TTS for queued sentences serially (to preserve order).
 * Runs concurrently with audio playback so the next sentence's audio
 * is ready by the time the current one finishes playing.
 */
async function processTTSRequests() {
    if (isFetchingTTS || ttsRequestQueue.length === 0) return;
    isFetchingTTS = true;

    const item = ttsRequestQueue.shift();
    try {
        const audioBuffer = await fetchTTS(item.personaName, item.text, item.messageId);
        if (audioBuffer) {
            audioBufferQueue.push(audioBuffer);
            processAudioBufferQueue();
        }
    } catch (err) {
        console.warn("TTS streaming fetch error:", err);
    } finally {
        isFetchingTTS = false;
        // Immediately fetch the next sentence if one is waiting
        setTimeout(() => processTTSRequests(), 0);
    }
}

/**
 * Play decoded audio buffers in order, with a small gap between sentences.
 * Runs independently of the fetch pipeline so playback starts as soon as
 * the first buffer is ready.
 */
async function processAudioBufferQueue() {
    if (isPlayingAudioBuffer || audioBufferQueue.length === 0) return;
    isPlayingAudioBuffer = true;

    const buffer = audioBufferQueue.shift();
    try {
        await playAudio(buffer);
        await new Promise(resolve => setTimeout(resolve, 80)); // brief inter-sentence gap
    } catch (err) {
        console.warn("Audio buffer playback error:", err);
    } finally {
        isPlayingAudioBuffer = false;
        processAudioBufferQueue();
    }
}

/* ==========================================================================
   Shared TTS helpers
   ========================================================================== */

/**
 * Fetch TTS audio from the server and persist it to disk.
 *
 * @param {string} personaName - Which persona to synthesize for.
 * @param {string} text - Text to synthesize.
 * @param {string|null} messageId - The message ID this audio belongs to.
 *   For queued streaming items this is backfilled by backfillStreamingTTSMessageId()
 *   when "done" fires. For items already dequeued (in-flight), falls back to
 *   streamingTTSMessageIdByPersona, which is keyed per persona so that a
 *   subsequent persona's "start" event never clobbers a prior persona's entry.
 *
 */
async function fetchTTS(personaName, text, messageId) {
    const resp = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, persona_name: personaName }),
    });

    if (!resp.ok) {
        console.warn("TTS request failed:", resp.status);
        return null;
    }

    const data = await resp.json();
    if (!data.audio_base64) return null;

    // Use the passed messageId if available (non-streaming, or backfilled by backfillStreamingTTSMessageId).
    // Fall back to the per-persona map for in-flight fetches whose item was already dequeued
    // before "done" fired. Using a per-persona Map means a subsequent persona's "start" event
    // cannot clobber the prior persona's entry.
    const effectiveId = messageId || streamingTTSMessageIdByPersona.get(personaName) || null;
    if (effectiveId) {
        uploadAudio(currentChatRoom, effectiveId, data.audio_base64, "audio/wav")
            .then(result => {
                if (result && result.filename) {
                    // Inject a playback button into the live chat bubble
                    addAudioButtonToAssistantMessage(effectiveId, result.filename);
                }
            })
            .catch(err => console.warn("Failed to persist TTS audio:", err));
    } else {
        // Safety net: if somehow no ID is available yet, buffer by persona for later flush.
        const buf = ttsAudioBufferByPersona.get(personaName) || [];
        buf.push({ audio_base64: data.audio_base64, mime_type: "audio/wav" });
        ttsAudioBufferByPersona.set(personaName, buf);
    }

    // Decode base64 to ArrayBuffer for playback
    const binary = atob(data.audio_base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }

    // Initialize AudioContext lazily (requires user gesture, which we have from the chat flow)
    if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }

    return await audioCtx.decodeAudioData(bytes.buffer);
}

function playAudio(buffer) {
    return new Promise((resolve) => {
        const source = audioCtx.createBufferSource();
        source.buffer = buffer;
        source.connect(audioCtx.destination);
        source.onended = resolve;
        source.start();
    });
}

/**
 * Persist any audio buffered for a persona during streaming TTS.
 *
 * During streaming, TTS sentences are fetched as tokens arrive, but the
 * assistant's message_id isn't known until the "done" event. This function
 * flushes the buffer for the given persona once the ID is available.
 */
function flushTtsAudioBuffer(personaName, messageId) {
    if (!messageId || !personaName) return;
    const buf = ttsAudioBufferByPersona.get(personaName);
    if (!buf || buf.length === 0) return;
    for (const entry of buf) {
        uploadAudio(currentChatRoom, messageId, entry.audio_base64, entry.mime_type)
            .then(result => {
                if (result && result.filename) {
                    addAudioButtonToAssistantMessage(messageId, result.filename);
                }
            })
            .catch(err => console.warn("Failed to persist buffered TTS audio:", err));
    }
    ttsAudioBufferByPersona.delete(personaName);
}
