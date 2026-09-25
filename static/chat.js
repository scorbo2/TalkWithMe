/**
 * chat.js — Chat messaging: sending, SSE streaming, bubble rendering.
 *
 * Handles the full message lifecycle: user input → POST to server →
 * streaming SSE response → TTS enqueue → bubble rendering.
 * Also handles rendering persisted chat history from disk.
 */

/* ==========================================================================
   Empty state
   ========================================================================== */

function showEmptyState() {
    messagesEl.innerHTML = `
        <div class="empty-state">
            <div>
                <p>No messages yet</p>
                <p class="hint">Say hello to start a conversation!</p>
            </div>
        </div>
    `;
}

/* ==========================================================================
   Persona mention detection
   ========================================================================== */

/**
 * Detect if the user mentioned any persona from the current room by name.
 * Uses case-insensitive word-boundary matching to avoid partial matches
 * (e.g., "Sam" won't trigger "Samuel"). Returns the first matching persona
 * name, or null if none found.
 */
function detectMentionedPersona(text, roomPersonaNames) {
    for (const name of roomPersonaNames) {
        // Escape regex special characters in the name
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        // For multi-word names like "Dr. Smith", allow flexible whitespace
        const flexible = escaped.split(/\s+/).join("\\s+");
        const regex = new RegExp(`\\b${flexible}\\b`, "i");

        if (regex.test(text)) {
            return name;
        }
    }
    return null;
}

/* ==========================================================================
   Who answers
   ========================================================================== */

function getWhoAnswers() {
    const chosen = document.querySelector('input[name="who_answers"]:checked').value;
    if (chosen === "selected") {
        return selectedPersona || "router";
    }
    return chosen;
}

/* ==========================================================================
   Send message
   ========================================================================== */

async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || isStreaming) return;

    // If the current chat room has no personas, show an error instead of sending
    const roomPersonaNames = roomPersonas[currentChatRoom] || [];
    if (roomPersonaNames.length === 0) {
        appendErrorBubble("No one is here.");
        return;
    }

    // Auto-select a persona if the user mentioned one by name in their message.
    // This runs before getWhoAnswers() so the "Selected persona" radio is
    // already checked by the time we determine who should respond.
    // Feature can be disabled via settings.yaml: general.persona_name_mentions
    if (personaNameMentionsEnabled) {
        const mentioned = detectMentionedPersona(text, roomPersonaNames);
        if (mentioned) {
            selectedPersona = mentioned;
            highlightSelectedPersona();
            const selectedRadio = document.querySelector('input[name="who_answers"][value="selected"]');
            if (selectedRadio) {
                selectedRadio.checked = true;
                selectedRadio.dispatchEvent(new Event("change", { bubbles: true }));
            }
        }
    }

    // Clear empty state if present
    if (messagesEl.querySelector(".empty-state")) {
        messagesEl.innerHTML = "";
    }

    // Generate a UUID for this user message (used for audio association).
    // If STT already generated one (for audio upload), reuse it.
    if (!pendingUserMessageId) {
        pendingUserMessageId = crypto.randomUUID();
    }

    // Append user bubble with the message ID
    appendUserBubble(text, pendingUserMessageId);
    inputEl.value = "";
    inputEl.style.height = "auto";

    isStreaming = true;
    sendBtn.disabled = true;

    // Create a placeholder assistant bubble for the first responder
    const who = getWhoAnswers();
    currentAssistantRow = createAssistantBubble(who);
    messagesEl.appendChild(currentAssistantRow);
    scrollToBottom();

    try {
        const resp = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                message: text,
                who_answers: who,
                chat_room: currentChatRoom,
                message_id: pendingUserMessageId,
            }),
        });

        if (!resp.ok || !resp.body) {
            handleSSEEvent({ type: "error", message: `Chat request failed (HTTP ${resp.status}).` });
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop(); // Keep incomplete line in buffer

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const json = line.slice(6);
                if (!json.trim()) continue;

                try {
                    const event = JSON.parse(json);
                    handleSSEEvent(event);
                } catch (e) {
                    console.warn("Failed to parse SSE event:", json, e);
                }
            }
        }
    } catch (err) {
        console.error("Chat error:", err);
        handleSSEEvent({ type: "error", message: "Connection failed. Is the LLM server running?" });
    } finally {
        isStreaming = false;
        sendBtn.disabled = false;
        pendingUserMessageId = null;
        inputEl.focus();
    }
}

