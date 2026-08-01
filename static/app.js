/**
 * TalkWithMe — Frontend logic
 *
 * Handles: persona loading, chat messaging via SSE, TTS audio queue,
 * and UI interactions.
 */

/* ==========================================================================
    State
    ========================================================================== */

let personas = [];
let selectedPersona = null;   // The persona selected in the sidebar
let ttsEnabled = false;        // Whether TTS playback is toggled on
let ttsAvailable = false;      // Whether the TTS server is reachable
let ttsStreaming = false;       // Whether streaming (sentence-by-sentence) TTS is enabled
let sttAvailable = false;      // Whether the STT server is reachable
let isStreaming = false;       // Guard: prevent double-sends during streaming

// Chat room state
let currentChatRoom = "default";       // Currently selected chat room
let allChatRooms = [];                 // Full list of rooms (including "default")
let roomPersonas = {};                 // Map: room name -> list of persona names

// Microphone / STT state
let mediaRecorder = null;
let recordedChunks = [];

// Non-streaming: FIFO audio queue (fetch full text, then play)
const audioQueue = [];
let audioCtx = null;
let isPlayingAudio = false;

// Streaming TTS state
let sentenceBuffer = "";         // Token accumulator for in-progress response
let currentStreamingPersona = null;

// Streaming: decoupled fetch queue and decoded-buffer playback queue
const ttsRequestQueue = [];      // [{personaName, text}] waiting to be fetched
const audioBufferQueue = [];     // AudioBuffers decoded and ready to play
let isFetchingTTS = false;
let isPlayingAudioBuffer = false;
const THEME_STORAGE_KEY = "talkwithme_theme";

/* ==========================================================================
   DOM references
   ========================================================================== */

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("message-input");
const sendBtn = document.getElementById("btn-send");
const micBtn = document.getElementById("btn-mic");
const newChatBtn = document.getElementById("btn-new-chat");
const ttsToggleBtn = document.getElementById("btn-tts-toggle");
const ttsIcon = document.getElementById("tts-icon");
const personaListEl = document.getElementById("persona-list");
const whoChooser = document.getElementById("who-chooser");
const themeSelectEl = document.getElementById("theme-select");

// Chat room DOM references
const chatRoomDropdown = document.getElementById("chat-room-dropdown");
const btnAddPersona = document.getElementById("btn-add-persona");

/* ==========================================================================
   Initialization
   ========================================================================== */

async function init() {
    initTheme();
    await loadPersonas(); // Also loads chat rooms internally
    await checkTTSHealth();
    await checkSTTHealth();
    setupEventListeners();
    setupChatRoomEventListeners();
    showEmptyState();
}

async function loadPersonas() {
    try {
        const resp = await fetch("/api/personas");
        personas = await resp.json();
        // After loading personas, also refresh chat rooms so the persona lists
        // in each room are up to date (handles rename/delete cascades).
        await loadChatRooms();
    } catch (err) {
        console.error("Failed to load personas:", err);
    }
}

/**
 * Load all chat rooms from the server and initialize the room state.
 * After loading, applies the current room filter and renders the persona list.
 */
async function loadChatRooms() {
    try {
        const resp = await fetch("/api/chatrooms/all");
        allChatRooms = await resp.json();

        // Build the persona map
        roomPersonas = {};
        for (const room of allChatRooms) {
            roomPersonas[room.name] = room.persona_names;
        }

        // Populate the dropdown
        renderChatRoomDropdown();

        // If previously selected room no longer exists, revert to default
        const roomExists = allChatRooms.some(r => r.name === currentChatRoom);
        if (!roomExists && allChatRooms.length > 0) {
            currentChatRoom = "default";
            chatRoomDropdown.value = "default";
        }

        // Apply the current room filter and render
        applyChatRoomFilter();
    } catch (err) {
        console.error("Failed to load chat rooms:", err);
        // Fallback: show all personas in "default" room
        currentChatRoom = "default";
        renderPersonaList();
    }
}

/**
 * Apply the current chat room filter: update persona list, active session,
 * and UI controls (add/remove buttons).
 */
function applyChatRoomFilter() {
    const isActiveRoom = currentChatRoom !== "default";
    const roomPersonaNames = roomPersonas[currentChatRoom] || [];

    // Filter the persona list to only those in this room
    const filtered = isActiveRoom
        ? personas.filter(p => roomPersonaNames.includes(p.name))
        : [...personas];

    // Update the persona list rendering
    renderPersonaList(filtered, isActiveRoom);

    // Select first persona if none selected or selected one not in room
    if (filtered.length > 0) {
        if (!selectedPersona || !filtered.some(p => p.name === selectedPersona)) {
            selectedPersona = filtered[0].name;
        }
        highlightSelectedPersona();
    } else {
        selectedPersona = null;
    }

    // Activate only the room's personas in the session
    const activeNames = filtered.map(p => p.name);
    if (activeNames.length > 0) {
        fetch("/api/session/personas", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ active_personas: activeNames }),
        }).catch(err => console.error("Failed to update session personas:", err));
    }

    // Show/hide the "Add persona" button
    if (isActiveRoom) {
        btnAddPersona.classList.remove("hidden");
    } else {
        btnAddPersona.classList.add("hidden");
    }

    // Update dropdown selection
    chatRoomDropdown.value = currentChatRoom;
}

async function checkTTSHealth() {
    try {
        const resp = await fetch("/api/tts/health");
        const data = await resp.json();
        ttsAvailable = data.available;
        ttsStreaming = data.streaming || false;
        ttsEnabled = data.available; // Default on if server is available
        updateTTSToggleUI();
    } catch (err) {
        console.warn("TTS health check failed:", err);
        ttsAvailable = false;
        ttsStreaming = false;
        ttsEnabled = false;
        updateTTSToggleUI();
    }
}

