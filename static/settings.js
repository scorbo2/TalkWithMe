/**
 * settings.js — Settings modal: load, validate, and persist server config.
 *
 * Handles LLM, TTS, and STT configuration through the settings overlay.
 * The TTS section is partly dynamic (TTS generification, plan M4): the
 * parameter widgets and engine info block are rendered from the connected
 * engine's /capabilities document by static/tts-params.js.
 */

/* ==========================================================================
   Dynamic TTS state (plan M4)
   ========================================================================== */

// The capabilities document for the currently rendered parameter section
// (null = nothing rendered: TTS off, no URL, or server unreachable).
let ttsCapabilitiesDoc = null;
// The URL the current document was fetched from: the probed URL when the
// user reconnected to an (unsaved) URL, otherwise the saved base URL.
// Compared on the next successful fetch to decide the "Engine switched"
// note — a same-URL refetch is not an engine switch.
let ttsCapabilitiesDocUrl = null;
// The tts.base_url last loaded from the server (GET /api/settings). A
// plain (no-probe) refresh serves the document for THIS url, so the
// field's current value is not the reference — the user may have edited
// it without blurring.
let ttsSavedBaseUrl = null;
// The parameter values last loaded from the server (GET /api/settings);
// re-applied on an engine switch, and passed through on save when no
// document is loaded (never silently wipe).
let ttsSavedParameters = {};
// Monotonic sequence so a slow capabilities response can't clobber a
// newer one (user typing fast in the Base URL field).
let ttsCapFetchSeq = 0;

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
    refreshTtsCapabilities();
});
sfSttEnabled.addEventListener("change", () => {
    updateSttFieldsState();
});

// The dynamic parameter section is tied to the TTS server, not the form:
// switching the Base URL means switching engines, so PROBE the new URL
// (possibly still unsaved — the ?base_url= parameter exists precisely for
// that; the plain endpoint only knows the SAVED url) and re-render
// against whatever answers (plan M4.1).
sfTtsBaseUrl.addEventListener("change", () => {
    refreshTtsCapabilities(sfTtsBaseUrl.value.trim());
});

