"""Chat rooms router — CRUD for chat rooms and persona assignment.

Chat rooms let users group personas into logical collections. The implicit
"default" room always exists and contains all personas; it cannot be created,
edited, or deleted via this API.
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException

from app import persistence
from app.config import (
    ChatRoom,
    ChatRoomsConfig,
    STTLanguagePolicy,
    get_chatrooms,
    get_personas,
    is_valid_room_name,
    save_chatrooms,
)
from app.models import (
    AssignPersonasRequest,
    ChatRoomCreateRequest,
    ChatRoomResponse,
    EchoChamberRequest,
    STTLanguagePolicyRequest,
    STTLanguagePolicyResponse,
)
from app.session import session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chatrooms", tags=["chatrooms"])

DEFAULT_ROOM = "default"


def _to_response(room: ChatRoom) -> ChatRoomResponse:
    return ChatRoomResponse(
        name=room.name,
        persona_names=list(room.persona_names),
        echo_chamber=room.echo_chamber,
        stt_language_policy=(
            STTLanguagePolicyResponse(**room.stt_language_policy.model_dump())
            if room.stt_language_policy is not None
            else None
        ),
    )


@router.get("", response_model=List[ChatRoomResponse])
def list_chatrooms():
    """Return all configured chat rooms (excluding the implicit 'default')."""
    return [_to_response(r) for r in get_chatrooms().chat_rooms]


@router.get("/all", response_model=List[ChatRoomResponse])
def list_all_chatrooms():
    """Return all chat rooms including the implicit 'default'.
    Used by the frontend to populate the dropdown."""
    config = get_chatrooms()
    # "default" room always contains all configured personas
    all_persona_names = [p.name for p in get_personas().personas]
    result = [ChatRoomResponse(name=DEFAULT_ROOM, persona_names=all_persona_names, echo_chamber=False)]
    result.extend(_to_response(r) for r in config.chat_rooms)
    return result


@router.post("", response_model=ChatRoomResponse, status_code=201)
def create_chatroom(req: ChatRoomCreateRequest):
    """Create a new chat room.

    - Name is case-insensitive for uniqueness checks.
    - 'default' (or any case variation) is reserved and rejected.
    - New rooms start with zero personas assigned.
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Room name is required.")
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=409,
            detail=f"'{DEFAULT_ROOM}' is a reserved chat room name and cannot be created.",
        )
    if not is_valid_room_name(name):
        raise HTTPException(
            status_code=422,
            detail="Room name may only contain letters, numbers, spaces, hyphens, and underscores.",
        )

    config = get_chatrooms()
    if any(r.name.lower() == name.lower() for r in config.chat_rooms):
        raise HTTPException(
            status_code=409,
            detail=f"A chat room named '{name}' already exists.",
        )

    new_room = ChatRoom(name=name, persona_names=[])
    save_chatrooms(ChatRoomsConfig(chat_rooms=config.chat_rooms + [new_room]))
    return _to_response(new_room)


@router.delete("/{name}", status_code=204)
def delete_chatroom(name: str):
    """Delete a chat room. The 'default' room cannot be deleted.

    Deleting a room also removes its persisted history and audio: the
    persistence directory is named after the room, so leaving it behind
    would let a re-created room with the same name resurrect the deleted
    room's conversation. When the deleted room was the session's active
    room, the session is reset to 'default' — otherwise it would keep
    pointing at a room that no longer exists, and the next message would
    recreate the deleted room's directory behind the user's back.
    """
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be deleted.",
        )

    config = get_chatrooms()
    # The canonical room name (from the config) is what names the
    # persistence directory on disk — the URL spelling may differ in case,
    # and directory names are case-sensitive.
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")

    # The YAML save goes first: if it fails, nothing else has been touched
    # and the room is still fully intact on both sides.
    save_chatrooms(
        ChatRoomsConfig(
            chat_rooms=[r for r in config.chat_rooms if r.name.lower() != room.name.lower()]
        )
    )

    persistence.delete_room(room.name)

    if session.current_room.lower() == room.name.lower():
        # Mirrors a normal room switch: point the session at 'default' and
        # load that room's persisted history into memory.
        session.load_room(DEFAULT_ROOM)