async function checkSTTHealth() {
    try {
        const resp = await fetch("/api/stt/health");
        const data = await resp.json();
        sttAvailable = data.available;
        updateMicButtonUI();
    } catch (err) {
        console.warn("STT health check failed:", err);
        sttAvailable = false;
        updateMicButtonUI();
    }
}

function updateMicButtonUI() {
    micBtn.disabled = !sttAvailable;
}

function setupEventListeners() {
    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", (e) => {
        // Enter sends; Shift+Enter for newline
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Auto-resize textarea
    inputEl.addEventListener("input", () => {
        inputEl.style.height = "auto";
        inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
    });

    newChatBtn.addEventListener("click", newChat);
    ttsToggleBtn.addEventListener("click", toggleTTS);
    micBtn.addEventListener("click", toggleMicrophone);
    themeSelectEl.addEventListener("change", () => {
        applyTheme(themeSelectEl.value, true);
    });

    document.addEventListener("keydown", (e) => {
        if (e.ctrlKey && e.code === "Space" && !micBtn.disabled) {
            e.preventDefault();
            toggleMicrophone();
        }
    });
}

function initTheme() {
    let storedTheme = "dark";
    try {
        const fromStorage = localStorage.getItem(THEME_STORAGE_KEY);
        if (fromStorage) {
            storedTheme = fromStorage;
        }
    } catch (err) {
        console.warn("Theme storage unavailable:", err);
    }
    applyTheme(storedTheme, false);
}

function applyTheme(theme, persist) {
    const allowedThemes = new Set(["dark", "light", "matrix", "blues"]);
    const normalizedTheme = allowedThemes.has(theme) ? theme : "dark";
    document.body.dataset.theme = normalizedTheme;
    themeSelectEl.value = normalizedTheme;

    if (persist) {
        try {
            localStorage.setItem(THEME_STORAGE_KEY, normalizedTheme);
        } catch (err) {
            console.warn("Failed to persist theme:", err);
        }
    }
}

/* ==========================================================================
   Persona list rendering
   ========================================================================== */

/**
 * Render the persona list in the sidebar.
 * @param {Array} [list] - Optional filtered list. If omitted, uses all personas.
 * @param {boolean} [showRemoveButtons] - Whether to show the remove "x" button per persona.
 */
function renderPersonaList(list, showRemoveButtons) {
    personaListEl.innerHTML = "";
    const personaList = list || personas;
    const showRemove = !!showRemoveButtons;

    for (const p of personaList) {
        const card = document.createElement("div");
        card.className = "persona-card";
        card.dataset.name = p.name;

        // Avatar
        const avatar = document.createElement("div");
        avatar.className = "persona-avatar";
        avatar.style.backgroundColor = p.avatar_color;

        if (p.avatar_image) {
            const img = document.createElement("img");
            img.src = `/api/personas/${encodeURIComponent(p.name)}/avatar`;
            img.alt = p.name;
            img.onerror = () => {
                // Fallback to initial on error
                avatar.innerHTML = "";
                avatar.textContent = p.name.charAt(0).toUpperCase();
            };
            avatar.appendChild(img);
        } else {
            avatar.textContent = p.name.charAt(0).toUpperCase();
        }

        // Mute indicator if not TTS-capable
        if (!p.tts_capable) {
            const mute = document.createElement("span");
            mute.className = "mute-indicator";
            mute.textContent = "\u{1F507}";  // 🔇
            avatar.appendChild(mute);
        }

        // Info
        const info = document.createElement("div");
        info.className = "persona-info";

        const nameEl = document.createElement("div");
        nameEl.className = "persona-name";
        nameEl.textContent = p.name;

        const descEl = document.createElement("div");
        descEl.className = "persona-desc";
        descEl.textContent = p.description;

        info.appendChild(nameEl);
        info.appendChild(descEl);

        card.appendChild(avatar);
        card.appendChild(info);

        // Click handler for selecting persona (on the card, not the remove button)
        card.addEventListener("click", (e) => {
            // Don't trigger selection when clicking the remove button
            if (e.target.classList.contains("persona-remove-btn")) return;
            selectedPersona = p.name;
            highlightSelectedPersona();
            // If I clicked a persona, I probably want it to answer. Switch the chooser.
            const selectedRadio = document.querySelector('input[name="who_answers"][value="selected"]');
            if (selectedRadio) {
                selectedRadio.checked = true;
                selectedRadio.dispatchEvent(new Event("change", { bubbles: true }));
            }
        });

        // Remove button (only for non-default rooms)
        if (showRemove) {
            const removeBtn = document.createElement("button");
            removeBtn.className = "persona-remove-btn";
            removeBtn.textContent = "\u2715";  // ×
            removeBtn.title = `Remove ${p.name} from this chat room`;
            removeBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                removePersonaFromRoom(p.name);
            });
            card.appendChild(removeBtn);
        }

        personaListEl.appendChild(card);
    }
}

function highlightSelectedPersona() {
    document.querySelectorAll(".persona-card").forEach(card => {
        card.classList.toggle("selected", card.dataset.name === selectedPersona);
    });
}

