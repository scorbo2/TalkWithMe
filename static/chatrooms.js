/**
 * chatrooms.js — Chat room management: CRUD, persona picker, room switching.
 *
 * Handles:
 *  - Loading and rendering the chat room dropdown
 *  - Filtering personas by room
 *  - Chat room editor modal (create, delete rooms)
 *  - Persona picker modal (add personas to a room)
 *  - Removing personas from rooms
 */

/* ==========================================================================
   Chat room loading and filtering
   ========================================================================== */

/**
 * Load all chat rooms from the server and initialize the room state.
 * After loading, applies the current room filter and renders the persona list.
 */
async function loadChatRooms() {
    try {
        const resp = await fetch("/api/chatrooms/all");
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
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

    // Update echo chamber checkbox state (case-insensitive, matching backend behavior)
    const roomInfo = allChatRooms.find(r => r.name.toLowerCase() === currentChatRoom.toLowerCase());
    const echoEnabled = roomInfo ? roomInfo.echo_chamber : false;
    echoChamberToggle.checked = echoEnabled;
    echoChamberToggle.disabled = !isActiveRoom;

    // Update the per-room STT language override controls from the room's
    // stt_language_policy (null = inheriting the global default).
    const policy = roomInfo ? roomInfo.stt_language_policy : null;
    roomSttMode.value = policy ? policy.mode : "";
    roomSttPrimaryLanguage.value = policy ? (policy.primary_language || "") : "";
    roomSttFallbackLanguage.value = policy ? (policy.fallback_language || "") : "";
    roomSttFallbackThreshold.value = policy ? (policy.fallback_threshold ?? 0.80) : 0.80;
    updateRoomSttControlAvailability();
    updateRoomSttFieldsVisibility();
}

/**
 * Show only the room-STT-override fields relevant to the selected mode
 * (mirrors the global settings form's updateSttLanguageModeFieldsState).
 */
function updateRoomSttFieldsVisibility() {
    const mode = roomSttMode.value;
    roomSttPrimaryRow.classList.toggle("hidden", mode === "" || mode === "auto");
    roomSttFallbackRow.classList.toggle("hidden", mode !== "primary_fallback");
}

/**
 * Disable the per-room STT language controls when there's nothing for them
 * to do: the "default" room (never overridable) or STT not usable globally
 * (disabled in settings, or unreachable — sttAvailable, set by
 * checkSTTHealth() in app.js, already reuses that same flag for the mic
 * button). Only `.disabled` changes here — the room's saved override is
 * never read from the server again or altered, and disabling via JS does
 * not fire a "change" event, so no PUT/DELETE request is sent.
 */
function updateRoomSttControlAvailability() {
    const disabled = currentChatRoom === "default" || !sttAvailable;
    roomSttMode.disabled = disabled;
    roomSttPrimaryLanguage.disabled = disabled;
    roomSttFallbackLanguage.disabled = disabled;
    roomSttFallbackThreshold.disabled = disabled;
}

/**
 * Persist the room's STT language override: DELETE when reverting to
 * "Use global default", otherwise PUT the full policy (full replacement,
 * same as the global stt: settings section).
 */
async function updateRoomSttPolicy() {
    if (currentChatRoom === "default") return;
    const mode = roomSttMode.value;

    try {
        let resp;
        if (mode === "") {
            resp = await fetch(`/api/chatrooms/${encodeURIComponent(currentChatRoom)}/stt-language-policy`, {
                method: "DELETE",
            });
        } else {
            resp = await fetch(`/api/chatrooms/${encodeURIComponent(currentChatRoom)}/stt-language-policy`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    mode,
                    primary_language: roomSttPrimaryLanguage.value.trim() || null,
                    fallback_language: roomSttFallbackLanguage.value.trim() || null,
                    fallback_threshold: parseFloat(roomSttFallbackThreshold.value) || 0.80,
                }),
            });
        }
        if (!resp.ok) {
            console.error("Failed to update room STT language policy:", resp.status);
            return;
        }
        // Reload so allChatRooms (and thus roomInfo above) reflects the save.
        await loadChatRooms();
    } catch (err) {
        console.error("Update room STT language policy error:", err);
    }
}

/**
 * Populate the chat room dropdown from the server's full list.
 */
function renderChatRoomDropdown() {
    chatRoomDropdown.innerHTML = "";
    // "default" (All Personas) always first; remainder sorted alphabetically, case-insensitive
    const sorted = [...allChatRooms].sort((a, b) => {
        if (a.name === "default") return -1;
        if (b.name === "default") return 1;
        return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    });
    for (const room of sorted) {
        const opt = document.createElement("option");
        opt.value = room.name;
        opt.textContent = room.name === "default" ? "All Personas" : room.name;
        chatRoomDropdown.appendChild(opt);
    }
}