/* ==========================================================================
   SSE event handling
   ========================================================================== */

function handleSSEEvent(event) {
    switch (event.type) {
        case "start": {
            // If currentAssistantRow already has content, this is a subsequent persona
            // reply — create a fresh bubble instead of reusing the existing one.
            // Tool chips count as content: a persona whose agentic loop produced
            // only tool calls (no text) still "spent" its row, and reusing it
            // would merge those chips into the next persona's bubble.
            const existingBubble = currentAssistantRow && currentAssistantRow.querySelector(".bubble");
            const existingRowSpent = !!(existingBubble && (
                existingBubble.textContent.trim() || currentAssistantRow.querySelector(".tool-chips")
            ));
            if (existingRowSpent) {
                currentAssistantRow = createAssistantBubble(event.persona);
                messagesEl.appendChild(currentAssistantRow);
                scrollToBottom();
            }

            // Adopt the server-issued message ID for this response. Every TTS item
            // enqueued from this point on carries this ID explicitly, so audio is
            // associated with the correct message regardless of when each fetch
            // resolves. (The old approach — clear the global here and backfill on
            // "done" — misattributed audio across turns whenever a fetch resolved
            // before "done", which is the normal timing in streaming mode.)
            currentAssistantMessageId = event.message_id || null;
            if (currentAssistantRow && event.message_id) {
                currentAssistantRow.dataset.messageId = event.message_id;
            }

            // Update the bubble with the actual persona name
            const persona = personas.find(p => p.name === event.persona);
            setupAssistantBubble(currentAssistantRow, persona || { name: event.persona, avatar_color: "#888" });

            // Visual confirmation: update selected persona in sidebar immediately.
            // Only on the FIRST start event — subsequent persona replies should not
            // hijack the user's selection in the sidebar.
            if (!existingRowSpent && event.persona && event.persona !== selectedPersona) {
                selectedPersona = event.persona;
                highlightSelectedPersona();
            }

            // Streaming TTS: track persona and reset sentence accumulator
            if (ttsStreaming) {
                currentStreamingPersona = event.persona;
                sentenceBuffer = "";
            }
            break;
        }
        case "token": {
            const bubble = currentAssistantRow && currentAssistantRow.querySelector(".bubble");
            if (bubble) {
                bubble.textContent += event.token;
                scrollToBottom();
            }

            // Streaming TTS: accumulate tokens and queue complete sentences immediately
            if (ttsEnabled && ttsStreaming && currentStreamingPersona) {
                const persona = personas.find(p => p.name === currentStreamingPersona);
                if (persona && persona.tts_capable) {
                    accumulateForTTS(event.token, currentStreamingPersona);
                }
            }
            break;
        }
        case "done": {
            // The server persists the reply right before emitting "done",
            // so the row is on disk now — the delete button is safe to use.
            enableDeleteButtonOnRow(currentAssistantRow);

            // The message ID was already adopted on "start" and stamped onto
            // every TTS item at enqueue time, so there is nothing to backfill.
            if (ttsEnabled && event.text) {
                const persona = personas.find(p => p.name === event.persona);
                if (persona && persona.tts_capable) {
                    if (ttsStreaming) {
                        // Flush any remaining partial sentence from the buffer
                        const remaining = sentenceBuffer.trim();
                        if (remaining) {
                            enqueueStreamingTTS(event.persona, remaining);
                        }
                        sentenceBuffer = "";
                        currentStreamingPersona = null;
                    } else {
                        // Non-streaming: enqueue full text at once
                        enqueueTTS(event.persona, event.text);
                    }
                }
            }
            break;
        }
        case "tool_call": {
            addToolCallChip(event);
            break;
        }
        case "error": {
            const bubble = currentAssistantRow && currentAssistantRow.querySelector(".bubble");
            if (bubble) {
                bubble.textContent += `\n\n[Error: ${event.message}]`;
            }
            // The stream ended WITHOUT "done" — on the error path the server
            // never persists the reply, so this row (if it has a message ID)
            // exists only in the DOM. Enable the delete button anyway: a
            // click will 404 on the server and the handler removes the row
            // from view, so display and disk end up in agreement.
            enableDeleteButtonOnRow(currentAssistantRow);
            break;
        }
        case "complete": {
            // Final signal — nothing to do. In-flight audio fetches already
            // carry their own message IDs, and currentAssistantMessageId is
            // simply overwritten by the next "start" event.
            break;
        }
    }
}

