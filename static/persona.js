/**
 * persona.js — Persona sidebar list rendering and persona editor (CRUD).
 *
 * Handles:
 *  - Rendering the persona cards in the sidebar
 *  - The persona editor modal: list, create, edit, clone, delete
 *
 * The editor form is multipart/form-data: text fields plus file uploads
 * (avatar image, reference audio) are submitted together, so the server
 * stores everything in the persona's directory in one request.
 */

/* ==========================================================================
    Persona Editor — form-private state
    ========================================================================== */

// Whether the persona being edited has an avatar / reference audio on the
// server when the form opens. Drives the previews and Remove-button
// visibility (see renderPersonaAvatarPreview / updatePersonaAudioControls).
let peAvatarOnServer = false;
let peAudioOnServer = false;

// Whether the user has explicitly clicked "Remove" for the avatar /
// reference audio since the form opened (or since the last file selection).
// This is the ONLY thing that may set the remove_* flags on submit. They
// must never be derived from peAvatarOnServer / peAudioOnServer: that would
// silently delete a persona's avatar / reference audio on every plain text
// save (the original bug behind the remove-requested split).
let peAvatarRemoveRequested = false;
let peAudioRemoveRequested = false;

// Object URLs + Audio element for the in-form previews. Reused across
// plays; revoked and stopped when the form closes.
let peAvatarObjectUrl = null;
let peAudioObjectUrl = null;
let pePreviewAudio = null;

/* ==========================================================================
   Sidebar persona list rendering
   ========================================================================== */

/**
 * Render the persona list in the sidebar.
 * @param {Array} [list] - Optional filtered list. If omitted, uses all personas.
 * @param {boolean} [showRemoveButtons] - Whether to show the remove "x" button per persona.
 */
