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
        // Same verdict path the streaming queue uses, and the same rule: the
        // SERVER says whether this is a permanent statement about this
        // persona's voice (no transcript while the connected engine demands
        // one, an unclonable clip), and the reason belongs under the bubble
        // where the user can read it — not in a console nobody opens. Nothing
        // to drop here: this queue holds one item per reply, but the verdict
        // set and the note are keyed by item.messageId, which enqueueTTS
        // already captured.
        if (isVoiceVerdict(err)) {
            handleUnvoiceableVerdict(item, err);
            console.warn("TTS playback refused:", err.status, err.refusal, err.detail);
        } else {
            // Loud on purpose. Worded neutrally on purpose too: this catch also
            // sees a bad base64 payload, a dead AudioContext, or a DOM throw
            // from the highlight, so it may not claim "not a voice verdict"
            // about an error that never reached /api/tts. What it DOES know is
            // the one thing worth knowing — no verdict was recorded, so the
            // next reply asks again — and the error object carries whatever
            // status/detail/refusal the request did get.
            //
            // (It deliberately does NOT branch on err.status to word this
            // better. The suite forbids reading a status in this file at all,
            // and "but this one is only for the log" is how that rule starts
            // having exceptions.)
            console.error("TTS playback failed with no voice verdict recorded;",
                          "the next reply will ask again:", err && err.detail, err);
        }
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
    const messageId = currentAssistantMessageId;
    // The engine has already refused THIS message's voice: its verdict is
    // about the engine/persona pairing, not about this sentence, so asking
    // again buys the same answer at the price of a serialized synthesis slot.
    // Messages with no verdict of their own are untouched — a late 422 from an
    // earlier responder must not mute the one currently streaming.
    if (isKnownMessageId(messageId) && ttsBrokenMessageIds.has(messageId)) {
        console.warn("streaming TTS skipped (voice already refused for this message):",
                     previewForLog(text));
        return;
    }
    // No round trip for a chunk with no letters in it at all — see isSpeakableText.
    if (!isSpeakableText(text)) {
        console.warn("streaming TTS skipped (no letters to voice):", previewForLog(text));
        return;
    }
    ttsRequestQueue.push({ personaName, text, messageId });
    processTTSRequests();
}

/**
 * Does this chunk contain anything at all that a TTS engine could voice?
 *
 * The streaming splitter breaks on "." AND on every newline, so a fragment of
 * reply scaffolding arrives as its own whole request: a closing code fence, a
 * `|---|` table rule, a bare "...", a timestamp line, an emoji-only line.
 * None of those contains a single letter, and no engine in the tts-serve
 * catalogue speaks them, so the round trip is worthless — drop the chunk with
 * a console line instead of paying for it.
 *
 * This test is about FORM, never about SCRIPT (issue #169). It used to hard-
 * code LuxTTS' tokenizer (`[A-Za-z\u00C0-\u024F\u4E00-\u9FFF]`) as an app-wide
 * rule, and one engine's script coverage is not every engine's spec: a persona
 * answering in Cyrillic, kana, Hangul, Greek or Arabic had every sentence
 * dropped client-side — zero `/api/tts` requests, no `.tts-note`, no 422, only
 * a console line — on engines that advertise exactly those languages
 * (server_qwen3TTS ships ru/ja/ko, server_indexTTS ships ar, server_ming3TTS
 * ships ja/ko). Which scripts a voice can carry is a property of the ENGINE,
 * and the engine is the authority here: it answers its own coverage with a 422
 * sentence, and handleUnvoiceableVerdict turns that into the user-facing note.
 *
 * Bias towards "speakable", same as ever: a chunk this calls speakable costs
 * at most the request we would have made anyway; a chunk it wrongly calls
 * unspeakable silently deletes words from the user's audio.
 */
function isSpeakableText(text) {
    return /\p{L}/u.test(String(text ?? ""));
}

/** A chunk preview for the console: long LLM replies are not log material. */
function previewForLog(text) {
    const oneLine = String(text ?? "").replace(/\s+/g, " ").trim();
    return oneLine.length > 60 ? `${oneLine.slice(0, 60)}…` : oneLine;
}

/**
 * Forget the resilience verdicts of the previous reply.
 *
 * Called on every "start" event, in BOTH modes (the call sits outside the
 * `ttsStreaming` guard in chat.js): a new assistant reply is a fresh chance,
 * so one persona's broken voice never mutes the session. Non-streaming has to
 * be included because `processAudioQueue` feeds this same set and nothing else
 * ever drains it. Clearing the whole set is safe because verdicts are only
 * ever useful for the reply they were earned by: a later reply of the same
 * persona re-asks once and, if the voice is still broken, re-earns the
 * verdict for its own message ID.
 */
function resetTtsReplyState() {
    ttsBrokenMessageIds.clear();
}