// Explicit reconnect button (plan M4.1): re-probe the URL currently in the
// field and re-render in place — no save + reopen needed to see a new
// engine's parameter set. type=button in the markup, so it never submits.
sfTtsCapRefreshBtn.addEventListener("click", () => {
    refreshTtsCapabilities(sfTtsBaseUrl.value.trim());
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

    // Populate the dynamic TTS section from the live capabilities document
    // (plan M4). Runs after loadSettingsIntoForm on purpose: the saved
    // parameter values it re-applies come from the same response.
    await refreshTtsCapabilities();
}

function closeSettings() {
    settingsOverlay.classList.add("hidden");
}

/* ==========================================================================
   Dynamic TTS section (plan M4)
   ========================================================================== */

/**
 * Fetch the TTS server's capabilities document and (re)render the dynamic
 * parameter section. Every "nothing to show" state renders a status line
 * in the info area (renderTtsInfo with a null doc) so the user is never
 * left wondering why the parameter list is missing.
 *
 * @param {string} [probeUrl] - When given, probe THIS url (the ?base_url=
 *   endpoint parameter — it may be an unsaved edit the user just typed or
 *   is about to save). When omitted, use the plain endpoint, which serves
 *   the cached document for the SAVED base_url (the modal-open path: no
 *   extra round-trip to the same server).
 */
async function refreshTtsCapabilities(probeUrl) {
    const seq = ++ttsCapFetchSeq;

    if (!sfTtsEnabled.checked) {
        renderTtsUnavailable("TTS is not configured.");
        return;
    }
    if (!sfTtsBaseUrl.value.trim()) {
        renderTtsUnavailable("TTS Base URL is not set — parameter options unavailable.");
        return;
    }

    const fetchUrl = probeUrl
        ? `/api/tts/capabilities?base_url=${encodeURIComponent(probeUrl)}`
        : "/api/tts/capabilities";

    try {
        const resp = await fetch(fetchUrl);
        if (seq !== ttsCapFetchSeq) return; // superseded by a newer refresh
        if (!resp.ok) {
            // 422: a malformed probe URL (scheme-less typo) — the user can
            // fix the field right here, so say what is wrong specifically
            // instead of a generic "not reachable".
            if (resp.status === 422) {
                renderTtsUnavailable("TTS Base URL must start with http:// or https://.");
                return;
            }
            // 503: TTS inactive or the server has no /capabilities — the
            // detail string is server-side; this line is the modal's own.
            renderTtsUnavailable("TTS server not reachable — parameter options unavailable");
            return;
        }
        const doc = await resp.json();
        if (seq !== ttsCapFetchSeq) return;
        // The document belongs to the probed URL, or (no probe) to the
        // SAVED one — ttsSavedBaseUrl, not the field's current value.
        // Trailing slashes are stripped for the comparison: the backend
        // normalizes urls on save (clean_base_url), so "http://x/" is the
        // SAME engine as a saved "http://x" — a slash difference alone must
        // not read as an engine switch (which would discard in-flight
        // edits and show a false "switched" note).
        const newUrl = (probeUrl || ttsSavedBaseUrl).replace(/\/+$/, "");
        const prevDoc = ttsCapabilitiesDoc;
        const prevUrl = ttsCapabilitiesDocUrl;
        ttsCapabilitiesDoc = doc;
        ttsCapabilitiesDocUrl = newUrl;
        // Only a real engine change discards on-screen parameter edits
        // (the note says so); a same-URL refetch must not clobber edits
        // the user has in flight, so it re-applies what is on screen.
        // First render (no previous doc) re-applies the saved values.
        const engineSwitched = prevDoc !== null && prevUrl !== newUrl;
        const refillValues = engineSwitched
            ? ttsSavedParameters
            : prevDoc !== null
                ? collectTtsParamValues(sfTtsParams)
                : ttsSavedParameters;
        renderTtsInfo(
            doc,
            sfTtsInfo,
            engineSwitched ? "Engine switched — unsaved parameter edits were discarded." : null,
        );
        // The re-applied values cover whichever names this engine
        // advertises; names from another engine drop out naturally.
        renderTtsParameters(doc, sfTtsParams, refillValues);
    } catch (err) {
        if (seq !== ttsCapFetchSeq) return;
        console.error("Failed to load TTS capabilities:", err);
        renderTtsUnavailable("TTS server not reachable — parameter options unavailable");
    }
}

/**
 * Clear the dynamic section and show a single status line explaining why
 * there is nothing to render. Every "nothing to show" state (TTS off, no
 * URL, scheme typo, unreachable server, fetch failure) funnels through
 * here, so the section can never be left half-rendered and the user is
 * never left wondering why the parameter list is missing.
 */
function renderTtsUnavailable(message) {
    ttsCapabilitiesDoc = null;
    ttsCapabilitiesDocUrl = null;
    renderTtsParameters(null, sfTtsParams, {});
    renderTtsInfo(null, sfTtsInfo, message);
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

    // TTS (the dynamic parameter section is populated from the capabilities
    // document by refreshTtsCapabilities(); only the static fields here)
    sfTtsEnabled.checked = data.tts.enabled;
    sfTtsBaseUrl.value = data.tts.base_url || "";
    // The no-probe refresh path's document belongs to the SAVED url, and
    // the engine-switch note compares against it.
    ttsSavedBaseUrl = data.tts.base_url || "";
    ttsSavedParameters =
        data.tts.parameters && typeof data.tts.parameters === "object" ? data.tts.parameters : {};
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
        // This dialog doesn't edit general settings, so it sends none: the
        // backend treats `general` as a partial update (omitted fields keep
        // their current values). Carrying in-memory state here previously let
        // a stale copy clobber values the user changed in the General
        // Settings dialog (e.g. show_tool_calls).
        tts: {
            enabled: sfTtsEnabled.checked,
            base_url: sfTtsBaseUrl.value.trim(),
            timeout: parseFloat(sfTtsTimeout.value),
            streaming: sfTtsStreaming.checked,
            // Generic engine parameters (plan T1/M4): collected from the
            // rendered widgets; an empty object when nothing is rendered.
            // When no capabilities document is loaded (server down, TTS
            // off, no URL) there are no widgets to collect from, so the
            // last-saved values are passed through unchanged — silently
            // wiping them on an unrelated save is exactly the data-loss
            // class this modal has been burned by before (persona form).
            parameters: ttsCapabilitiesDoc
                ? collectTtsParamValues(sfTtsParams)
                : { ...ttsSavedParameters },
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
        // Generic parameter values are checked against the live capabilities
        // document (plan T7/T9): type, bounds, enum membership — immediate
        // feedback instead of a 422 round-trip. With no document loaded
        // there is nothing to check against; the server's own 422 is the
        // backstop (the old hard-coded 4–20 / 1.0–2.0 ranges are gone —
        // they described one engine's knobs and now belong to the engine).
        const paramError = validateTtsParamValues(data.tts.parameters, ttsCapabilitiesDoc);
        if (paramError) return paramError;
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
            return showSettingsError(extractApiErrorMessage(err, resp.status));
        }

        // Update in-memory TTS state based on new settings
        ttsStreaming = data.tts.streaming;
        // Refresh in-memory general settings from the server's authoritative
        // response (we no longer send them, so there is nothing to read back
        // from our own payload).
        const saved = await resp.json();
        if (saved.general != null) {
            personaNameMentionsEnabled = saved.general.persona_name_mentions;
            maxPersonaReplies = saved.general.max_persona_replies ?? 1;
            maxTurnsForContext = saved.general.max_turns_for_context ?? 6;
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
