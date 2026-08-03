/**
 * app.js — TalkWithMe frontend orchestrator.
 *
 * This file is intentionally slim. It coordinates initialization across
 * feature modules (state, chat, tts, stt, persona, chatrooms, settings, theme).
 *
 * Cross-cutting concerns that multiple modules depend on live here:
 *  - loadPersonas() — fetched by persona.js after CRUD, triggers loadChatRooms()
 *  - Health checks — called by init() and settings.js after save
 *  - Event listener setup — wires up topbar buttons shared across modules
 */

/* ==========================================================================
   Initialization
   ========================================================================== */

async function init() {
    initTheme();
    await loadPersonas(); // Also loads chat rooms internally
    await checkTTSHealth();
    await checkSTTHealth();
    await loadGeneralSettings();
    setupEventListeners();
    setupChatRoomEventListeners();

    // Load persisted history for the current room
    const history = await loadPersistedHistory(currentChatRoom);
    renderPersistedHistory(history.messages, currentChatRoom);
}

/**
 * Fetch general settings from the server. Currently only used to gate
 * the persona-name-mention detection feature.
 */
async function loadGeneralSettings() {
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.general != null) {
            personaNameMentionsEnabled = data.general.persona_name_mentions;
        }
    } catch (err) {
        console.warn("Failed to load general settings, using defaults:", err);
    }
}

/**
 * Load personas from server, then refresh chat rooms so persona lists
 * in each room are up to date (handles rename/delete cascades).
 * Called by init() on startup and by persona.js after CRUD operations.
 */
async function loadPersonas() {
    try {
        const resp = await fetch("/api/personas");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        personas = await resp.json();
        await loadChatRooms();
    } catch (err) {
        console.error("Failed to load personas:", err);
    }
}

/* ==========================================================================
   Health checks
   ========================================================================== */

async function checkTTSHealth() {
    try {
        const resp = await fetch("/api/tts/health");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        ttsAvailable = data.available;
        ttsStreaming = data.streaming || false;
        ttsEnabled = data.available; // Default on if server is available
        ttsServerType = data.server_type || "";
        updateTTSToggleUI();
    } catch (err) {
        console.warn("TTS health check failed:", err);
        ttsAvailable = false;
        ttsStreaming = false;
        ttsEnabled = false;
        ttsServerType = "";
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

/* ==========================================================================
   Top-level event listeners (shared UI controls)
   ========================================================================== */

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

/* ==========================================================================
   Session management
   ========================================================================== */

async function newChat() {
    try {
        // POST /api/session/new clears both the in-memory session AND
        // the persisted files for the current room.
        await fetch("/api/session/new", { method: "POST" });
        messagesEl.innerHTML = "";
        showEmptyState();
    } catch (err) {
        console.error("Failed to reset session:", err);
    }
}

/* ==========================================================================
   Boot
   ========================================================================== */

init();