/* ==========================================================================
   Bubble creation
   ========================================================================== */

function appendUserBubble(text, messageId) {
    const row = document.createElement("div");
    row.className = "message-row user";
    if (messageId) {
        row.dataset.messageId = messageId;
    }

    // Wrapper keeps bubble + audio stacked vertically (row-reverse would
    // otherwise place audio to the left of the bubble).
    const wrapper = document.createElement("div");
    wrapper.className = "user-message-content";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;

    wrapper.appendChild(bubble);
    row.appendChild(wrapper);
    addDeleteButtonToRow(row, messageId);
    messagesEl.appendChild(row);
    scrollToBottom();
}

function createAssistantBubble(whoHint) {
    const row = document.createElement("div");
    row.className = "message-row assistant";

    // Avatar placeholder
    const avatar = document.createElement("div");
    avatar.className = "bubble-avatar";
    avatar.style.backgroundColor = "#555";
    avatar.textContent = "?";

    const content = document.createElement("div");
    content.className = "bubble-content";

    const nameEl = document.createElement("div");
    nameEl.className = "bubble-name";
    nameEl.textContent = "...";

    // Loading indicator
    const loading = document.createElement("div");
    loading.className = "bubble loading-bubble";
    loading.innerHTML = "<span></span><span></span><span></span>";

    content.appendChild(nameEl);
    content.appendChild(loading);
    row.appendChild(avatar);
    row.appendChild(content);

    // Delete button from birth, disabled until the reply is persisted:
    // the server writes the row only at the end of the stream ("done"),
    // so a live button mid-stream would have nothing to delete yet.
    // The message ID is not known until the "start" event; the click
    // handler falls back to row.dataset.messageId at click time.
    addDeleteButtonToRow(row, null, true);

    return row;
}

function setupAssistantBubble(row, persona) {
    const avatar = row.querySelector(".bubble-avatar");
    const nameEl = row.querySelector(".bubble-name");
    const loading = row.querySelector(".loading-bubble");

    // Set avatar
    avatar.style.backgroundColor = persona.avatar_color;
    avatar.textContent = persona.name.charAt(0).toUpperCase();

    // If persona has an avatar image, load it
    const p = personas.find(pp => pp.name === persona.name);
    if (p && p.avatar_image) {
        const img = document.createElement("img");
        img.src = `/api/personas/${encodeURIComponent(p.name)}/avatar`;
        img.alt = p.name;
        img.onerror = () => {
            avatar.innerHTML = p.name.charAt(0).toUpperCase();
        };
        avatar.innerHTML = "";
        avatar.appendChild(img);
    }

    nameEl.textContent = persona.name;

    // Replace loading dots with actual bubble
    if (loading) {
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        bubble.textContent = "";
        loading.replaceWith(bubble);
    }
}

/**
 * Append a non-interactive chip to the active assistant message showing
 * that the persona invoked an MCP tool.
 *
 * The chip row lives in .bubble-content between the name and the bubble —
 * NOT inside the bubble — because streaming tokens append via
 * bubble.textContent, which would destroy any child elements within it.
 *
 * Events only arrive when general.show_tool_calls is enabled (the server
 * suppresses them otherwise), so no client-side gating is needed here.
 */