/* ==========================================================================
   Chat
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

function getWhoAnswers() {
    const chosen = document.querySelector('input[name="who_answers"]:checked').value;
    if (chosen === "selected") {
        return selectedPersona || "router";
    }
    return chosen;
}

async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || isStreaming) return;

    // If the current chat room has no personas, show an error instead of sending
    const roomPersonaNames = roomPersonas[currentChatRoom] || [];
    if (roomPersonaNames.length === 0) {
        appendErrorBubble("No one is here.");
        return;
    }

    // Clear empty state if present
    if (messagesEl.querySelector(".empty-state")) {
        messagesEl.innerHTML = "";
    }

    // Append user bubble
    appendUserBubble(text);
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
            body: JSON.stringify({ message: text, who_answers: who }),
        });

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
        appendErrorBubble("Connection failed. Is the LLM server running?");
    } finally {
        isStreaming = false;
        sendBtn.disabled = false;
        inputEl.focus();
    }
}

function handleSSEEvent(event, assistantRow) {
    switch (event.type) {
        case "start": {
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
                        // Non-streaming: enqueue full text at once (original behavior)
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
            // Final signal — nothing to do
            break;
        }
    }
}

/* ==========================================================================
   Bubble creation
   ========================================================================== */

function appendUserBubble(text) {
    const row = document.createElement("div");
    row.className = "message-row user";

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;

    row.appendChild(bubble);
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
   TTS
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
    if (!ttsAvailable) return; // Can't enable if server is down
    ttsEnabled = !ttsEnabled;
    updateTTSToggleUI();
}

// ---------------------------------------------------------------------------
// Non-streaming TTS (original behavior: enqueue full text after LLM finishes)
// ---------------------------------------------------------------------------

function enqueueTTS(personaName, text) {
    audioQueue.push({ personaName, text });
    processAudioQueue();
}

async function processAudioQueue() {
    if (isPlayingAudio || audioQueue.length === 0) return;
    isPlayingAudio = true;

    const item = audioQueue.shift();
    try {
        const audioBuffer = await fetchTTS(item.personaName, item.text);
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

// ---------------------------------------------------------------------------
// Streaming TTS (sentence-by-sentence: fetch and play are pipelined)
// ---------------------------------------------------------------------------

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
    ttsRequestQueue.push({ personaName, text });
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
        const audioBuffer = await fetchTTS(item.personaName, item.text);
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

// ---------------------------------------------------------------------------
// Shared TTS helpers
// ---------------------------------------------------------------------------

async function fetchTTS(personaName, text) {
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

    // Decode base64 to ArrayBuffer
    const binary = atob(data.audio_base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }

    // Initialize AudioContext lazily
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
   STT / Microphone
   ========================================================================== */

async function toggleMicrophone() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
        micBtn.disabled = true; // prevent re-entry until onstop finishes
        mediaRecorder.stop();
        return;
    }

    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
        console.error("Microphone access denied:", err);
        appendErrorBubble("Microphone access was denied.");
        return;
    }

    recordedChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    // Capture the actual MIME type the browser chose. Different browsers/platforms
    // may produce webm, ogg, mp4, or other containers.
    const audioMimeType = mediaRecorder.mimeType || "audio/webm";
    mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
        // Stop all tracks to release the microphone
        stream.getTracks().forEach(t => t.stop());
        micBtn.classList.remove("recording");
        micBtn.disabled = true;

        const blob = new Blob(recordedChunks, { type: audioMimeType });

        const audio_base64 = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(reader.error);
            reader.onload = () => {
                const result = String(reader.result || "");
                resolve(result.split(",")[1] || "");
            };
            reader.readAsDataURL(blob);
        });
        let sttFailed = false;
        try {
            const resp = await fetch("/api/stt", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ audio_base64, audio_mime_type: audioMimeType }),
            });

            if (!resp.ok) {
                console.error("STT request failed:", resp.status);
                appendErrorBubble("Unable to process STT data.");
                sttFailed = true;
                return;
            }

            const data = await resp.json();
            if (data.text) {
                // Append transcribed text (never replace existing content)
                const existing = inputEl.value;
                inputEl.value = existing ? existing + " " + data.text : data.text;
                inputEl.dispatchEvent(new Event("input")); // trigger auto-resize
                sendMessage();
            }
        } catch (err) {
            console.error("STT error:", err);
            appendErrorBubble("Unable to process STT data.");
            sttFailed = true;
        } finally {
            if (!sttFailed) micBtn.disabled = false;
        }
    };

    mediaRecorder.start();
    micBtn.classList.add("recording");
}

/* ==========================================================================
   Session management
   ========================================================================== */

async function newChat() {
    try {
        await fetch("/api/session/new", { method: "POST" });
        messagesEl.innerHTML = "";
        showEmptyState();
    } catch (err) {
        console.error("Failed to reset session:", err);
    }
}

/* ==========================================================================
   Utilities
   ========================================================================== */

function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    });
}

/* ==========================================================================
   Boot
   ========================================================================== */

init();

/* ==========================================================================
   Persona Editor
   ========================================================================== */

const personaEditorOverlay = document.getElementById("persona-editor-overlay");
const peListView           = document.getElementById("pe-list-view");
const peFormView           = document.getElementById("pe-form-view");
const peListEl             = document.getElementById("pe-list");
const peFormTitle          = document.getElementById("pe-form-title");
const peFormError          = document.getElementById("pe-form-error");
const peForm               = document.getElementById("pe-form");
const peConfirmOverlay     = document.getElementById("pe-confirm-overlay");
const peConfirmMsg         = document.getElementById("pe-confirm-msg");

