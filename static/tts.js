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
    Stop button (temporary "mute" — see docs/feature_stop_button.md)
    ========================================================================== */

/**
 * Sync the stop button with reality: enabled while any audio source is
 * producing sound, disabled otherwise. Called on every source start and
 * end (from playAudioSource) so the button never lags a frame behind
 * the audio.
 */
function updateStopButtonUI() {
    stopAudioBtn.disabled = activeAudioSources.size === 0;
}

/**
 * Stop button click: halt every source currently producing sound and
 * engage the mute. Muted audio is still fetched and persisted, and still
 * gets its replay button — it just never plays, until the next user
 * prompt or a manual replay click clears audioPlaybackStopped.
 *
 * The button is disabled immediately (per the spec), not when the async
 * onended callbacks finish draining the source set.
 */
function stopAudioPlayback() {
    if (audioPlaybackStopped) return;
    audioPlaybackStopped = true;
    stopAllAudioSources();
    stopAudioBtn.disabled = true;
}

/**
 * Stop every active BufferSource. Each source is removed from the set
 * BEFORE stop() so its async onended handler is a harmless no-op; the
 * try/catch covers the browser race where a source ends naturally
 * between the set iteration and the stop() call (stop() then throws
 * InvalidStateError, but onended has already done the cleanup).
 */
function stopAllAudioSources() {
    for (const source of [...activeAudioSources]) {
        activeAudioSources.delete(source);
        try {
            source.stop();
        } catch (err) {
            // The source ended naturally a moment ago: nothing left to stop.
            console.warn("stopAllAudioSources: source already ended:", err);
        }
    }
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
        // "Stop" is a mute, not a discard: fetchTTS already persisted this
        // audio and injected its replay button, so a muted item simply
        // skips the playback — and the highlight, since nothing is audible.
        // The flag is checked at playback start, so a mute lifted by the
        // next user prompt mid-fetch still lets this item play.
        if (audioBuffer && !audioPlaybackStopped) {
            // Brighten the row while this reply's audio plays.
            beginSpeaking(item.messageId);
            try {
                await playAudioSource(audioBuffer);
            } finally {
                endSpeaking(item.messageId);
            }
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
 * Words that take a "." without ending the sentence ("Mrs. Hudson", "vs. them").
 * Matched lowercase with dots removed. Deliberately short: a missed split only
 * makes one TTS chunk longer, while a wrong split audibly breaks a sentence.
 * Dotted forms ("e.g.", "i.e.", "U.S.") and initials are handled by a pattern
 * in isSentenceEnd(), so they are not listed here.
 */
const NON_TERMINAL_ABBREVIATIONS = new Set([
    "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "mt",
    "vs", "etc", "cf", "approx", "fig", "vol",
]);

/**
 * Candidate sentence boundaries:
 *   - . ! ? … (plus any closing quotes/brackets) followed by whitespace. The
 *     whitespace is required so a streamed "3." / "Mrs." is not cut before the
 *     next token shows whether it continues ("3.50", "Mrs. Hudson").
 *   - CJK 。！？ (plus closers), which are not followed by spaces.
 *   - a line break: list items and paragraphs are separate chunks even
 *     without punctuation.
 */
const SENTENCE_BOUNDARY_RE = /[.!?…]+["'”’»)\]]*(?=\s)|[。！？]+["'”’」』)\]]*|\n/g;

/**
 * Decide whether the punctuation `punct` really ends the sentence whose text
 * so far is `before`. Only a lone "." can be a false alarm.
 */
function isSentenceEnd(before, punct) {
    if (punct !== ".") return true;
    const word = (before.match(/\S+$/) || [""])[0];
    const bare = word.replace(/^["'“‘«(\[]+/, "");
    // "1." opening a line or chunk is a numbered-list marker, not a sentence.
    if (/^\d+$/.test(bare) && before.trim() === word) return false;
    // Initials and dotted abbreviations: "J. R. R. Tolkien", "U.S.", "e.g.".
    if (/^(?:[A-Za-z]\.)*[A-Za-z]$/.test(bare)) return false;
    return !NON_TERMINAL_ABBREVIATIONS.has(bare.replace(/\./g, "").toLowerCase());
}

/**
 * Split accumulated text into complete sentences.
 * Returns the sentences found and the remaining fragment, which is not yet
 * known to be complete (the caller flushes it when the response is done).
 */
function extractSentences(text) {
    const sentences = [];
    const boundary = new RegExp(SENTENCE_BOUNDARY_RE);
    let start = 0;
    let match;
    while ((match = boundary.exec(text)) !== null) {
        if (match[0] !== "\n" && !isSentenceEnd(text.slice(start, match.index), match[0])) {
            continue;
        }
        const end = match.index + match[0].length;
        const s = text.slice(start, end).trim();
        if (s) sentences.push(s);
        start = end;
    }
    return { sentences, remaining: text.slice(start) };
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
            // Carry the message ID into the playback queue so the row can be
            // brightened while its sentences play (see processAudioBufferQueue).
            audioBufferQueue.push({ buffer: audioBuffer, messageId: item.messageId });
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

    const item = audioBufferQueue.shift();
    if (audioPlaybackStopped) {
        // "Stop" is a mute, not a discard: this buffer was already fetched
        // and persisted (replay button included) by processTTSRequests, so
        // a muted item just drains the queue silently — no playback, no
        // highlight, no inter-sentence gap. The flag is checked per item,
        // so a mute lifted mid-drain resumes playback on the very next one.
        isPlayingAudioBuffer = false;
        setTimeout(processAudioBufferQueue, 0);
        return;
    }
    // Brighten the row while this sentence plays.
    beginSpeaking(item.messageId);
    try {
        await playAudioSource(item.buffer);
        await new Promise(resolve => setTimeout(resolve, 80)); // brief inter-sentence gap
    } catch (err) {
        console.warn("Audio buffer playback error:", err);
    } finally {
        isPlayingAudioBuffer = false;
        endSpeaking(item.messageId);
        // The recursive call below re-highlights the next sentence in the
        // same tick, so consecutive sentences of one message never paint a
        // flicker across the inter-sentence gap.
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

/**
 * Play a decoded AudioBuffer on a fresh BufferSource — the single shared
 * Web Audio source-construction path. The TTS playback queues resolve a
 * promise when playback ends; chat.js' persisted-audio playback passes an
 * onEnd callback (to clear the speaking highlight) instead of duplicating
 * the source setup.
 *
 * @param {AudioBuffer} buffer - Decoded audio to play.
 * @param {Function} [onEnd] - Called when playback ends (source.onended),
 *     before the returned promise resolves.
 * @returns {Promise} Resolves when playback ends.
 */
function playAudioSource(buffer, onEnd) {
    return new Promise((resolve) => {
        const source = audioCtx.createBufferSource();
        source.buffer = buffer;
        source.connect(audioCtx.destination);
        source.onended = () => {
            // Natural end and a stop() both land here: the set cleanup is
            // idempotent, and the button state is re-derived from the set.
            activeAudioSources.delete(source);
            updateStopButtonUI();
            if (onEnd) onEnd();
            resolve();
        };
        source.start();
        activeAudioSources.add(source);
        updateStopButtonUI();
    });
}

