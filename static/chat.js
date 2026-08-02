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

    // Create a placeholder assistant bubble for streaming
    const who = getWhoAnswers();
    const assistantRow = createAssistantBubble(who);
    messagesEl.appendChild(assistantRow);
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
            handleSSEEvent({ type: "error", message: `Chat request failed (HTTP ${resp.status}).` }, assistantRow);
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
                    handleSSEEvent(event, assistantRow);
                } catch (e) {
                    console.warn("Failed to parse SSE event:", json, e);
                }
            }
        }
    } catch (err) {
        console.error("Chat error:", err);
        handleSSEEvent({ type: "error", message: "Connection failed. Is the LLM server running?" }, assistantRow);
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

function handleSSEEvent(event, assistantRow) {
    switch (event.type) {
        case "start": {
            // A new response is starting — clear any leftover message ID from
            // the previous response so its late-arriving audio doesn't get
            // associated with this new message.
            currentAssistantMessageId = null;

            // Update the bubble with the actual persona name
            const persona = personas.find(p => p.name === event.persona);
            setupAssistantBubble(assistantRow, persona || { name: event.persona, avatar_color: "#888" });

            // Visual confirmation: update selected persona in sidebar immediately
            if (event.persona && event.persona !== selectedPersona) {
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
            const bubble = assistantRow.querySelector(".bubble");
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
            // Track the assistant message ID for TTS audio association.
            // In streaming mode, async audio fetches are still in-flight when
            // this event arrives. They will use currentAssistantMessageId as
            // a fallback when they resolve (see fetchTTS).
            if (event.message_id) {
                currentAssistantMessageId = event.message_id;
                // Tag the live assistant bubble with the message ID so audio
                // buttons can be injected into the correct DOM node later.
                if (assistantRow) {
                    assistantRow.dataset.messageId = event.message_id;
                }
            }

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
        case "error": {
            const bubble = assistantRow.querySelector(".bubble");
            if (bubble) {
                bubble.textContent += `\n\n[Error: ${event.message}]`;
            }
            break;
        }
        case "complete": {
            // Final signal — don't clear currentAssistantMessageId here.
            // In streaming mode, async audio fetches may still be in-flight.
            // The ID will be cleared by the next "start" event instead.
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
            playBtn.addEventListener("click", () => playPersistedAudio(roomName, filename));
            audioContainer.appendChild(playBtn);
        }
        wrapper.appendChild(audioContainer);
    }

    row.appendChild(wrapper);
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
            playBtn.addEventListener("click", () => playPersistedAudio(roomName, filename));
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
    messagesEl.appendChild(row);
}

/**
 * Play a persisted audio file using Web Audio API.
 */
async function playPersistedAudio(roomName, filename) {
    const url = getAudioUrl(roomName, filename);
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
        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);
        source.start();
    } catch (err) {
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

    // Lazily create the audio container on first button
    let audioContainer = content.querySelector(".message-audio");
    if (!audioContainer) {
        audioContainer = document.createElement("div");
        audioContainer.className = "message-audio";
        content.appendChild(audioContainer);
    }

    const playBtn = document.createElement("button");
    playBtn.className = "audio-play-btn";
    playBtn.innerHTML = "\u{1F501}"; // play icon
    playBtn.title = "Play audio";
    playBtn.addEventListener("click", () => playPersistedAudio(currentChatRoom, filename));
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

    // Lazily create the audio container on first button
    let audioContainer = wrapper.querySelector(".message-audio");
    if (!audioContainer) {
        audioContainer = document.createElement("div");
        audioContainer.className = "message-audio";
        wrapper.appendChild(audioContainer);
    }

    const playBtn = document.createElement("button");
    playBtn.className = "audio-play-btn";
    playBtn.innerHTML = "\u{1F501}"; // play icon
    playBtn.title = "Play audio";
    playBtn.addEventListener("click", () => playPersistedAudio(currentChatRoom, filename));
    audioContainer.appendChild(playBtn);
}