// Form fields
const pfName              = document.getElementById("pf-name");
const pfDescription       = document.getElementById("pf-description");
const pfSystemPrompt      = document.getElementById("pf-system-prompt");
const pfRouterHints       = document.getElementById("pf-router-hints");
const pfAvatarColor       = document.getElementById("pf-avatar-color");
const pfLanguage          = document.getElementById("pf-language");
const pfAvatarImage       = document.getElementById("pf-avatar-image");
const pfReferenceAudio    = document.getElementById("pf-reference-audio");
const pfReferenceAudioTx  = document.getElementById("pf-reference-audio-transcript");

// Editing state
let peEditingName = null;  // null = creating, string = editing existing name

document.getElementById("btn-persona-editor").addEventListener("click", openPersonaEditor);
document.getElementById("pe-btn-close").addEventListener("click", closePersonaEditor);
document.getElementById("pe-btn-new").addEventListener("click", () => openPersonaForm(null));
document.getElementById("pe-form-btn-cancel").addEventListener("click", showPersonaList);
document.getElementById("pe-form-btn-cancel2").addEventListener("click", showPersonaList);
peForm.addEventListener("submit", submitPersonaForm);
document.getElementById("pe-confirm-cancel").addEventListener("click", () => {
    peConfirmOverlay.classList.add("hidden");
});

// Close modals on overlay backdrop click
personaEditorOverlay.addEventListener("click", (e) => {
    if (e.target === personaEditorOverlay) closePersonaEditor();
});
peConfirmOverlay.addEventListener("click", (e) => {
    if (e.target === peConfirmOverlay) peConfirmOverlay.classList.add("hidden");
});

function openPersonaEditor() {
    personaEditorOverlay.classList.remove("hidden");
    showPersonaList();
}

function closePersonaEditor() {
    personaEditorOverlay.classList.add("hidden");
}

function showPersonaList() {
    peListView.classList.remove("hidden");
    peFormView.classList.add("hidden");
    renderPersonaEditorList();
}

async function renderPersonaEditorList() {
    try {
        const resp = await fetch("/api/personas");
        const list = await resp.json();
        peListEl.innerHTML = "";

        if (list.length === 0) {
            peListEl.innerHTML = '<p class="pe-empty">No personas defined yet. Click &ldquo;+ New Persona&rdquo; to create one.</p>';
            return;
        }

        for (const p of list) {
            const item = document.createElement("div");
            item.className = "pe-list-item";

            const avatar = document.createElement("div");
            avatar.className = "pe-list-item-avatar";
            avatar.style.backgroundColor = p.avatar_color;
            avatar.textContent = p.name.charAt(0).toUpperCase();

            const info = document.createElement("div");
            info.className = "pe-list-item-info";
            info.innerHTML = `<div class="pe-list-item-name">${escapeHtml(p.name)}</div>
                              <div class="pe-list-item-desc">${escapeHtml(p.description || "")}</div>`;

            const actions = document.createElement("div");
            actions.className = "pe-list-item-actions";

            const editBtn = document.createElement("button");
            editBtn.textContent = "Edit";
            editBtn.addEventListener("click", () => openPersonaForm(p.name));

            const cloneBtn = document.createElement("button");
            cloneBtn.textContent = "Clone";
            cloneBtn.addEventListener("click", () => clonePersona(p.name));

            const deleteBtn = document.createElement("button");
            deleteBtn.textContent = "Delete";
            deleteBtn.className = "btn-delete";
            deleteBtn.addEventListener("click", () => confirmDeletePersona(p.name));

            actions.appendChild(editBtn);
            actions.appendChild(cloneBtn);
            actions.appendChild(deleteBtn);

            item.appendChild(avatar);
            item.appendChild(info);
            item.appendChild(actions);
            peListEl.appendChild(item);
        }
    } catch (err) {
        console.error("Failed to load persona list:", err);
    }
}

async function openPersonaForm(name) {
    peEditingName = name;
    peFormError.classList.add("hidden");
    peFormError.textContent = "";

    if (name) {
        peFormTitle.textContent = `Edit Persona: ${name}`;
        try {
            const resp = await fetch(`/api/personas/${encodeURIComponent(name)}/detail`);
            if (!resp.ok) {
                showPersonaFormError("Failed to load persona details.");
                return;
            }
            const p = await resp.json();
            pfName.value              = p.name;
            pfDescription.value       = p.description || "";
            pfSystemPrompt.value      = p.system_prompt;
            pfRouterHints.value       = p.router_hints;
            pfAvatarColor.value       = p.avatar_color || "#FF0000";
            pfLanguage.value          = p.language || "en";
            pfAvatarImage.value       = p.avatar_image || "";
            pfReferenceAudio.value    = p.reference_audio || "";
            pfReferenceAudioTx.value  = p.reference_audio_transcript || "";
        } catch (err) {
            showPersonaFormError("Failed to load persona details.");
            return;
        }
    } else {
        peFormTitle.textContent = "New Persona";
        pfName.value              = "";
        pfDescription.value       = "";
        pfSystemPrompt.value      = "";
        pfRouterHints.value       = "";
        pfAvatarColor.value       = "#FF0000";
        pfLanguage.value          = "en";
        pfAvatarImage.value       = "";
        pfReferenceAudio.value    = "";
        pfReferenceAudioTx.value  = "";
    }

    peListView.classList.add("hidden");
    peFormView.classList.remove("hidden");
    pfName.focus();
}

