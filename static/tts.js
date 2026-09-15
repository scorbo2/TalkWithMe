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
        await new Promise(resolve => setTimeout(resolve, 250)); // inter-sentence gap
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

/* ==========================================================================
   Play All — replay entire conversation audio
   ========================================================================== */

let playAllAbort = null;  // AbortController for stopping playback
let playAllActive = false;  // Is playback currently running?

function updatePlayAllUI() {
    const playBtn = document.getElementById("btn-play-all");
    const stopBtn = document.getElementById("btn-stop-all");
    if (playBtn && stopBtn) {
        playBtn.style.display = playAllActive ? "none" : "inline-block";
        stopBtn.style.display = playAllActive ? "inline-block" : "none";
    }
}

async function playAllAudio(roomName) {
    // Stop any current playback — close old AudioContext to kill buffered audio
    if (playAllAbort) {
        playAllAbort.abort();
    }
    if (audioCtx) {
        try { audioCtx.close(); } catch (_) {}
        audioCtx = null;
    }
    const myAbort = new AbortController();
    playAllAbort = myAbort;
    const signal = myAbort.signal;

    // Pause TTS streaming while playing all
    const wasTtsEnabled = ttsEnabled;
    ttsEnabled = false;
    playAllActive = true;
    updatePlayAllUI();

    try {
        const roomName = window.currentChatRoom || currentChatRoom;
        const resp = await fetch(`/api/persist/audio/${encodeURIComponent(roomName)}/all`);
        if (!resp.ok) {
            console.warn("Failed to load room audio list:", resp.status);
            return;
        }
        const items = await resp.json();
        if (!items.length) {
            console.log("No audio files in this room");
            return;
        }

        // Initialize AudioContext if needed
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext())();
        }

        for (const item of items) {
            if (signal.aborted) break;

            if (!item.has_audio) continue;
            try {
                const audioResp = await fetch(
                    `/api/persist/audio/${encodeURIComponent(roomName)}/${encodeURIComponent(item.filename)}`
                );
                if (!audioResp.ok) continue;

                const blob = await audioResp.blob();
                const arrayBuffer = await blob.arrayBuffer();
                const decoded = await audioCtx.decodeAudioData(arrayBuffer);

                if (signal.aborted) break;
                await playAudio(decoded);
                await new Promise(resolve => setTimeout(resolve, 200)); // gap between messages
            } catch (err) {
                if (err.name === 'AbortError') break;
                console.warn("Play all: failed to play", item.filename, err);
            }
        }
    } catch (err) {
        console.warn("Play all failed:", err);
    } finally {
        // If a new shift+click already replaced us, don't touch its state
        if (playAllAbort === myAbort) {
            playAllAbort = null;
            playAllActive = false;
            updatePlayAllUI();
            ttsEnabled = wasTtsEnabled;
            clearMessageHighlight();
        }
    }
}

function stopAllPlayback() {
    if (playAllAbort) {
        playAllAbort.abort();
        playAllAbort = null;
    }
    if (audioCtx) {
        try { audioCtx.close(); } catch (_) {}
        audioCtx = null;
    }
    playAllActive = false;
    updatePlayAllUI();
    clearMessageHighlight();
}

function stopAllTTS() {
    // Clear the TTS request and audio queues
    ttsRequestQueue.length = 0;
    audioBufferQueue.length = 0;
    audioQueue.length = 0;
    isPlayingAudio = false;
    isFetchingTTS = false;
    isPlayingAudioBuffer = false;
    sentenceBuffer = "";
    // Stop any currently playing AudioContext source
    if (audioCtx) {
        audioCtx.close();
        audioCtx = null;
    }
}

async function playAllAudioFrom(roomName, startMessageId) {
    // Stop any current playback — close old AudioContext to kill buffered audio
    if (playAllAbort) {
        playAllAbort.abort();
    }
    if (audioCtx) {
        try { audioCtx.close(); } catch (_) {}
        audioCtx = null;
    }
    const myAbort = new AbortController();
    playAllAbort = myAbort;
    const signal = myAbort.signal;

    // Highlight immediately — before any async work
    highlightMessage(startMessageId);

    // Pause TTS streaming while playing all
    const wasTtsEnabled = ttsEnabled;
    ttsEnabled = false;
    playAllActive = true;
    updatePlayAllUI();

    try {
        const resp = await fetch(`/api/persist/audio/${encodeURIComponent(roomName)}/all`);
        if (!resp.ok) {
            console.warn("Failed to load room audio list:", resp.status);
            return;
        }
        const items = await resp.json();
        if (!items.length) {
            console.log("No audio files in this room");
            return;
        }

        // Find the starting index — start from the clicked message,
        // then find the next one that actually has audio
        let startIndex = 0;
        if (startMessageId) {
            const msgIndex = items.findIndex(item => item.message_id === startMessageId);
            if (msgIndex !== -1) {
                startIndex = items.findIndex((item, i) => i >= msgIndex && item.has_audio);
                if (startIndex === -1) {
                    // No audio after this message, play from beginning
                    startIndex = 0;
                }
            }
        }

        // Initialize AudioContext if needed
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext())();
        }

        for (let i = startIndex; i < items.length; i++) {
            if (signal.aborted) break;

            const item = items[i];
            if (!item.has_audio) continue;  // skip messages without TTS audio
            try {
                const audioResp = await fetch(
                    `/api/persist/audio/${encodeURIComponent(roomName)}/${encodeURIComponent(item.filename)}`
                );
                if (!audioResp.ok) continue;

                const blob = await audioResp.blob();
                const arrayBuffer = await blob.arrayBuffer();
                const decoded = await audioCtx.decodeAudioData(arrayBuffer);

                if (signal.aborted) break;
                await playAudio(decoded);
                await new Promise(resolve => setTimeout(resolve, 200));
            } catch (err) {
                if (err.name === 'AbortError') break;
                console.warn("Play all: failed to play", item.filename, err);
            }
        }
    } catch (err) {
        console.warn("Play all failed:", err);
    } finally {
        // If a new shift+click already replaced us, don't touch its state
        if (playAllAbort === myAbort) {
            playAllAbort = null;
            playAllActive = false;
            updatePlayAllUI();
            ttsEnabled = wasTtsEnabled;
            clearMessageHighlight();
        }
    }
}

function highlightMessage(messageId) {
    clearMessageHighlight();
    if (!messageId) return;
    const row = document.querySelector(`.message-row[data-message-id="${messageId}"]`);
    if (row) {
        row.classList.add("playback-start");
        row.scrollIntoView({ behavior: "smooth", block: "center" });
    }
}

function clearMessageHighlight() {
    document.querySelectorAll(".message-row.playback-start").forEach(row => {
        row.classList.remove("playback-start");
    });
}

