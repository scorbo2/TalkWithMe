/**
 * gen-settings.js — General Settings modal: load and persist app-level config.
 *
 * Handles max_persona_replies and persona_name_mentions through the general
 * settings overlay. Reads/writes via the existing /api/settings endpoint.
 */

/* ==========================================================================
   Event listeners
   ========================================================================== */

document.getElementById("btn-gen-settings").addEventListener("click", openGenSettings);
document.getElementById("gen-settings-btn-close").addEventListener("click", closeGenSettings);
document.getElementById("gen-settings-btn-cancel").addEventListener("click", closeGenSettings);
genSettingsForm.addEventListener("submit", submitGenSettings);

genSettingsOverlay.addEventListener("click", (e) => {
    if (e.target === genSettingsOverlay) closeGenSettings();
});

/* ==========================================================================
   Modal lifecycle
   ========================================================================== */

async function openGenSettings() {
    genSettingsOverlay.classList.remove("hidden");
    genSettingsError.classList.add("hidden");

    const saveBtn = document.getElementById("gen-settings-btn-save");
    saveBtn.disabled = true;

    const ok = await loadGenSettingsIntoForm();
    saveBtn.disabled = !ok;
}

function closeGenSettings() {
    genSettingsOverlay.classList.add("hidden");
}

/* ==========================================================================
   Form population
   ========================================================================== */

async function loadGenSettingsIntoForm() {
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) {
            showGenSettingsError(`Failed to load settings (HTTP ${resp.status}).`);
            return false;
        }
        const data = await resp.json();
        gsfMaxPersonaReplies.value = data.general.max_persona_replies ?? 1;
        gsfPersonaNameMentions.checked = data.general.persona_name_mentions ?? true;
        return true;
    } catch (err) {
        console.error("Failed to load settings:", err);
        showGenSettingsError("Failed to load settings. Is the server running?");
        return false;
    }
}

function showGenSettingsError(msg) {
    genSettingsError.textContent = msg;
    genSettingsError.classList.remove("hidden");
}

/* ==========================================================================
   Form submission
   ========================================================================== */

async function submitGenSettings(e) {
    e.preventDefault();
    genSettingsError.classList.add("hidden");

    const maxReplies = parseInt(gsfMaxPersonaReplies.value, 10);
    if (isNaN(maxReplies) || maxReplies < 1 || maxReplies > 4) {
        return showGenSettingsError("Max Persona Replies must be between 1 and 4.");
    }

    // Fetch current full settings so we can patch only the general section
    let current;
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) return showGenSettingsError(`Failed to load current settings (HTTP ${resp.status}).`);
        current = await resp.json();
    } catch (err) {
        return showGenSettingsError("Failed to load current settings.");
    }

    const payload = {
        ...current,
        // Restore null seed as 0 (API contract: 0 means no seed)
        tts: { ...current.tts, base_url: current.tts.base_url ?? "", seed: current.tts.seed ?? 0 },
        stt: { ...current.stt, base_url: current.stt.base_url ?? "" },
        general: {
            persona_name_mentions: gsfPersonaNameMentions.checked,
            max_persona_replies: maxReplies,
        },
    };

    try {
        const resp = await fetch("/api/settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            return showGenSettingsError(err.detail || `Server error ${resp.status}`);
        }

        // Sync in-memory state
        personaNameMentionsEnabled = gsfPersonaNameMentions.checked;
        maxPersonaReplies = maxReplies;

        closeGenSettings();
    } catch (err) {
        console.error("Failed to save settings:", err);
        showGenSettingsError("Request failed. Is the server running?");
    }
}
