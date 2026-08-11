"""Chat persistence — per-room message and audio storage on disk.

Each chat room gets its own subdirectory under the top-level `chatrooms/`
directory. A single JSON file stores all messages, and audio files are
written alongside it using the message UUID as filename prefix.

This module is intentionally framework-agnostic so it can be called from
routers, the session manager, or tests without pulling in FastAPI deps.
"""

import base64
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.models import ChatMessage

logger = logging.getLogger(__name__)

# Top-level persistence root — created lazily on first write.
# app/persistence.py → .parent → app/ → .parent → project root
_PERSISTENCE_ROOT = Path(__file__).resolve().parent.parent / "chatrooms"


def _room_dir(room_name: str) -> Path:
    """Return the path to a chat room's persistence subdirectory."""
    return _PERSISTENCE_ROOT / room_name


def _history_path(room_name: str) -> Path:
    """Return the path to the JSON history file for a room."""
    return _room_dir(room_name) / "history.json"


def _audio_file_extension(mime_type: Optional[str]) -> str:
    """Derive a file extension from a MIME type, falling back to '.bin'."""
    if not mime_type:
        return ".bin"
    # "audio/webm" -> ".webm", "audio/ogg" -> ".ogg", etc.
    ext = mime_type.split("/")[-1].split("+")[0]
    if ext:
        return f".{ext}"
    return ".bin"


def _audio_filename(message_id: str, index: int, mime_type: Optional[str] = None) -> str:
    """Generate a deterministic audio filename from a message UUID and index."""
    return f"{message_id}_{index}{_audio_file_extension(mime_type)}"


# ---------------------------------------------------------------------------
# Message persistence
# ---------------------------------------------------------------------------

def persist_message(room_name: str, message: ChatMessage, message_id: str) -> str:
    """Append a message to the room's persisted history and write to disk.

    Returns the message ID (same one passed in).
    """
    room = _room_dir(room_name)
    room.mkdir(parents=True, exist_ok=True)
    path = _history_path(room_name)

    # Load existing history or start fresh (handles corrupted JSON cleanly)
    data = {"datetime": None, "messages": []}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Corrupted JSON found in room '{room_name}'. Starting fresh.")

    now = datetime.now().astimezone().isoformat()
    data["datetime"] = now

    sender = "USER" if message.role == "user" else (message.persona or "unknown")
    data["messages"].append({
        "id": message_id,
        "sender": sender,
        "text": message.content,
        "audio": [],
    })

    # Atomic write to prevent partial saves crashing the app on the next load
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, indent=2, ensure_ascii=False)
        temp_path = tf.name
    
    os.replace(temp_path, path)

    logger.debug("Persisted message %s to room '%s'", message_id, room_name)
    return message_id


def persist_audio(
    room_name: str,
    message_id: str,
    audio_base64: str,
    mime_type: Optional[str] = None,
) -> str:
    """Save an audio file for a message and update its audio list in history.

    Returns the filename that was written.
    """
    room = _room_dir(room_name)
    room.mkdir(parents=True, exist_ok=True)
    path = _history_path(room_name)

    # Single read — determine next index and locate the target message
    data: Dict[str, Any] = {"datetime": None, "messages": []}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Corrupted JSON found in room '{room_name}'. Continuing with blank data.")

    index = 0
    msg_found = None
    for msg in data.get("messages", []):
        if msg["id"] == message_id:
            index = len(msg.get("audio", []))
            msg_found = msg
            break

    filename = _audio_filename(message_id, index, mime_type)

    # Write the raw audio bytes (binary mode, so no encoding needed)
    raw = base64.b64decode(audio_base64)
    with open(room / filename, "wb") as f:
        f.write(raw)

    # Update the message's audio list in a single atomic write
    if msg_found:
        msg_found.setdefault("audio", []).append(filename)
        
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, indent=2, ensure_ascii=False)
        temp_path = tf.name
        
    os.replace(temp_path, path)

    logger.debug("Persisted audio '%s' for message %s in room '%s'", filename, message_id, room_name)
    return filename


def load_history(room_name: str) -> List[Dict[str, Any]]:
    """Load persisted messages for a room.

    Returns a list of message dicts matching the JSON schema.
    Returns an empty list if no history exists or file is corrupted.
    """
    path = _history_path(room_name)
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []

    return data.get("messages", [])


def load_history_with_metadata(room_name: str) -> Dict[str, Any]:
    """Load persisted messages and metadata for a room.

    Returns a dict with "datetime" and "messages" keys.
    Returns {"datetime": None, "messages": []} if no history exists or file is corrupted.
    """
    path = _history_path(room_name)
    if not path.exists():
        return {"datetime": None, "messages": []}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"datetime": None, "messages": []}

    return {
        "datetime": data.get("datetime"),
        "messages": data.get("messages", []),
    }


def clear_room(room_name: str) -> None:
    """Delete all persisted files for a room.

    The subdirectory itself is preserved (so it still exists for future messages).
    """
    room = _room_dir(room_name)
    if not room.exists():
        return

    for item in room.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)

    logger.info("Cleared persistence for room '%s'", room_name)