async function submitPersonaForm(e) {
    e.preventDefault();
    peFormError.classList.add("hidden");

    const name         = pfName.value.trim();
    const description  = pfDescription.value.trim();
    const systemPrompt = pfSystemPrompt.value.trim();
    const routerHints  = pfRouterHints.value.trim();
    const avatarColor  = pfAvatarColor.value;
    const language     = pfLanguage.value.trim();
    const avatarImage  = pfAvatarImage.value.trim();
    const refAudio     = pfReferenceAudio.value.trim();
    const refAudioTx   = pfReferenceAudioTx.value.trim();

    if (!name) return showPersonaFormError("Name is required.");
    if (!systemPrompt) return showPersonaFormError("System prompt is required.");
    if (!routerHints) return showPersonaFormError("Router hints are required.");
    if (language.length !== 2) return showPersonaFormError("Language must be a 2-letter code.");

    const payload = {
        name,
        description,
        system_prompt: systemPrompt,
        router_hints: routerHints,
        avatar_color: avatarColor,
        language,
        avatar_image: avatarImage || null,
        reference_audio: refAudio || null,
        reference_audio_transcript: refAudioTx || null,
    };

    try {
        let resp;
        if (peEditingName) {
            resp = await fetch(`/api/personas/${encodeURIComponent(peEditingName)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        } else {
            resp = await fetch("/api/personas", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        }

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            return showPersonaFormError(err.detail || `Error ${resp.status}`);
        }

        // Refresh sidebar persona list
        await loadPersonas();
        showPersonaList();
    } catch (err) {
        showPersonaFormError("Request failed. Is the server running?");
    }
}

async function clonePersona(name) {
    try {
        const resp = await fetch(`/api/personas/${encodeURIComponent(name)}/clone`, { method: "POST" });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            console.error("Clone failed:", err.detail);
            return;
        }
        await loadPersonas();
        renderPersonaEditorList();
    } catch (err) {
        console.error("Clone error:", err);
    }
}

function confirmDeletePersona(name) {
    peConfirmMsg.textContent = `Delete persona "${name}"? This cannot be undone.`;
    peConfirmOverlay.classList.remove("hidden");

    const deleteBtn = document.getElementById("pe-confirm-delete");
    // Replace to clear old listeners
    const newBtn = deleteBtn.cloneNode(true);
    deleteBtn.parentNode.replaceChild(newBtn, deleteBtn);
    newBtn.addEventListener("click", () => deletePersona(name));
}

async function deletePersona(name) {
    peConfirmOverlay.classList.add("hidden");
    try {
        const resp = await fetch(`/api/personas/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!resp.ok && resp.status !== 204) {
            console.error("Delete failed:", resp.status);
            return;
        }
        await loadPersonas();
        renderPersonaEditorList();
    } catch (err) {
        console.error("Delete error:", err);
    }
}

function showPersonaFormError(msg) {
    peFormError.textContent = msg;
    peFormError.classList.remove("hidden");
}

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

/* ==========================================================================
    Chat Rooms
    ========================================================================== */

/**
 * Populate the chat room dropdown from the server's full list.
 */
function renderChatRoomDropdown() {
    chatRoomDropdown.innerHTML = "";
    for (const room of allChatRooms) {
        const opt = document.createElement("option");
        opt.value = room.name;
        opt.textContent = room.name === "default" ? "All Personas" : room.name;
        chatRoomDropdown.appendChild(opt);
    }
}

function setupChatRoomEventListeners() {
    // Dropdown change: switch rooms
    chatRoomDropdown.addEventListener("change", () => {
        switchChatRoom(chatRoomDropdown.value);
    });

    // "Add persona" button in sidebar
    btnAddPersona.addEventListener("click", openPersonaPicker);

    // Chat rooms editor button in topbar
    document.getElementById("btn-chat-rooms").addEventListener("click", openChatRoomsEditor);
    document.getElementById("cr-btn-close").addEventListener("click", closeChatRoomsEditor);

    // New room form
    document.getElementById("cr-btn-new").addEventListener("click", showNewRoomForm);
    document.getElementById("cr-new-cancel").addEventListener("click", hideNewRoomForm);
    document.getElementById("cr-new-save").addEventListener("click", createChatRoom);

    // Delete confirmation
    document.getElementById("cr-confirm-cancel").addEventListener("click", () => {
        document.getElementById("cr-confirm-overlay").classList.add("hidden");
    });

    // Backdrop click to close
    document.getElementById("chatrooms-overlay").addEventListener("click", (e) => {
        if (e.target === document.getElementById("chatrooms-overlay")) closeChatRoomsEditor();
    });
    document.getElementById("cr-confirm-overlay").addEventListener("click", (e) => {
        if (e.target === document.getElementById("cr-confirm-overlay")) {
            document.getElementById("cr-confirm-overlay").classList.add("hidden");
        }
    });

    // Persona picker
    document.getElementById("pp-btn-close").addEventListener("click", closePersonaPicker);
    document.getElementById("pp-btn-cancel").addEventListener("click", closePersonaPicker);
    document.getElementById("pp-btn-add").addEventListener("click", addSelectedPersonasToRoom);
    document.getElementById("persona-picker-overlay").addEventListener("click", (e) => {
        if (e.target === document.getElementById("persona-picker-overlay")) closePersonaPicker();
    });
}

/**
 * Switch to a different chat room. Clears the chat panel and updates the
 * persona list to match the room's assigned personas.
 */
function switchChatRoom(roomName) {
    currentChatRoom = roomName;

    // Clear chat panel — same as "New Chat"
    messagesEl.innerHTML = "";
    showEmptyState();

    // Reset session history on backend
    fetch("/api/session/new", { method: "POST" })
        .catch(err => console.error("Failed to reset session:", err));

    // Re-apply filter
    applyChatRoomFilter();
}

/**
 * Remove a persona from the current chat room.
 */
async function removePersonaFromRoom(personaName) {
    if (currentChatRoom === "default") return; // Shouldn't happen, but guard anyway

    try {
        const resp = await fetch(
            `/api/chatrooms/${encodeURIComponent(currentChatRoom)}/personas/${encodeURIComponent(personaName)}`,
            { method: "DELETE" }
        );
        if (!resp.ok) {
            console.error("Failed to remove persona from room:", resp.status);
            return;
        }
        // Update local state
        if (roomPersonas[currentChatRoom]) {
            roomPersonas[currentChatRoom] = roomPersonas[currentChatRoom].filter(
                p => p !== personaName
            );
        }
        // If the removed persona was selected, clear selection
        if (selectedPersona === personaName) {
            const roomNames = roomPersonas[currentChatRoom] || [];
            selectedPersona = roomNames.length > 0 ? roomNames[0] : null;
        }
        applyChatRoomFilter();
    } catch (err) {
        console.error("Remove persona from room error:", err);
    }
}

/* --------------------------------------------------------------------------
   Chat Rooms Editor Modal
   -------------------------------------------------------------------------- */

function openChatRoomsEditor() {
    hideNewRoomForm();
    document.getElementById("chatrooms-overlay").classList.remove("hidden");
    document.getElementById("cr-form-error").classList.add("hidden");
    renderChatRoomList();
}

function closeChatRoomsEditor() {
    document.getElementById("chatrooms-overlay").classList.add("hidden");
    // Refresh the dropdown and re-apply room filter (in case rooms were deleted)
    loadChatRooms();
}

function renderChatRoomList() {
    const listEl = document.getElementById("cr-list");
    listEl.innerHTML = "";

    // Only show non-default rooms
    const rooms = allChatRooms.filter(r => r.name !== "default");

    if (rooms.length === 0) {
        listEl.innerHTML = '<p class="cr-empty">No chat rooms yet. Click &ldquo;+ New Room&rdquo; to create one.</p>';
        return;
    }

    for (const room of rooms) {
        const item = document.createElement("div");
        item.className = "cr-list-item";

        const nameEl = document.createElement("span");
        nameEl.className = "cr-list-item-name";
        nameEl.textContent = room.name;

        const countEl = document.createElement("span");
        countEl.className = "cr-list-item-count";
        countEl.textContent = `${room.persona_names.length} persona${room.persona_names.length !== 1 ? 's' : ''}`;

        const deleteBtn = document.createElement("button");
        deleteBtn.className = "cr-list-item-delete";
        deleteBtn.textContent = "Delete";
        deleteBtn.title = `Delete "${room.name}"`;
        deleteBtn.addEventListener("click", () => confirmDeleteChatRoom(room.name));

        item.appendChild(nameEl);
        item.appendChild(countEl);
        item.appendChild(deleteBtn);
        listEl.appendChild(item);
    }
}

function showNewRoomForm() {
    document.getElementById("cr-new-form").classList.remove("hidden");
    document.getElementById("cr-list").classList.add("hidden");
    document.getElementById("cr-name-input").value = "";
    document.getElementById("cr-form-error").classList.add("hidden");
    const input = document.getElementById("cr-name-input");
    input.focus();
    // Allow Enter key to create the room
    input.onkeydown = (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            createChatRoom();
        }
    };
}

