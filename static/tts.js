/**
 * tts.js — Text-to-Speech: toggle, audio queues, streaming, playback.
 *
 * Supports two modes:
 *  - Non-streaming: enqueue full text after LLM finishes responding.
 *  - Streaming: split response into sentences, fetch and play in a pipeline.
 *
 * Audio persistence: each TTS item is stamped with its message ID at
 * enqueue time (the ID is issued by the server in the "start" event), so
 * audio is always associated with the correct message regardless of when
 * the fetch resolves — no shared-state lookup at resolution time.
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
    // Stamp the current message ID at enqueue time. It was issued by the
    // server in the "start" event, so it is already correct for this
    // response — no backfilling needed when "done" arrives.
    ttsRequestQueue.push({ personaName, text, messageId: currentAssistantMessageId });
    processTTSRequests();
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
 *   Stamped at enqueue time from the "start" event, so it is correct
 *   regardless of when this fetch resolves.
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

    // Persist the audio against the message it was enqueued for.
    if (messageId) {
        uploadAudio(currentChatRoom, messageId, data.audio_base64, "audio/wav")
            .then(result => {
                if (result && result.filename) {
                    // Inject a playback button into the live chat bubble
                    addAudioButtonToAssistantMessage(messageId, result.filename);
                }
            })
            .catch(err => console.warn("Failed to persist TTS audio:", err));
    } else {
        // Should not happen with the current protocol (the "start" event
        // always carries a message_id). Warn loudly so a server/frontend
        // version mismatch is visible instead of silently dropping audio.
        console.warn("fetchTTS: no message ID available; audio will play but not be persisted");
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