function addToolCallChip(event) {
    if (!currentAssistantRow) return;
    const content = currentAssistantRow.querySelector(".bubble-content");
    if (!content) return;

    let chipRow = content.querySelector(".tool-chips");
    if (!chipRow) {
        chipRow = document.createElement("div");
        chipRow.className = "tool-chips";
        const bubble = content.querySelector(".bubble");
        content.insertBefore(chipRow, bubble);
    }

    // The server computes this flag (tool error, unknown tool, or
    // unparseable/truncated arguments) — don't re-derive it from prose,
    // a legitimate result may well start with the words "Error: ".
    const failed = event.failed === true;
    const chip = document.createElement("span");
    chip.className = failed ? "tool-chip error" : "tool-chip";
    chip.textContent = `🔧 ${event.tool_name}`;

    // Display-only tooltip: what was called, with what, and what came back
    let argsStr;
    try {
        argsStr = JSON.stringify(event.arguments ?? {});
    } catch (_) {
        argsStr = "[unserializable arguments]";
    }
    if (argsStr.length > 300) argsStr = argsStr.slice(0, 300) + "…";
    let tooltip = `Arguments: ${argsStr}`;
    if (event.result) {
        const result = event.result.length > 300 ? event.result.slice(0, 300) + "…" : event.result;
        tooltip += `\nResult: ${result}`;
    }
    chip.title = tooltip;

    chipRow.appendChild(chip);
    scrollToBottom();
}

function appendErrorBubble(text) {
    const row = document.createElement("div");
    row.className = "message-row assistant";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.style.color = "#ff6b6b";
    bubble.textContent = text;

    row.appendChild(bubble);
    messagesEl.appendChild(row);
    scrollToBottom();
}

/* ==========================================================================
    Message deletion
    ========================================================================== */

/**
 * Attach a delete button to a message row.
 *
 * The button lands in the row's content container (the
 * .user-message-content wrapper for user rows, .bubble-content for persona
 * rows), as the last child — below the bubble and any audio buttons.
 *
 * @param {HTMLElement} row - The .message-row element.
 * @param {string|null} messageId - The row's message ID, if known. Live
 *     assistant rows are created before the "start" event issues one, so
 *     this may be null; the click handler then falls back to the
 *     row's data-message-id attribute (read at click time).
 * @param {boolean} [disabled=false] - Start disabled (live assistant rows:
 *     the reply is not persisted until the stream finishes).
 */
function addDeleteButtonToRow(row, messageId, disabled = false) {
    const container = row.querySelector(".user-message-content")
        || row.querySelector(".bubble-content");
    if (!container) return;

    const btn = document.createElement("button");
    btn.className = "message-delete-btn";
    btn.textContent = "\u{1F5D1}"; // trash icon
    btn.title = "Delete message";
    btn.setAttribute("aria-label", "Delete message");
    btn.disabled = disabled;
    btn.addEventListener("click", () => deleteMessageFromChat(row, messageId));
    container.appendChild(btn);
}

/**
 * Enable the delete button on a row, if it has one.
 * Called when a persona reply's stream has settled (the "done" event, or
 * the "error" event for streams that died before persisting).
 */
function enableDeleteButtonOnRow(row) {
    if (!row) return;
    const btn = row.querySelector(".message-delete-btn");
    if (btn) {
        btn.disabled = false;
    }
}

/**
 * Delete one message: ask the server to remove the persisted row and its
 * audio, then remove the row from the DOM on a definitive answer.
 *
 * 200 — deleted on disk, remove from the DOM.
 * 404 — the row was never persisted (e.g. a reply whose stream errored
 *       before completion, or a user row from a failed request): there is
 *       nothing on disk to clean up, so remove from the DOM as well and
 *       warn; display and disk end up in agreement.
 * anything else — keep the row (the user can retry) and warn.
 *
 * No confirmation dialog: single-user local app, worst case is one
 * recoverable message.
 */