function renderPersonaList(list, showRemoveButtons) {
    personaListEl.innerHTML = "";
    // Always render alphabetically (case-insensitive), regardless of the
    // order the caller's list happens to be in.
    const personaList = [...(list || personas)].sort(comparePersonasByName);
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
   Persona Editor — event listeners
   ========================================================================== */

document.getElementById("btn-persona-editor").addEventListener("click", openPersonaEditor);
document.getElementById("pe-btn-close").addEventListener("click", closePersonaEditor);
document.getElementById("pe-btn-new").addEventListener("click", () => openPersonaForm(null));
document.getElementById("pe-form-btn-cancel").addEventListener("click", showPersonaList);
document.getElementById("pe-form-btn-cancel2").addEventListener("click", showPersonaList);
peForm.addEventListener("submit", submitPersonaForm);
pfAvatarImage.addEventListener("change", onPersonaAvatarFileSelected);
pfAvatarRemoveBtn.addEventListener("click", () => resetPersonaAvatarField());
pfReferenceAudio.addEventListener("change", onPersonaAudioFileSelected);
pfAudioRemoveBtn.addEventListener("click", () => resetPersonaAudioField());
pfAudioPlayBtn.addEventListener("click", playPersonaReferenceAudio);
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

/* ==========================================================================
   Persona Editor — modal lifecycle
   ========================================================================== */

function openPersonaEditor() {
    personaEditorOverlay.classList.remove("hidden");
    showPersonaList();
}

function closePersonaEditor() {
    stopPersonaPreviewAudio();
    personaEditorOverlay.classList.add("hidden");
}

function showPersonaList() {
    stopPersonaPreviewAudio();
    peListView.classList.remove("hidden");
    peFormView.classList.add("hidden");
    renderPersonaEditorList();
}

/* ==========================================================================
   Persona Editor — list rendering
   ========================================================================== */

async function renderPersonaEditorList() {
    try {
        const resp = await fetch("/api/personas");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        // Alphabetical (case-insensitive), not YAML/creation order.
        const list = (await resp.json()).sort(comparePersonasByName);
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

/* ==========================================================================
   Persona Editor — form (create / edit)
   ========================================================================== */

async function openPersonaForm(name) {
    peEditingName = name;
    peFormError.classList.add("hidden");
    peFormError.textContent = "";

    // Always start from a clean slate: stop any preview playback and clear
    // the file inputs (setting value="" is the only way to reset them).
    stopPersonaPreviewAudio();
    pfAvatarImage.value = "";
    pfReferenceAudio.value = "";
    peAvatarOnServer = false;
    peAudioOnServer = false;
    peAvatarRemoveRequested = false;
    peAudioRemoveRequested = false;

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
            pfReferenceAudioLanguage.value = p.reference_audio_language || "en";
            pfReferenceAudioTx.value  = p.reference_audio_transcript || "";
            pfAllowToolCalls.checked  = p.allow_tool_calls ?? false;
            // avatar_image / reference_audio are now presence flags; the
            // actual files are previewed via their dedicated endpoints.
            peAvatarOnServer = !!p.avatar_image;
            peAudioOnServer  = !!p.reference_audio;
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
        pfReferenceAudioLanguage.value = "en";
        pfReferenceAudioTx.value  = "";
        pfAllowToolCalls.checked  = false;
    }

    renderPersonaAvatarPreview();
    updatePersonaAudioControls();

    peListView.classList.add("hidden");
    peFormView.classList.remove("hidden");
    pfName.focus();
}

/* ==========================================================================
    Persona Editor — file fields (avatar image, reference audio)
    ========================================================================== */

/**
 * Rebuild the avatar preview circle: the selected file's object URL if
 * one is chosen, the server's avatar otherwise, or the initial as a
 * fallback.
 */
function renderPersonaAvatarPreview() {
    if (peAvatarObjectUrl) {
        URL.revokeObjectURL(peAvatarObjectUrl);
        peAvatarObjectUrl = null;
    }
    const file = pfAvatarImage.files[0];
    if (file) {
        peAvatarObjectUrl = URL.createObjectURL(file);
        pfAvatarPreview.innerHTML = "";
        const img = document.createElement("img");
        img.src = peAvatarObjectUrl;
        img.alt = "Avatar preview";
        pfAvatarPreview.appendChild(img);
    } else if (peAvatarOnServer && !peAvatarRemoveRequested) {
        const img = document.createElement("img");
        img.src = `/api/personas/${encodeURIComponent(peEditingName)}/avatar`;
        img.alt = "Current avatar";
        img.onerror = () => {
            pfAvatarPreview.innerHTML = "";
            pfAvatarPreview.textContent = peEditingName.charAt(0).toUpperCase();
        };
        pfAvatarPreview.innerHTML = "";
        pfAvatarPreview.appendChild(img);
    } else {
        pfAvatarPreview.innerHTML = "";
        pfAvatarPreview.textContent = (peEditingName || "?").charAt(0).toUpperCase();
    }
    // Remove makes sense when there is anything left to remove: a selected
    // file, or a server file that has not been marked for removal. (Once a
    // removal is pending there is nothing left to remove — the preview
    // already shows the post-save state.)
    pfAvatarRemoveBtn.classList.toggle(
        "hidden",
        !file && !(peAvatarOnServer && !peAvatarRemoveRequested)
    );
}

function onPersonaAvatarFileSelected() {
    // Picking a file supersedes any pending removal request.
    peAvatarRemoveRequested = false;
    renderPersonaAvatarPreview();
}

function resetPersonaAvatarField() {
    pfAvatarImage.value = "";
    peAvatarRemoveRequested = true;
    renderPersonaAvatarPreview();
}

/**
 * Refresh the status text and Play/Remove visibility for the reference
 * audio field based on what is currently selected / on the server.
 */
function updatePersonaAudioControls() {
    const file = pfReferenceAudio.files[0];
    // A file marked for removal is no longer "current" — show the post-save
    // state so the UI never lies about what will happen on submit.
    const audioOnServerKept = peAudioOnServer && !peAudioRemoveRequested;
    if (file) {
        pfAudioStatus.textContent = `New file: ${file.name}`;
    } else if (audioOnServerKept) {
        pfAudioStatus.textContent = "Current file on server";
    } else {
        pfAudioStatus.textContent = "None";
    }
    // Same rule as the avatar Remove button: only show controls when there
    // is something left to play / remove.
    pfAudioPlayBtn.classList.toggle("hidden", !file && !audioOnServerKept);
    pfAudioRemoveBtn.classList.toggle("hidden", !file && !audioOnServerKept);
}

function onPersonaAudioFileSelected() {
    // Picking a file supersedes any pending removal request.
    peAudioRemoveRequested = false;
    updatePersonaAudioControls();
}

function resetPersonaAudioField() {
    pfReferenceAudio.value = "";
    peAudioRemoveRequested = true;
    stopPersonaPreviewAudio();
    updatePersonaAudioControls();
}

/**
 * Play the reference audio: the freshly selected file if there is one,
 * otherwise the file currently on the server. A single Audio element is
 * reused so starting a new playback stops the previous one.
 */
async function playPersonaReferenceAudio() {
    stopPersonaPreviewAudio();
    const file = pfReferenceAudio.files[0];
    if (file) {
        peAudioObjectUrl = URL.createObjectURL(file);
        const audio = new Audio(peAudioObjectUrl);
        audio.onended = () => stopPersonaPreviewAudio();
        audio.onerror = () => {
            pfAudioStatus.textContent = "Playback failed";
            stopPersonaPreviewAudio();
        };
        await audio.play().catch(() => {
            pfAudioStatus.textContent = "Playback failed";
            stopPersonaPreviewAudio();
        });
        pePreviewAudio = audio;
        return;
    }
    if (!peAudioOnServer) return;
    try {
        const resp = await fetch(`/api/personas/${encodeURIComponent(peEditingName)}/reference-audio`);
        if (!resp.ok) {
            pfAudioStatus.textContent = "Playback failed";
            return;
        }
        const blob = await resp.blob();
        peAudioObjectUrl = URL.createObjectURL(blob);
        const audio = new Audio(peAudioObjectUrl);
        audio.onended = () => stopPersonaPreviewAudio();
        audio.onerror = () => {
            pfAudioStatus.textContent = "Playback failed";
            stopPersonaPreviewAudio();
        };
        await audio.play().catch(() => {
            pfAudioStatus.textContent = "Playback failed";
            stopPersonaPreviewAudio();
        });
        pePreviewAudio = audio;
    } catch (err) {
        console.error("Failed to fetch reference audio:", err);
        pfAudioStatus.textContent = "Playback failed";
    }
}

function stopPersonaPreviewAudio() {
    if (pePreviewAudio) {
        pePreviewAudio.pause();
        pePreviewAudio = null;
    }
    if (peAudioObjectUrl) {
        URL.revokeObjectURL(peAudioObjectUrl);
        peAudioObjectUrl = null;
    }
}

async function submitPersonaForm(e) {
    e.preventDefault();
    peFormError.classList.add("hidden");

    const name         = pfName.value.trim();
    const description  = pfDescription.value.trim();
    const systemPrompt = pfSystemPrompt.value.trim();
    const routerHints  = pfRouterHints.value.trim();
    const avatarColor  = pfAvatarColor.value;
    const referenceAudioLanguage = pfReferenceAudioLanguage.value.trim();

    if (!name) return showPersonaFormError("Name is required.");
    if (name.toLowerCase() === "user") return showPersonaFormError("'user' is a reserved name and cannot be used.");
    if (!systemPrompt) return showPersonaFormError("System prompt is required.");
    if (!routerHints) return showPersonaFormError("Router hints are required.");
    if (referenceAudioLanguage.length !== 2) return showPersonaFormError("Reference audio language must be a 2-letter code.");

    // Multipart: text fields + the chosen files in one request. The remove_*
    // flags are sent ONLY for an explicit "Remove" click (see the
    // peAvatarRemoveRequested / peAudioRemoveRequested flags) — never just
    // because a file exists on the server. That is what made every plain
    // text save silently delete the persona's avatar and reference audio.
    const form = new FormData();
    form.append("name", name);
    form.append("description", description);
    form.append("system_prompt", systemPrompt);
    form.append("router_hints", routerHints);
    form.append("avatar_color", avatarColor);
    form.append("reference_audio_language", referenceAudioLanguage);
    form.append("allow_tool_calls", String(pfAllowToolCalls.checked));
    form.append("reference_audio_transcript", pfReferenceAudioTx.value.trim());

    const avatarFile = pfAvatarImage.files[0];
    if (avatarFile) {
        form.append("avatar_image", avatarFile);
    } else if (peAvatarRemoveRequested) {
        form.append("remove_avatar_image", "true");
    }

    const audioFile = pfReferenceAudio.files[0];
    if (audioFile) {
        form.append("reference_audio", audioFile);
    } else if (peAudioRemoveRequested) {
        form.append("remove_reference_audio", "true");
    }

    try {
        let resp;
        if (peEditingName) {
            resp = await fetch(`/api/personas/${encodeURIComponent(peEditingName)}`, {
                method: "PUT",
                body: form,
            });
        } else {
            resp = await fetch("/api/personas", {
                method: "POST",
                body: form,
            });
        }

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            showPersonaFormError(extractApiErrorMessage(err, resp.status));
            return;
        }

        // Refresh sidebar persona list (showPersonaList stops previews too)
        await loadPersonas();
        showPersonaList();
    } catch (err) {
        showPersonaFormError("Request failed. Is the server running?");
    }
}

/* ==========================================================================
   Persona Editor — clone / delete
   ========================================================================== */

async function clonePersona(name) {
    try {
        const resp = await fetch(`/api/personas/${encodeURIComponent(name)}/clone`, { method: "POST" });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            console.error("Clone failed:", extractApiErrorMessage(err, resp.status));
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
