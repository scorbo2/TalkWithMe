/**
 * settings.js — Settings modal: load, validate, and persist server config.
 *
 * Handles LLM, TTS, and STT configuration through the settings overlay.
 */

/* ==========================================================================
   Event listeners
   ========================================================================== */

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

/* ==========================================================================
   Modal lifecycle
   ========================================================================== */

async function openSettings() {
    settingsOverlay.classList.remove("hidden");
    settingsError.classList.add("hidden");

    const saveBtn = document.getElementById("settings-btn-save");
    saveBtn.disabled = true;

    const ok = await loadSettingsIntoForm();
    saveBtn.disabled = !ok;

    // Server type is ephemeral — comes from health check, not settings
    updateTtsServerTypeField();
}

function closeSettings() {
    settingsOverlay.classList.add("hidden");
}

/* ==========================================================================
   Form population
   ========================================================================== */

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

// Display the TTS server type from the latest health check, truncated to 12 chars.
function updateTtsServerTypeField() {
    sfTtsServerType.value = ttsServerType.length > 12 ? ttsServerType.slice(0, 12) : ttsServerType;
}

function showSettingsError(msg) {
    settingsError.textContent = msg;
    settingsError.classList.remove("hidden");
}

/* ==========================================================================
   Form submission
   ========================================================================== */

function collectSettingsFromForm() {
    return {
        llm: {
            base_url: sfLlmBaseUrl.value.trim(),
            model: sfLlmModel.value.trim(),
            max_tokens: parseInt(sfLlmMaxTokens.value, 10),
            temperature: parseFloat(sfLlmTemperature.value),
        },
        // No UI for general settings in this dialog; preserve current values so they
        // survive a settings save round-trip.
        general: {
            persona_name_mentions: personaNameMentionsEnabled,
            max_persona_replies: maxPersonaReplies,
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
        // Update in-memory general settings
        if (data.general != null) {
            personaNameMentionsEnabled = data.general.persona_name_mentions;
            maxPersonaReplies = data.general.max_persona_replies ?? 1;
        }
        // Re-check service health to update UI availability after settings change
        await checkTTSHealth();
        await checkSTTHealth();

        closeSettings();
    } catch (err) {
        console.error("Failed to save settings:", err);
        showSettingsError("Request failed. Is the server running?");
    }
}