async function deleteMessageFromChat(row, messageId) {
    const btn = row.querySelector(".message-delete-btn");
    const rowMessageId = messageId || row.dataset.messageId;

    // No message ID (e.g. a pre-"start" error placeholder): nothing on disk
    // could ever reference this row, so it is a display-only cleanup.
    if (!rowMessageId) {
        console.warn("Delete requested for a row without a message ID; removing from view only.");
        row.remove();
        return;
    }

    // Prevent a double-click from firing a second delete while in flight.
    if (btn) {
        btn.disabled = true;
    }

    try {
        const resp = await fetch(
            `/api/persist/message/${encodeURIComponent(currentChatRoom)}/${encodeURIComponent(rowMessageId)}`,
            { method: "DELETE" },
        );
        if (resp.status === 200 || resp.status === 404) {
            if (resp.status === 404) {
                console.warn(
                    `Delete: message ${rowMessageId} is not persisted in room ` +
                    `"${currentChatRoom}"; removing from view only.`,
                );
            }
            row.remove();
        } else {
            console.warn(
                `Delete failed (HTTP ${resp.status}) for message ${rowMessageId} in ` +
                `room "${currentChatRoom}"; the message remains, you can retry.`,
            );
            if (btn) {
                btn.disabled = false;
            }
        }
    } catch (err) {
        console.error("Delete request failed:", err);
        if (btn) {
            btn.disabled = false;
        }
    }
}

/* ==========================================================================
    Speaking highlight
    ========================================================================== */

// Message IDs with audio actively playing, mapped to the number of audio
// sources currently playing that message. A count rather than a boolean:
// two sources for the same message can legitimately overlap (a double-
// clicked play button, or a manual play while the message's live TTS is
// still finishing), and the first source ending must not un-highlight the
// row while the second is still audible.
const speakingMessageIds = new Map(); // messageId -> active source count

/**
 * Brighten the message row while its audio is playing.
 *
 * The row is located by the message ID stamped on it in the "start" event
 * (live rows) or on history load (persisted rows). A no-op when the ID is
 * missing or the row is gone (deleted, or the room was switched away
 * mid-playback — Web Audio keeps playing either way; this app has no stop
 * mechanism, so the highlight simply has nowhere to land).
 *
 * @param {string|null} messageId - The message ID whose row to brighten.
 * @param {boolean} on - true while audio plays, false when it stops.
 */
function setSpeakingHighlight(messageId, on) {
    if (!messageId) return;
    const row = messagesEl.querySelector(`.message-row[data-message-id="${messageId}"]`);
    if (row) {
        row.classList.toggle("speaking", on);
    }
}

/** Mark one of a message's audio sources as starting playback. */
function beginSpeaking(messageId) {
    if (!messageId) return;
    const count = (speakingMessageIds.get(messageId) || 0) + 1;
    speakingMessageIds.set(messageId, count);
    if (count === 1) {
        setSpeakingHighlight(messageId, true);
    }
}

/** Mark one of a message's audio sources as finished. */
function endSpeaking(messageId) {
    if (!messageId) return;
    const count = (speakingMessageIds.get(messageId) || 0) - 1;
    if (count <= 0) {
        speakingMessageIds.delete(messageId);
        setSpeakingHighlight(messageId, false);
    } else {
        speakingMessageIds.set(messageId, count);
    }
}

/* ==========================================================================
    Persisted history rendering
    ========================================================================== */

/**
 * Render persisted chat history into the message panel.
 * Called when loading a room's history from disk.
 */
function renderPersistedHistory(messages, roomName) {
    messagesEl.innerHTML = "";

    if (!messages || messages.length === 0) {
        showEmptyState();
        return;
    }

    for (const msg of messages) {
        if (msg.sender === "USER") {
            appendPersistedUserBubble(msg, roomName);
        } else {
            appendPersistedAssistantBubble(msg, roomName);
        }
    }

    scrollToBottom();
}