function hideNewRoomForm() {
    document.getElementById("cr-new-form").classList.add("hidden");
    document.getElementById("cr-list").classList.remove("hidden");
}

async function createChatRoom() {
    const name = document.getElementById("cr-name-input").value.trim();
    const errorEl = document.getElementById("cr-form-error");

    if (!name) {
        errorEl.textContent = "Room name is required.";
        errorEl.classList.remove("hidden");
        return;
    }
    if (name.length > 20) {
        errorEl.textContent = "Room name must be 20 characters or fewer.";
        errorEl.classList.remove("hidden");
        return;
    }
    if (name.toLowerCase() === "default") {
        errorEl.textContent = "'default' is a reserved name and cannot be used.";
        errorEl.classList.remove("hidden");
        return;
    }

    try {
        const resp = await fetch("/api/chatrooms", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            errorEl.textContent = err.detail || `Error ${resp.status}`;
            errorEl.classList.remove("hidden");
            return;
        }

        hideNewRoomForm();
        // Reload rooms to refresh the list
        await loadChatRooms();
        renderChatRoomList();
    } catch (err) {
        errorEl.textContent = "Request failed. Is the server running?";
        errorEl.classList.remove("hidden");
    }
}

function confirmDeleteChatRoom(name) {
    const overlay = document.getElementById("cr-confirm-overlay");
    const msgEl = document.getElementById("cr-confirm-msg");
    msgEl.textContent = `Delete chat room "${name}"? Personas will not be deleted, only unassigned from this room.`;
    overlay.classList.remove("hidden");

    const deleteBtn = document.getElementById("cr-confirm-delete");
    const newBtn = deleteBtn.cloneNode(true);
    deleteBtn.parentNode.replaceChild(newBtn, deleteBtn);
    newBtn.addEventListener("click", () => deleteChatRoom(name));
}

