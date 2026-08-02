"""Session router — inspect, reset, and configure the active session."""

import logging
import uuid

from fastapi import APIRouter

from app.config import get_personas
from app.models import SessionPersonasRequest, SessionState, PersistedHistoryResponse, PersistedMessage
from app.persistence import load_history_with_metadata
from app.session import session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("", response_model=SessionState)
def get_session():
    """Return the current session state (history + active personas)."""
    return SessionState(
        history=session.get_history_dicts(),
        active_personas=session.active_personas,
        current_room=session.current_room,
    )


@router.post("/new")
def new_session():
    """Clear history and reset the session. Returns the fresh state."""
    session.reset()
    return {"status": "cleared"}


@router.post("/personas")
def update_active_personas(req: SessionPersonasRequest):
    """Update which personas are active in the current session.

    Validates that all requested persona names exist in the config.
    """
    config = get_personas()
    valid_names = {p.name for p in config.personas}
    requested = set(req.active_personas)

    # Silently drop unknown names — they'll just be ignored
    unknown = requested - valid_names
    if unknown:
        logger.warning("Unknown persona names requested: %s", unknown)

    session.set_active_personas(list(requested & valid_names))
    return {"status": "updated", "active_personas": session.active_personas}


@router.get("/load-room/{room_name}")
def load_room(room_name: str):
    """Load persisted chat history for a room into the active session.

    Used when switching chat rooms. Clears any existing in-memory history
    and populates from the room's persisted data.
    """
    session.load_room(room_name)
    metadata = load_history_with_metadata(room_name)
    return PersistedHistoryResponse(
        room=room_name,
        datetime=metadata["datetime"],
        messages=[PersistedMessage(**m) for m in metadata["messages"]],
    )