function appendPersistedUserBubble(msg, roomName) {
    const row = document.createElement("div");
    row.className = "message-row user";
    if (msg.id) {
        row.dataset.messageId = msg.id;
    }

    // Wrapper keeps bubble + audio stacked vertically.
    const wrapper = document.createElement("div");
    wrapper.className = "user-message-content";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = msg.text;

    wrapper.appendChild(bubble);

    // Add audio playback buttons if this message has audio files
    if (msg.audio && msg.audio.length > 0) {
        const audioContainer = document.createElement("div");
        audioContainer.className = "message-audio";
        for (const filename of msg.audio) {
            const playBtn = document.createElement("button");
            playBtn.className = "audio-play-btn";
            playBtn.innerHTML = "\u{1F501}"; // play icon
            playBtn.title = "Play audio";
            playBtn.addEventListener("click", () => playPersistedAudio(roomName, filename, row));
            audioContainer.appendChild(playBtn);
        }
        wrapper.appendChild(audioContainer);
    }

    row.appendChild(wrapper);
    addDeleteButtonToRow(row, msg.id);
    messagesEl.appendChild(row);
}

function appendPersistedAssistantBubble(msg, roomName) {
    const row = document.createElement("div");
    row.className = "message-row assistant";
    if (msg.id) {
        row.dataset.messageId = msg.id;
    }

    // Find persona info for avatar
    const persona = personas.find(p => p.name === msg.sender);
    const personaData = persona || { name: msg.sender, avatar_color: "#888" };

    // Avatar
    const avatar = document.createElement("div");
    avatar.className = "bubble-avatar";
    avatar.style.backgroundColor = personaData.avatar_color;

    if (persona && persona.avatar_image) {
        const img = document.createElement("img");
        img.src = `/api/personas/${encodeURIComponent(persona.name)}/avatar`;
        img.alt = persona.name;
        img.onerror = () => {
            avatar.innerHTML = persona.name.charAt(0).toUpperCase();
        };
        avatar.appendChild(img);
    } else {
        avatar.textContent = personaData.name.charAt(0).toUpperCase();
    }

    const content = document.createElement("div");
    content.className = "bubble-content";

    const nameEl = document.createElement("div");
    nameEl.className = "bubble-name";
    nameEl.textContent = personaData.name;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = msg.text;

    // Add audio playback buttons if this message has audio files
    if (msg.audio && msg.audio.length > 0) {
        const audioContainer = document.createElement("div");
        audioContainer.className = "message-audio";
        for (const filename of msg.audio) {
            const playBtn = document.createElement("button");
            playBtn.className = "audio-play-btn";
            playBtn.innerHTML = "\u{1F501}"; // 🔁 play icon
            playBtn.title = "Play audio";
            playBtn.addEventListener("click", () => playPersistedAudio(roomName, filename, row));
            audioContainer.appendChild(playBtn);
        }
        content.appendChild(nameEl);
        content.appendChild(bubble);
        content.appendChild(audioContainer);
    } else {
        content.appendChild(nameEl);
        content.appendChild(bubble);
    }

    row.appendChild(avatar);
    row.appendChild(content);
    addDeleteButtonToRow(row, msg.id);
    messagesEl.appendChild(row);
}

/**
 * Play a persisted audio file using Web Audio API.
 *
 * @param {string} roomName - The chat room the file belongs to.
 * @param {string} filename - The persisted audio filename.
 * @param {HTMLElement} [row] - The message row whose play button was
 *     clicked; when given, the row is brightened for the duration of
 *     playback (see setSpeakingHighlight).
 */