async function deleteChatRoom(name) {
    document.getElementById("cr-confirm-overlay").classList.add("hidden");
    try {
        const resp = await fetch(`/api/chatrooms/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!resp.ok) {
            console.error("Delete chat room failed:", resp.status);
            return;
        }
        // If we deleted the currently selected room, switch back to default
        if (currentChatRoom === name) {
            currentChatRoom = "default";
        }
        await loadChatRooms();
        renderChatRoomList();
    } catch (err) {
        console.error("Delete chat room error:", err);
    }
}

/* --------------------------------------------------------------------------
   Persona Picker Modal (for adding personas to a chat room)
   -------------------------------------------------------------------------- */

let ppSelectedNames = []; // Track which personas are selected in the picker

function openPersonaPicker() {
    if (currentChatRoom === "default") return;

    ppSelectedNames = [];
    document.getElementById("persona-picker-overlay").classList.remove("hidden");
    renderPersonaPickerList();
}

function closePersonaPicker() {
    document.getElementById("persona-picker-overlay").classList.add("hidden");
}

function renderPersonaPickerList() {
    const listEl = document.getElementById("pp-list");
    listEl.innerHTML = "";

    // Get personas already in this room (to pre-check them? No — show ALL personas,
    // let user pick ones to add)
    const alreadyInRoom = new Set(roomPersonas[currentChatRoom] || []);

    if (personas.length === 0) {
        listEl.innerHTML = '<p class="pp-empty">No personas configured.</p>';
        return;
    }

    for (const p of personas) {
        const item = document.createElement("div");
        item.className = "pp-list-item";
        item.dataset.name = p.name;

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.className = "pp-checkbox";
        checkbox.checked = false;
        checkbox.addEventListener("click", (e) => {
            e.stopPropagation();
            togglePickerSelection(p.name, checkbox.checked, item);
        });

        const avatar = document.createElement("div");
        avatar.className = "pp-list-item-avatar";
        avatar.style.backgroundColor = p.avatar_color;
        avatar.textContent = p.name.charAt(0).toUpperCase();

        const info = document.createElement("div");
        info.className = "pp-list-item-info";

        const nameEl = document.createElement("div");
        nameEl.className = "pp-list-item-name";
        nameEl.textContent = p.name;

        const descEl = document.createElement("div");
        descEl.className = "pp-list-item-desc";
        descEl.textContent = alreadyInRoom.has(p.name) ? p.description + " (already in room)" : p.description || "";

        info.appendChild(nameEl);
        info.appendChild(descEl);

        item.appendChild(checkbox);
        item.appendChild(avatar);
        item.appendChild(info);

        // Clicking the row toggles the checkbox
        item.addEventListener("click", () => {
            const isChecked = !checkbox.checked;
            checkbox.checked = isChecked;
            togglePickerSelection(p.name, isChecked, item);
        });

        listEl.appendChild(item);
    }
}

function togglePickerSelection(name, isSelected, itemEl) {
    if (isSelected) {
        ppSelectedNames.push(name);
        if (itemEl) itemEl.classList.add("selected");
    } else {
        ppSelectedNames = ppSelectedNames.filter(n => n !== name);
        if (itemEl) itemEl.classList.remove("selected");
    }

}

async function addSelectedPersonasToRoom() {
    if (ppSelectedNames.length === 0 || currentChatRoom === "default") return;

    try {
        const resp = await fetch(
            `/api/chatrooms/${encodeURIComponent(currentChatRoom)}/personas`,
            {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ persona_names: ppSelectedNames }),
            }
        );

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            console.error("Failed to add personas to room:", err.detail);
            return;
        }

        closePersonaPicker();
        // Reload to refresh the persona list
        await loadChatRooms();
    } catch (err) {
        console.error("Add personas to room error:", err);
    }
}

/* ==========================================================================
    Settings Modal
    ========================================================================== */

const settingsOverlay   = document.getElementById("settings-overlay");
const settingsForm      = document.getElementById("settings-form");
const settingsError     = document.getElementById("settings-error");

// LLM fields
const sfLlmBaseUrl      = document.getElementById("sf-llm-base-url");
const sfLlmModel        = document.getElementById("sf-llm-model");
const sfLlmMaxTokens    = document.getElementById("sf-llm-max-tokens");
const sfLlmTemperature  = document.getElementById("sf-llm-temperature");

// TTS fields
const sfTtsEnabled      = document.getElementById("sf-tts-enabled");
const sfTtsFields       = document.getElementById("sf-tts-fields");
const sfTtsBaseUrl      = document.getElementById("sf-tts-base-url");
const sfTtsNumSteps     = document.getElementById("sf-tts-num-steps");
const sfTtsGuidanceScale = document.getElementById("sf-tts-guidance-scale");
const sfTtsSeed         = document.getElementById("sf-tts-seed");
const sfTtsTimeout      = document.getElementById("sf-tts-timeout");
const sfTtsStreaming    = document.getElementById("sf-tts-streaming");

// STT fields
const sfSttEnabled      = document.getElementById("sf-stt-enabled");
const sfSttFields       = document.getElementById("sf-stt-fields");
const sfSttBaseUrl      = document.getElementById("sf-stt-base-url");
const sfSttTimeout      = document.getElementById("sf-stt-timeout");

// Event listeners
document.getElementById("btn-settings").addEventListener("click", openSettings);
document.getElementById("settings-btn-close").addEventListener("click", closeSettings);
document.getElementById("settings-btn-cancel").addEventListener("click", closeSettings);
settingsForm.addEventListener("submit", submitSettings);

// Close on backdrop click
settingsOverlay.addEventListener("click", (e) => {
    if (e.target === settingsOverlay) closeSettings();
});

// Toggle TTS/STT fields visibility on checkbox change
sfTtsEnabled.addEventListener("change", () => {
    updateTtsFieldsState();
});
sfSttEnabled.addEventListener("change", () => {
    updateSttFieldsState();
});

async function openSettings() {
    settingsOverlay.classList.remove("hidden");
    settingsError.classList.add("hidden");

    const saveBtn = document.getElementById("settings-btn-save");
    saveBtn.disabled = true;

    const ok = await loadSettingsIntoForm();
    saveBtn.disabled = !ok;
}

function closeSettings() {
    settingsOverlay.classList.add("hidden");
}

async function loadSettingsIntoForm() {
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) {
            console.error("Failed to load settings:", resp.status);
            showSettingsError(`Failed to load settings (HTTP ${resp.status}).`);
            return false;
        }
        const data = await resp.json();
        populateSettingsForm(data);
        return true;
    } catch (err) {
        console.error("Failed to load settings:", err);
        showSettingsError("Failed to load settings. Is the server running?");
        return false;
    }
}

function populateSettingsForm(data) {
    // LLM (always populated)
    sfLlmBaseUrl.value = data.llm.base_url || "";
    sfLlmModel.value = data.llm.model || "";
    sfLlmMaxTokens.value = data.llm.max_tokens || 1024;
    sfLlmTemperature.value = data.llm.temperature ?? 0.8;

    // TTS
    sfTtsEnabled.checked = data.tts.enabled;
    sfTtsBaseUrl.value = data.tts.base_url || "";
    sfTtsNumSteps.value = data.tts.num_steps ?? 12;
    sfTtsGuidanceScale.value = data.tts.guidance_scale ?? 1.5;
    // seed: null from API -> 0 in form (0 means "no seed")
    sfTtsSeed.value = data.tts.seed ?? 0;
    sfTtsTimeout.value = data.tts.timeout ?? 120;
    sfTtsStreaming.checked = data.tts.streaming || false;
    updateTtsFieldsState();

    // STT
    sfSttEnabled.checked = data.stt.enabled;
    sfSttBaseUrl.value = data.stt.base_url || "";
    sfSttTimeout.value = data.stt.timeout ?? 30;
    updateSttFieldsState();
}

function updateTtsFieldsState() {
    if (sfTtsEnabled.checked) {
        sfTtsFields.classList.remove("disabled");
    } else {
        sfTtsFields.classList.add("disabled");
    }
}

function updateSttFieldsState() {
    if (sfSttEnabled.checked) {
        sfSttFields.classList.remove("disabled");
    } else {
        sfSttFields.classList.add("disabled");
    }
}

function showSettingsError(msg) {
    settingsError.textContent = msg;
    settingsError.classList.remove("hidden");
}

function collectSettingsFromForm() {
    return {
        llm: {
            base_url: sfLlmBaseUrl.value.trim(),
            model: sfLlmModel.value.trim(),
            max_tokens: parseInt(sfLlmMaxTokens.value, 10),
            temperature: parseFloat(sfLlmTemperature.value),
        },
        tts: {
            enabled: sfTtsEnabled.checked,
            base_url: sfTtsBaseUrl.value.trim(),
            num_steps: parseInt(sfTtsNumSteps.value, 10),
            guidance_scale: parseFloat(sfTtsGuidanceScale.value),
            // 0 in form means null (no seed)
            seed: sfTtsEnabled.checked ? parseInt(sfTtsSeed.value, 10) : 0,
            timeout: parseFloat(sfTtsTimeout.value),
            streaming: sfTtsStreaming.checked,
        },
        stt: {
            enabled: sfSttEnabled.checked,
            base_url: sfSttBaseUrl.value.trim(),
            timeout: parseFloat(sfSttTimeout.value),
        },
    };
}

function validateSettings(data) {
    // LLM validation
    if (!data.llm.base_url) return "LLM Base URL is required.";
    if (!data.llm.model) return "LLM Model is required.";
    if (isNaN(data.llm.max_tokens) || data.llm.max_tokens < 1) return "LLM Max Tokens must be a positive number.";
    if (isNaN(data.llm.temperature) || data.llm.temperature < 0 || data.llm.temperature > 1) {
        return "LLM Temperature must be between 0.0 and 1.0.";
    }

    // TTS validation (only if enabled)
    if (data.tts.enabled) {
        if (!data.tts.base_url) return "TTS Base URL is required when TTS is enabled.";
        if (isNaN(data.tts.num_steps) || data.tts.num_steps < 4 || data.tts.num_steps > 20) {
            return "TTS Step Count must be between 4 and 20.";
        }
        if (isNaN(data.tts.guidance_scale) || data.tts.guidance_scale < 1.0 || data.tts.guidance_scale > 2.0) {
            return "TTS CFG must be between 1.0 and 2.0.";
        }
        if (isNaN(data.tts.seed) || data.tts.seed < 0) {
            return "TTS Seed must be a non-negative integer.";
        }
        if (isNaN(data.tts.timeout) || data.tts.timeout < 5 || data.tts.timeout > 300) {
            return "TTS Timeout must be between 5 and 300 seconds.";
        }
    }

    // STT validation (only if enabled)
    if (data.stt.enabled) {
        if (!data.stt.base_url) return "STT Base URL is required when STT is enabled.";
        if (isNaN(data.stt.timeout) || data.stt.timeout < 5 || data.stt.timeout > 300) {
            return "STT Timeout must be between 5 and 300 seconds.";
        }
    }

    return null; // No errors
}

async function submitSettings(e) {
    e.preventDefault();
    settingsError.classList.add("hidden");

    const data = collectSettingsFromForm();
    const error = validateSettings(data);
    if (error) {
        return showSettingsError(error);
    }

    try {
        const resp = await fetch("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            return showSettingsError(err.detail || `Server error ${resp.status}`);
        }

        // Update in-memory TTS state based on new settings
        ttsStreaming = data.tts.streaming;
        // Re-check service health to update UI availability after settings change
        await checkTTSHealth();
        await checkSTTHealth();

        closeSettings();
    } catch (err) {
        console.error("Failed to save settings:", err);
        showSettingsError("Request failed. Is the server running?");
    }
}