/**
 * Apply one engine "this voice cannot be had" verdict to one message.
 *
 * Records the verdict against the message that ASKED, drops that message's
 * queued sentences, and shows the engine's reason on that message's row. All
 * three are keyed to the asking message on purpose: the fetch that failed can
 * resolve long after the LLM stream moved on to the next responder, so the
 * "current" row/ID at verdict time belongs to somebody else's reply.
 *
 * The WHOLE message pays for one refusal. For a reply in a language the
 * connected engine has no voice for, that is the point. For a mixed-script
 * reply it is a real cost (the refusal was about one sentence's script, and
 * the sibling sentences never got to argue), and it is accepted rather than
 * worked around: any client-side rule fine enough to tell those two cases
 * apart is a script table again, which is the bug #169 was. Pinned by
 * tests/test_tts_resilience.js — see the note on ttsBrokenMessageIds.
 *
 * An item with NO message ID condemns nothing at all (issue #174). The ID is
 * stamped from the "start" event, so it is missing only when the server never
 * sent one (an older backend, or a token that arrived before the ID was
 * adopted) — and then it is missing for EVERY responder in the turn, so it
 * identifies no message in particular. Judging `null` would be guessing at an
 * owner, and guessing wrong mutes an innocent responder; asking the next
 * sentence of an unidentified reply and hearing the same 422 again is the
 * cheaper mistake. Both callers still log the refusal after this returns, so
 * the verdict is never silently swallowed.
 */
function handleUnvoiceableVerdict(item, err) {
    if (!isKnownMessageId(item.messageId)) {
        console.warn("TTS voice verdict left unattributed (no message ID on the asking item):",
                     previewForLog(item.text));
        return;
    }
    ttsBrokenMessageIds.add(item.messageId);
    dropQueuedSentencesOfMessage(item.messageId);
    noteTtsUnavailableForReply(item.messageId, err.detail || err.message);
}

/**
 * Does this queued request belong to the message identified by `messageId`?
 *
 * Both sides must be known IDs before they are allowed to be equal — the one
 * comparison of message IDs in the whole frontend, and the only place that may
 * write `===` between two of them (grep guard in tests/test_tts_resilience.js).
 * Two items stamped `null` compare equal, and the question "whose sentences are
 * these?" answered about an unidentified message must be *nobody's*, not
 * "everybody unidentified" — that equality was issue #174.
 */
function belongsToMessage(item, messageId) {
    return isKnownMessageId(messageId)
        && isKnownMessageId(item && item.messageId)
        && item.messageId === messageId;
}

/**
 * Drop the queued sentences of one message; every other message's sentences
 * stay queued (this verdict says nothing about them). Splice backwards so the
 * remaining indexes stay valid as items are removed.
 *
 * No separate empty-ID guard: belongsToMessage() already refuses to identify a
 * message by an unknown ID, so an unidentified caller drops nothing.
 */
function dropQueuedSentencesOfMessage(messageId) {
    for (let i = ttsRequestQueue.length - 1; i >= 0; i -= 1) {
        if (belongsToMessage(ttsRequestQueue[i], messageId)) ttsRequestQueue.splice(i, 1);
    }
}

/**
 * One muted line under the reply whose audio died. The alternative is a
 * silently half-spoken reply plus a 422 in a console nobody opens; the
 * engine's own words ("...speech other than English / Chinese cannot be
 * cloned") are actionable.
 *
 * Best-effort by design: a missing row (deleted mid-playback, room switched
 * away) or a stub DOM without createElement is not an error worth throwing
 * inside a fetch chain. Message IDs are server-issued UUIDs, so they are safe
 * inside the attribute selector unescaped.
 */