async function playPersistedAudio(roomName, filename, row) {
    const url = getAudioUrl(roomName, filename);
    const messageId = row ? row.dataset.messageId : null;
    let startedSpeaking = false;
    try {
        const resp = await fetch(url);
        if (!resp.ok) {
            console.warn(`Failed to fetch audio: HTTP ${resp.status}`);
            return;
        }
        const arrayBuffer = await resp.arrayBuffer();

        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }

        const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);

        if (messageId) {
            beginSpeaking(messageId);
            startedSpeaking = true;
        }
        // playAudioSource() is the shared source-construction path (tts.js):
        // onEnd clears the highlight when playback finishes. If the promise
        // rejects instead, onended never fires and the catch below does it.
        await playAudioSource(audioBuffer, messageId ? () => endSpeaking(messageId) : undefined);
    } catch (err) {
        // A failure after beginSpeaking() must still clear the highlight —
        // but only then: an earlier failure (fetch/decode) never began one,
        // and an endSpeaking() without a matching beginSpeaking() would
        // underflow a concurrent source's refcount for the same message.
        if (startedSpeaking) {
            endSpeaking(messageId);
        }
        console.error("Failed to play persisted audio:", err);
    }
}

/**
 * Inject an audio playback button into a live assistant bubble.
 * Called from tts.js after each audio file is persisted.
 *
 * @param {string} messageId - The message ID to locate the bubble by.
 * @param {string} filename - The persisted audio filename.
 */
function addAudioButtonToAssistantMessage(messageId, filename) {
    const row = messagesEl.querySelector(`.message-row.assistant[data-message-id="${messageId}"]`);
    if (!row) {
        console.warn("addAudioButtonToAssistantMessage: no bubble found for message", messageId);
        return;
    }

    const content = row.querySelector(".bubble-content");
    if (!content) return;

    // Lazily create the audio container on first button. Insert it before
    // the delete button (if present) so the delete button stays the last
    // child of .bubble-content.
    let audioContainer = content.querySelector(".message-audio");
    if (!audioContainer) {
        audioContainer = document.createElement("div");
        audioContainer.className = "message-audio";
        const deleteBtn = content.querySelector(".message-delete-btn");
        if (deleteBtn) {
            content.insertBefore(audioContainer, deleteBtn);
        } else {
            content.appendChild(audioContainer);
        }
    }

    const playBtn = document.createElement("button");
    playBtn.className = "audio-play-btn";
    playBtn.innerHTML = "\u{1F501}"; // play icon
    playBtn.title = "Play audio";
    playBtn.addEventListener("click", () => playPersistedAudio(currentChatRoom, filename, row));
    audioContainer.appendChild(playBtn);
}

/**
 * Inject an audio playback button into a live user bubble.
 * Called from stt.js after the recorded audio is persisted.
 * Uses a retry mechanism in case the bubble isn't in the DOM yet.
 *
 * @param {string} messageId - The message ID to locate the bubble by.
 * @param {string} filename - The persisted audio filename.
 */
function addAudioButtonToUserMessage(messageId, filename, retries = 3) {
    const row = messagesEl.querySelector(`.message-row.user[data-message-id="${messageId}"]`);
    if (!row) {
        if (retries > 0) {
            // Bubble not in DOM yet — retry after a short delay.
            setTimeout(() => addAudioButtonToUserMessage(messageId, filename, retries - 1), 150);
        }
        return;
    }

    // User bubbles use a .user-message-content wrapper for proper stacking.
    const wrapper = row.querySelector(".user-message-content");
    if (!wrapper) return;

    // Lazily create the audio container on first button. Insert it before
    // the delete button (if present) so the delete button stays the last
    // child of .user-message-content.
    let audioContainer = wrapper.querySelector(".message-audio");
    if (!audioContainer) {
        audioContainer = document.createElement("div");
        audioContainer.className = "message-audio";
        const deleteBtn = wrapper.querySelector(".message-delete-btn");
        if (deleteBtn) {
            wrapper.insertBefore(audioContainer, deleteBtn);
        } else {
            wrapper.appendChild(audioContainer);
        }
    }

    const playBtn = document.createElement("button");
    playBtn.className = "audio-play-btn";
    playBtn.innerHTML = "\u{1F501}"; // play icon
    playBtn.title = "Play audio";
    playBtn.addEventListener("click", () => playPersistedAudio(currentChatRoom, filename, row));
    audioContainer.appendChild(playBtn);
}