/* ==========================================================================
   Event listeners
   ========================================================================== */

function setupChatRoomEventListeners() {
    // Dropdown change: switch rooms
    chatRoomDropdown.addEventListener("change", () => {
        switchChatRoom(chatRoomDropdown.value);
    });

    // Echo chamber toggle
    echoChamberToggle.addEventListener("change", () => {
        updateEchoChamber(currentChatRoom, echoChamberToggle.checked);
    });

    // Per-room STT language override
    roomSttMode.addEventListener("change", () => {
        updateRoomSttFieldsVisibility();
        updateRoomSttPolicy();
    });
    roomSttPrimaryLanguage.addEventListener("change", updateRoomSttPolicy);
    roomSttFallbackLanguage.addEventListener("change", updateRoomSttPolicy);
    roomSttFallbackThreshold.addEventListener("change", updateRoomSttPolicy);

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
        crConfirmOverlay.classList.add("hidden");
    });

    // Backdrop click to close
    chatroomsOverlay.addEventListener("click", (e) => {
        if (e.target === chatroomsOverlay) closeChatRoomsEditor();
    });
    crConfirmOverlay.addEventListener("click", (e) => {
        if (e.target === crConfirmOverlay) {
            crConfirmOverlay.classList.add("hidden");
        }
    });

    // Persona picker
    document.getElementById("pp-btn-close").addEventListener("click", closePersonaPicker);
    document.getElementById("pp-btn-cancel").addEventListener("click", closePersonaPicker);
    document.getElementById("pp-btn-add").addEventListener("click", addSelectedPersonasToRoom);
    personaPickerOverlay.addEventListener("click", (e) => {
        if (e.target === personaPickerOverlay) closePersonaPicker();
    });
}

/* ==========================================================================
   Room switching
   ========================================================================== */

/**
 * Switch to a different chat room. Clears the chat display, loads the
 * persisted history for the new room, and updates the persona list.
 */
async function switchChatRoom(roomName) {
    currentChatRoom = roomName;

    // Clear chat panel momentarily
    messagesEl.innerHTML = "";
    showEmptyState();

    // Load persisted history for this room (also resets the backend session)
    const history = await loadPersistedHistory(roomName);
    renderPersistedHistory(history.messages, roomName);

    // Re-apply filter
    applyChatRoomFilter();
}

/**
 * Persist echo chamber toggle for the current chat room.
 */
async function updateEchoChamber(roomName, enabled) {
    if (roomName === "default") {
        // Default room cannot be modified
        echoChamberToggle.checked = false;
        return;
    }
    // Skip no-op to avoid unnecessary PUTs (and handle case-insensitive room matching)
    const currentRoom = allChatRooms.find(r => r.name.toLowerCase() === roomName.toLowerCase());
    if (currentRoom && currentRoom.echo_chamber === enabled) {
        return;
    }
    try {
        const resp = await fetch(`/api/chatrooms/${encodeURIComponent(roomName)}/echo-chamber`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ echo_chamber: enabled }),
        });
        if (!resp.ok) {
            console.error("Failed to update echo chamber:", resp.status);
            // Revert UI on failure
            echoChamberToggle.checked = !enabled;
            return;
        }
        // Reload from server to sync all state (persona lists, echo flag, etc.)
        await loadChatRooms();
    } catch (err) {
        console.error("Update echo chamber error:", err);
        echoChamberToggle.checked = !enabled;
    }
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
        // If the removed persona was selected, clear the selection so
        // applyChatRoomFilter() re-picks the first (alphabetical) persona
        // from the rendered list — picking from roomNames[0] here would
        // follow room-assignment order and could highlight a middle card.
        if (selectedPersona === personaName) {
            selectedPersona = null;
        }
        applyChatRoomFilter();
    } catch (err) {
        console.error("Remove persona from room error:", err);
    }
}

/* ==========================================================================
   Chat Rooms Editor Modal
   ========================================================================== */

function openChatRoomsEditor() {
    hideNewRoomForm();
    chatroomsOverlay.classList.remove("hidden");
    crFormError.classList.add("hidden");
    renderChatRoomList();
}

function closeChatRoomsEditor() {
    chatroomsOverlay.classList.add("hidden");
    // Refresh the dropdown and re-apply room filter (in case rooms were deleted)
    loadChatRooms();
}

function renderChatRoomList() {
    crListEl.innerHTML = "";

    // Only show non-default rooms, sorted alphabetically (case-insensitive)
    const rooms = allChatRooms
        .filter(r => r.name !== "default")
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));

    if (rooms.length === 0) {
        crListEl.innerHTML = '<p class="cr-empty">No chat rooms yet. Click &ldquo;+ New Room&rdquo; to create one.</p>';
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
        crListEl.appendChild(item);
    }
}