function noteTtsUnavailableForReply(messageId, reason) {
    if (!isKnownMessageId(messageId) || typeof document === "undefined" || !document.createElement) return;
    const row = typeof messagesEl !== "undefined" && messagesEl
        ? messagesEl.querySelector(`.message-row[data-message-id="${messageId}"]`)
        : null;
    if (!row || !row.appendChild) return;
    // Anchor on the row's .bubble-content column, not on the row itself: the
    // row is a flex ROW (avatar | content), so a direct child renders beside
    // the bubble as a squeezed third column, not under it. This is also where
    // .message-actions and .message-audio live.
    const content = row.querySelector(".bubble-content") || row;
    const note = document.createElement("div");
    note.className = "tts-note";
    note.textContent = `Voice unavailable for this reply: ${reason}`;
    // Keep the line under the bubble text and above the copy/delete row: the
    // same anchor getAudioContainer() uses, because insertBefore needs a direct
    // child of the container and .message-actions is one.
    const anchor = content.querySelector(".message-actions")
        || content.querySelector(".message-delete-btn");
    if (anchor) {
        content.insertBefore(note, anchor);
    } else {
        content.appendChild(note);
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
            // Carry the message ID into the playback queue so the row can be
            // brightened while its sentences play (see processAudioBufferQueue).
            audioBufferQueue.push({ buffer: audioBuffer, messageId: item.messageId });
            processAudioBufferQueue();
        }
    } catch (err) {
        // The server decides whether this failure is a verdict on the voice;
        // see isVoiceVerdict. When it is one, every remaining sentence of this
        // message would hear the same answer, so drop them and show its reason.
        // When it is not — an engine hiccup, or a request the backend never
        // accepted — the next sentence is a DIFFERENT request and gets asked.
        if (isVoiceVerdict(err)) {
            handleUnvoiceableVerdict(item, err);
            console.warn("TTS streaming fetch refused:", err.status, err.refusal, err.detail);
        } else {
            // Loud on purpose: a synthesis failure with no refusal class is
            // either an engine hiccup or a malformed request from this very
            // file. The second one used to hide inside the breaker above.
            console.error("TTS streaming fetch failed (not a voice verdict):",
                          err.status, err.detail);
        }
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
 * Build the error a failed /api/tts carries: the refusal class the server
 * decided on (`refusal`, the ONLY thing the pipeline acts on), the sentence it
 * wrote for the user (`detail`), and the HTTP status for the console.
 */
function ttsRequestError(status, detail, refusal) {
    const err = new Error(`TTS request failed: HTTP ${status}`);
    err.status = status;
    err.detail = detail;
    err.refusal = refusal || null;
    return err;
}

/**
 * Read a failed /api/tts response into the two facts it carries.
 *
 * `refusal` is the server's own classification — "is this a permanent verdict
 * on this persona's voice?" — and it is the ONLY reason the client gets to
 * decide that (see isVoiceVerdict). Null when the body does not carry it, and
 * a body WITHOUT the key is provably not this app's answer: the router stamps
 * every one of its failure bodies (app/routers/tts.py::_failure_body), so a
 * missing key means FastAPI rejected the request model, or a proxy answered
 * instead of us. Neither says anything about a voice.
 *
 * `detail` is verbatim when it is a sentence; anything else (a validation list,
 * a foreign envelope) is serialized — that form is LOG material, never bubble
 * material, and only a refused `refusal` ever puts it on screen.
 */
async function readTtsErrorBody(resp) {
    let body;
    try {
        body = await resp.json();
    } catch (err) {
        // Non-JSON body (a proxy's HTML error page, an empty 502).
        return { detail: `HTTP ${resp.status}`, refusal: null };
    }
    const refusal = body && typeof body.refusal === "string" && body.refusal
        ? body.refusal
        : null;
    const detail = body && typeof body.detail === "string" && body.detail
        ? body.detail
        : JSON.stringify(body);
    return { detail, refusal };
}

/**
 * Did the SERVER classify this failure as a permanent verdict on the voice?
 *
 * The browser no longer reads HTTP numbers to decide that, deliberately
 * (issue #173). `/api/tts` answers 422 for two unrelated things: the verdicts
 * worth muting a reply over, and FastAPI rejecting the request BODY
 * (`TTSRequest.text` is min_length=1) before the handler ever ran. Classifying
 * both as "this persona cannot speak" muted replies that had a perfectly good
 * voice — the next sentence is a different, valid request — and printed a
 * serialized validation list under the bubble accusing a voice that was fine.
 * The server already knows which is which; it now says so.
 *
 * A refusal-less failure is NOT silence-by-another-name either: the remaining
 * sentences stay queued and keep asking, and the caller logs it as the bug it
 * probably is.
 */
function isVoiceVerdict(err) {
    return Boolean(err && typeof err.refusal === "string" && err.refusal);
}

/**
 * Fetch TTS audio from the server and persist it to disk.
 *
 * @param {string} personaName - Which persona to synthesize for.
 * @param {string} text - Text to synthesize.
 * @param {string|null} messageId - The message ID this audio belongs to.
 *   Stamped at enqueue time from the "start" event, so it is correct
 *   regardless of when this fetch resolves.
 * @throws {Error} with `.status`, `.detail` and the server's `.refusal` class
 *   on a non-2xx response, so the pipeline can tell a voice verdict from a
 *   hiccup without reading the status (isVoiceVerdict).
 */
async function fetchTTS(personaName, text, messageId) {
    const resp = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, persona_name: personaName }),
    });

    if (!resp.ok) {
        const { detail, refusal } = await readTtsErrorBody(resp);
        console.warn("TTS request failed:", resp.status, refusal || "no-refusal", detail);
        throw ttsRequestError(resp.status, detail, refusal);
    }

    const data = await resp.json();
    if (!data.audio_base64) return null;

    // Persist the audio against the message it was enqueued for.
    if (isKnownMessageId(messageId)) {
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

