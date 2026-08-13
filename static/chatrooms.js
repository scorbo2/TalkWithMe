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
}

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

    // Only show non-default rooms
    const rooms = allChatRooms.filter(r => r.name !== "default");

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
            crFormError.textContent = err.detail || `Error ${resp.status}`;
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

function confirmDeleteChatRoom(name) {
    crConfirmMsg.textContent = `Delete chat room "${name}"? Personas will not be deleted, only unassigned from this room.`;
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