@router.get("/{name}", response_model=ChatRoomResponse)
def get_chatroom(name: str):
    """Return a specific chat room's details, including 'default'."""
    if name.lower() == DEFAULT_ROOM:
        all_persona_names = [p.name for p in get_personas().personas]
        return ChatRoomResponse(name=DEFAULT_ROOM, persona_names=all_persona_names, echo_chamber=False)

    config = get_chatrooms()
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if not room:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")
    return _to_response(room)


@router.put("/{name}/personas", response_model=ChatRoomResponse)
def assign_personas(name: str, req: AssignPersonasRequest):
    """Add personas to a chat room. Cannot modify the 'default' room."""
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be modified.",
        )

    config = get_chatrooms()
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if not room:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")

    # Validate that requested personas actually exist
    valid_names = {p.name for p in get_personas().personas}
    for pname in req.persona_names:
        if pname not in valid_names:
            raise HTTPException(
                status_code=422,
                detail=f"Persona '{pname}' does not exist.",
            )

    # Add new personas (avoid duplicates, preserving existing order)
    updated_names = list(room.persona_names)
    for pname in req.persona_names:
        if pname not in updated_names:
            updated_names.append(pname)

    updated_room = room.model_copy(update={"persona_names": updated_names})
    updated_rooms = [updated_room if r.name.lower() == room.name.lower() else r for r in config.chat_rooms]
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated_rooms))
    return _to_response(updated_room)


@router.delete("/{name}/personas/{persona_name}", response_model=ChatRoomResponse)
def remove_persona_from_room(name: str, persona_name: str):
    """Remove a persona from a chat room. Cannot modify the 'default' room."""
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be modified.",
        )

    config = get_chatrooms()
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if not room:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")

    updated_names = [p for p in room.persona_names if p != persona_name]
    updated_room = room.model_copy(update={"persona_names": updated_names})
    updated_rooms = [updated_room if r.name.lower() == room.name.lower() else r for r in config.chat_rooms]
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated_rooms))
    return _to_response(updated_room)


@router.put("/{name}/echo-chamber", response_model=ChatRoomResponse)
def set_echo_chamber(name: str, req: EchoChamberRequest):
    """Set the echo chamber flag for a chat room. Cannot modify the 'default' room."""
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be modified.",
        )
    config = get_chatrooms()
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if not room:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")
    updated_room = room.model_copy(update={"echo_chamber": req.echo_chamber})
    updated_rooms = [updated_room if r.name.lower() == room.name.lower() else r for r in config.chat_rooms]
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated_rooms))
    return _to_response(updated_room)


@router.put("/{name}/stt-language-policy", response_model=ChatRoomResponse)
def set_stt_language_policy(name: str, req: STTLanguagePolicyRequest):
    """Set a room-level STT language policy override. Cannot modify the 'default' room.

    Replaces any existing override wholesale (no field-by-field merging) —
    the same full-replacement stance as the global stt: settings section.
    """
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be modified.",
        )
    config = get_chatrooms()
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if not room:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")
    policy = STTLanguagePolicy(**req.model_dump())
    updated_room = room.model_copy(update={"stt_language_policy": policy})
    updated_rooms = [updated_room if r.name.lower() == room.name.lower() else r for r in config.chat_rooms]
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated_rooms))
    return _to_response(updated_room)


@router.delete("/{name}/stt-language-policy", response_model=ChatRoomResponse)
def clear_stt_language_policy(name: str):
    """Clear a room's STT language policy override (revert to inheriting global). Cannot modify the 'default' room."""
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be modified.",
        )
    config = get_chatrooms()
    room = next((r for r in config.chat_rooms if r.name.lower() == name.lower()), None)
    if not room:
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")
    updated_room = room.model_copy(update={"stt_language_policy": None})
    updated_rooms = [updated_room if r.name.lower() == room.name.lower() else r for r in config.chat_rooms]
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated_rooms))
    return _to_response(updated_room)
