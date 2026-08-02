"""Chat rooms router — CRUD for chat rooms and persona assignment.

Chat rooms let users group personas into logical collections. The implicit
"default" room always exists and contains all personas; it cannot be created,
edited, or deleted via this API.
"""

import logging
import re
from typing import List

from fastapi import APIRouter, HTTPException

from app.config import (
    ChatRoom,
    ChatRoomsConfig,
    get_chatrooms,
    get_personas,
    save_chatrooms,
)
from app.models import (
    AssignPersonasRequest,
    ChatRoomCreateRequest,
    ChatRoomResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chatrooms", tags=["chatrooms"])

DEFAULT_ROOM = "default"


def _to_response(room: ChatRoom) -> ChatRoomResponse:
    return ChatRoomResponse(name=room.name, persona_names=list(room.persona_names))


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
    result = [ChatRoomResponse(name=DEFAULT_ROOM, persona_names=all_persona_names)]
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
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(
            status_code=422,
            detail="Room name may only contain letters, numbers, hyphens, and underscores.",
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
    """Delete a chat room. The 'default' room cannot be deleted."""
    if name.lower() == DEFAULT_ROOM:
        raise HTTPException(
            status_code=400,
            detail=f"The '{DEFAULT_ROOM}' chat room cannot be deleted.",
        )

    config = get_chatrooms()
    if not any(r.name.lower() == name.lower() for r in config.chat_rooms):
        raise HTTPException(status_code=404, detail=f"Chat room '{name}' not found.")

    save_chatrooms(
        ChatRoomsConfig(
            chat_rooms=[r for r in config.chat_rooms if r.name.lower() != name.lower()]
        )
    )


@router.get("/{name}", response_model=ChatRoomResponse)
def get_chatroom(name: str):
    """Return a specific chat room's details, including 'default'."""
    if name.lower() == DEFAULT_ROOM:
        all_persona_names = [p.name for p in get_personas().personas]
        return ChatRoomResponse(name=DEFAULT_ROOM, persona_names=all_persona_names)

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

    updated_room = ChatRoom(name=room.name, persona_names=updated_names)
    updated_rooms = [updated_room if r.name == room.name else r for r in config.chat_rooms]
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
    updated_room = ChatRoom(name=room.name, persona_names=updated_names)
    updated_rooms = [updated_room if r.name == room.name else r for r in config.chat_rooms]
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated_rooms))
    return _to_response(updated_room)
