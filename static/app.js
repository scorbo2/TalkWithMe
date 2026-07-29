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
let isStreaming = false;       // Guard: prevent double-sends during streaming

// FIFO audio queue for TTS playback
const audioQueue = [];
let audioCtx = null;
let isPlayingAudio = false;

// Microphone / STT state
let mediaRecorder = null;
let recordedChunks = [];

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

/* ==========================================================================
   Initialization
   ========================================================================== */

async function init() {
    await loadPersonas();
    await checkTTSHealth();
    setupEventListeners();
    showEmptyState();
}

async function loadPersonas() {
    try {
        const resp = await fetch("/api/personas");
        personas = await resp.json();
        renderPersonaList();
        // Default: select first persona
        if (personas.length > 0) {
            selectedPersona = personas[0].name;
            highlightSelectedPersona();
        }
        // Activate all personas in the session
        await fetch("/api/session/personas", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ active_personas: personas.map(p => p.name) }),
        });
    } catch (err) {
        console.error("Failed to load personas:", err);
    }
}

async function checkTTSHealth() {
    try {
        const resp = await fetch("/api/tts/health");
        const data = await resp.json();
        ttsAvailable = data.available;
        ttsEnabled = data.available; // Default on if server is available
        updateTTSToggleUI();
    } catch (err) {
        console.warn("TTS health check failed:", err);
        ttsAvailable = false;
        ttsEnabled = false;
        updateTTSToggleUI();
    }
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

    document.addEventListener("keydown", (e) => {
        if (e.ctrlKey && e.code === "Space" && !micBtn.disabled) {
            e.preventDefault();
            toggleMicrophone();
        }
    });
}

/* ==========================================================================
   Persona list rendering
   ========================================================================== */

function renderPersonaList() {
    personaListEl.innerHTML = "";
    for (const p of personas) {
        const card = document.createElement("div");
        card.className = "persona-card";
        card.dataset.name = p.name;
        card.addEventListener("click", () => {
            selectedPersona = p.name;
            highlightSelectedPersona();
        });

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
            break;
        }
        case "token": {
            const bubble = assistantRow.querySelector(".bubble");
            if (bubble) {
                bubble.textContent += event.token;
                scrollToBottom();
            }
            break;
        }
        case "done": {
            // Streaming complete — enqueue TTS if enabled
            if (ttsEnabled && event.text) {
                const persona = personas.find(p => p.name === event.persona);
                if (persona && persona.tts_capable) {
                    enqueueTTS(event.persona, event.text);
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
        // Process next item in queue
        setTimeout(() => processAudioQueue(), 100);
    }
}

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

    mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
        // Stop all tracks to release the microphone
        stream.getTracks().forEach(t => t.stop());
        micBtn.classList.remove("recording");
        micBtn.disabled = true;

        const blob = new Blob(recordedChunks, { type: "audio/webm" });
        const arrayBuffer = await blob.arrayBuffer();
        const bytes = new Uint8Array(arrayBuffer);
        let binary = "";
        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        const audio_base64 = btoa(binary);

        let sttFailed = false;
        try {
            const resp = await fetch("/api/stt", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ audio_base64 }),
            });

            if (!resp.ok) {
                console.error("STT request failed:", resp.status);
                appendErrorBubble("Speech recognition failed. Microphone disabled.");
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
            appendErrorBubble("Speech recognition failed. Microphone disabled.");
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