function showNewRoomForm() {
    crNewForm.classList.remove("hidden");
    crListEl.classList.add("hidden");
    crNameInput.value = "";
    crFormError.classList.add("hidden");
    crNameInput.focus();
    // Allow Enter key to create the room
    crNameInput.onkeydown = (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            createChatRoom();
        }
    };
}

function hideNewRoomForm() {
    crNewForm.classList.add("hidden");
    crListEl.classList.remove("hidden");
}

async function createChatRoom() {
    const name = crNameInput.value.trim();

    if (!name) {
        crFormError.textContent = "Room name is required.";
        crFormError.classList.remove("hidden");
        return;
    }
    if (name.length > 20) {
        crFormError.textContent = "Room name must be 20 characters or fewer.";
        crFormError.classList.remove("hidden");
        return;
    }
    if (name.toLowerCase() === "default") {
        crFormError.textContent = "'default' is a reserved name and cannot be used.";
        crFormError.classList.remove("hidden");
        return;
    }
    if (!/^[a-zA-Z0-9 _-]+$/.test(name)) {
        crFormError.textContent = "Name may only contain letters, numbers, spaces, hyphens, and underscores.";
        crFormError.classList.remove("hidden");
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
            crFormError.textContent = extractApiErrorMessage(err, resp.status);
            crFormError.classList.remove("hidden");
            return;
        }

        hideNewRoomForm();
        // Reload rooms to refresh the list
        await loadChatRooms();
        renderChatRoomList();
    } catch (err) {
        crFormError.textContent = "Request failed. Is the server running?";
        crFormError.classList.remove("hidden");
    }
}

async function confirmDeleteChatRoom(name) {
    // Warn about the persisted conversation only when the room actually
    // has one. The count comes from a read-only endpoint — loadPersistedHistory
    // would switch the backend session to the room being deleted. If the
    // count can't be determined, warn anyway: a missed warning about
    // deleted data is worse than a redundant one.
    const count = await getRoomMessageCount(name);
    let message = `Delete chat room "${name}"? Personas will not be deleted, only unassigned from this room.`;
    if (count === null || count > 0) {
        const detail = count !== null ? ` (${count} message${count === 1 ? "" : "s"})` : "";
        message += ` The room's saved conversation history and audio${detail} will be permanently deleted.`;
    }
    crConfirmMsg.textContent = message;
    crConfirmOverlay.classList.remove("hidden");

    const deleteBtn = document.getElementById("cr-confirm-delete");
    const newBtn = deleteBtn.cloneNode(true);
    deleteBtn.parentNode.replaceChild(newBtn, deleteBtn);
    newBtn.addEventListener("click", () => deleteChatRoom(name));
}

async function deleteChatRoom(name) {
    crConfirmOverlay.classList.add("hidden");
    try {
        const resp = await fetch(`/api/chatrooms/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!resp.ok) {
            console.error("Delete chat room failed:", resp.status);
            return;
        }
        // loadChatRooms() below reverts currentChatRoom to "default" on its
        // own once the room vanishes from the list, but that only updates
        // the dropdown — the chat panel would keep showing the deleted
        // room's messages. switchChatRoom() does the full job: clear the
        // panel, load default's persisted history (which also resets the
        // backend session), and re-apply the room filter.
        const deletedActiveRoom = currentChatRoom.toLowerCase() === name.toLowerCase();
        await loadChatRooms();
        if (deletedActiveRoom) {
            await switchChatRoom("default");
        }
        renderChatRoomList();
    } catch (err) {
        console.error("Delete chat room error:", err);
    }
}

/* ==========================================================================
   Persona Picker Modal (for adding personas to a chat room)
   ========================================================================== */

function openPersonaPicker() {
    if (currentChatRoom === "default") return;

    ppSelectedNames = [];
    personaPickerOverlay.classList.remove("hidden");
    renderPersonaPickerList();
}

function closePersonaPicker() {
    personaPickerOverlay.classList.add("hidden");
}

function renderPersonaPickerList() {
    ppListEl.innerHTML = "";

    // Get personas already in this room
    const alreadyInRoom = new Set(roomPersonas[currentChatRoom] || []);

    if (personas.length === 0) {
        ppListEl.innerHTML = '<p class="pp-empty">No personas configured.</p>';
        return;
    }

    // Render alphabetically (case-insensitive), not in creation order.
    const sorted = [...personas].sort(comparePersonasByName);
    for (const p of sorted) {
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
        descEl.textContent = alreadyInRoom.has(p.name) ? (p.description || "") + " (already in room)" : (p.description || "");

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

        ppListEl.appendChild(item);
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
            console.error("Failed to add personas to room:", extractApiErrorMessage(err, resp.status));
            return;
        }

        closePersonaPicker();
        // Reload to refresh the persona list
        await loadChatRooms();
    } catch (err) {
        console.error("Add personas to room error:", err);
    }
}
