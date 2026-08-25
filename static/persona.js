/**
 * persona.js — Persona sidebar list rendering and persona editor (CRUD).
 *
 * Handles:
 *  - Rendering the persona cards in the sidebar
 *  - The persona editor modal: list, create, edit, clone, delete
 */

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
   Persona Editor — event listeners
   ========================================================================== */

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

/* ==========================================================================
   Persona Editor — modal lifecycle
   ========================================================================== */

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

/* ==========================================================================
   Persona Editor — list rendering
   ========================================================================== */

async function renderPersonaEditorList() {
    try {
        const resp = await fetch("/api/personas");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
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

/* ==========================================================================
   Persona Editor — form (create / edit)
   ========================================================================== */

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
            pfReferenceAudioLanguage.value = p.reference_audio_language || "en";
            pfAvatarImage.value       = p.avatar_image || "";
            pfReferenceAudio.value    = p.reference_audio || "";
            pfReferenceAudioTx.value  = p.reference_audio_transcript || "";
            pfAllowToolCalls.checked  = p.allow_tool_calls ?? false;
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
        pfAvatarImage.value       = "";
        pfReferenceAudio.value    = "";
        pfReferenceAudioTx.value  = "";
        pfAllowToolCalls.checked  = false;
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
    const referenceAudioLanguage = pfReferenceAudioLanguage.value.trim();
    const avatarImage  = pfAvatarImage.value.trim();
    const refAudio     = pfReferenceAudio.value.trim();
    const refAudioTx   = pfReferenceAudioTx.value.trim();

    if (!name) return showPersonaFormError("Name is required.");
    if (name.toLowerCase() === "user") return showPersonaFormError("'user' is a reserved name and cannot be used.");
    if (!systemPrompt) return showPersonaFormError("System prompt is required.");
    if (!routerHints) return showPersonaFormError("Router hints are required.");
    if (referenceAudioLanguage.length !== 2) return showPersonaFormError("Reference audio language must be a 2-letter code.");

    const payload = {
        name,
        description,
        system_prompt: systemPrompt,
        router_hints: routerHints,
        avatar_color: avatarColor,
        reference_audio_language: referenceAudioLanguage,
        avatar_image: avatarImage || null,
        reference_audio: refAudio || null,
        reference_audio_transcript: refAudioTx || null,
        allow_tool_calls: pfAllowToolCalls.checked,
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
            return showPersonaFormError(extractApiErrorMessage(err, resp.status));
        }

        // Refresh sidebar persona list
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
