"""Chat persistence — per-room message and audio storage on disk.

Each chat room gets its own subdirectory under the top-level `chatrooms/`
directory. A single JSON file stores all messages, and audio files are
written alongside it using the message UUID as filename prefix.

This module is intentionally framework-agnostic so it can be called from
routers, the session manager, or tests without pulling in FastAPI deps.

Concurrency note: the audio upload endpoint is a sync route (it runs in
FastAPI's thread pool) and the frontend fires uploads without awaiting
them, so calls here can interleave. All access to a room's history.json
is serialized through _HISTORY_LOCK: read-modify-write cycles stay
atomic, and readers never observe a half-written file.
"""

import base64
import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.models import ChatMessage

logger = logging.getLogger(__name__)

# Top-level persistence root — created lazily on first write.
# app/persistence.py → .parent → app/ → .parent → project root
_PERSISTENCE_ROOT = Path(__file__).resolve().parent.parent / "chatrooms"

# Serializes all read-modify-write cycles on history.json (and all reads,
# so a reader can never observe a file mid-truncate).
_HISTORY_LOCK = threading.Lock()

# Audio that arrived for a message whose row has not been persisted yet.
# This happens by design in two flows:
#   - STT: the browser uploads the recording BEFORE the chat request even
#     creates the user message row.
#   - streaming TTS: sentences are synthesized (and uploaded) while the
#     assistant's reply is still streaming; the row only lands after the
#     token stream finishes.
# Keyed by (room_name, message_id) -> list of staging filenames written to
# disk in the meantime. persist_message() drains the list for a new message
# ID and attaches the files, so no audio is silently dropped or overwritten.
# The staging registry is in-memory, so a process restart between the upload
# and the row creation leaves the (valid) files on disk unreferenced.
_pending_audio: Dict[Tuple[str, str], List[str]] = {}


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


def _staged_audio_filename(message_id: str, mime_type: Optional[str]) -> str:
    """Generate a stable, unique filename for pre-row audio.

    The file is written before the message row exists, so its final position
    in the message's audio list is unknown. A stable unique name (rather than
    a guessed index) means the name is final on first write: playback
    buttons injected with it during the live session keep working, and
    persist_message() simply records the same name in history.json.
    """
    return f"{message_id}_pending_{uuid.uuid4().hex[:8]}{_audio_file_extension(mime_type)}"


def _read_history_file(room_name: str) -> Dict[str, Any]:
    """Load the room's history JSON (or a fresh skeleton if none exists).

    Caller must hold _HISTORY_LOCK.
    """
    path = _history_path(room_name)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"datetime": None, "messages": []}


def _write_history_file(room_name: str, data: Dict[str, Any]) -> None:
    """Atomically write the room's history JSON.

    Writes to a temporary file in the same directory, then swaps it into
    place with os.replace(). This way readers never observe a
    half-truncated file, even if the process dies mid-write.

    Caller must hold _HISTORY_LOCK. The directory is expected to exist
    (every public write path creates it first).
    """
    target = _history_path(room_name)
    # The temp file must live on the same filesystem for os.replace() to be atomic.
    tmp_path = target.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target)
    except BaseException:
        # Don't leave a half-written .tmp lying around if anything fails.
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Message persistence
# ---------------------------------------------------------------------------

def persist_message(room_name: str, message: ChatMessage, message_id: str) -> str:
    """Append a message to the room's persisted history and write to disk.

    Any audio that was uploaded for this message ID before the row existed
    (see _pending_audio) is attached to the new row's audio list in this
    same write.

    Returns the message ID (same one passed in).
    """
    with _HISTORY_LOCK:
        room = _room_dir(room_name)
        room.mkdir(parents=True, exist_ok=True)

        data = _read_history_file(room_name)
        data["datetime"] = datetime.now().astimezone().isoformat()

        sender = "USER" if message.role == "user" else (message.persona or "unknown")
        entry = {
            "id": message_id,
            "sender": sender,
            "text": message.content,
            "audio": list(_pending_audio.pop((room_name, message_id), [])),
        }
        data["messages"].append(entry)

        _write_history_file(room_name, data)

    if entry["audio"]:
        logger.info(
            "Attached %d pre-row audio file(s) to message %s in room '%s'",
            len(entry["audio"]), message_id, room_name,
        )
    logger.debug("Persisted message %s to room '%s'", message_id, room_name)
    return message_id


def persist_audio(
    room_name: str,
    message_id: str,
    audio_base64: str,
    mime_type: Optional[str] = None,
) -> str:
    """Save an audio file for a message and update its audio list in history.

    If the message row does not exist yet (STT audio uploaded before the chat
    request creates the user message, or a TTS sentence synthesized while the
    reply is still streaming), the file is staged under a unique name and the
    name is remembered; persist_message() attaches it when the row is created.
    Never returns a filename that another upload can overwrite.

    Returns the filename that was written.
    """
    with _HISTORY_LOCK:
        room = _room_dir(room_name)
        room.mkdir(parents=True, exist_ok=True)

        data = _read_history_file(room_name)

        index = 0
        msg_found = None
        for msg in data.get("messages", []):
            if msg["id"] == message_id:
                index = len(msg.get("audio", []))
                msg_found = msg
                break

        raw = base64.b64decode(audio_base64)

        if msg_found is None:
            # Stage until the message row exists. Writing an unlinked
            # "<id>_0.<ext>" here is how early streaming/STT uploads used to
            # overwrite each other and vanish on reload.
            filename = _staged_audio_filename(message_id, mime_type)
            with open(room / filename, "wb") as f:
                f.write(raw)
            _pending_audio.setdefault((room_name, message_id), []).append(filename)
            logger.warning(
                "Audio for message %s in room '%s' arrived before the message "
                "row was persisted; staged as '%s'",
                message_id, room_name, filename,
            )
            return filename

        filename = _audio_filename(message_id, index, mime_type)
        with open(room / filename, "wb") as f:
            f.write(raw)
        msg_found.setdefault("audio", []).append(filename)
        _write_history_file(room_name, data)

    logger.debug("Persisted audio '%s' for message %s in room '%s'", filename, message_id, room_name)
    return filename


def load_history(room_name: str) -> List[Dict[str, Any]]:
    """Load persisted messages for a room.

    Returns a list of message dicts matching the JSON schema.
    Returns an empty list if no history exists.
    """
    with _HISTORY_LOCK:
        data = _read_history_file(room_name)
    return data.get("messages", [])


def load_history_with_metadata(room_name: str) -> Dict[str, Any]:
    """Load persisted messages and metadata for a room.

    Returns a dict with "datetime" and "messages" keys.
    Returns {"datetime": None, "messages": []} if no history exists.
    """
    with _HISTORY_LOCK:
        data = _read_history_file(room_name)
    return {
        "datetime": data.get("datetime"),
        "messages": data.get("messages", []),
    }


def clear_room(room_name: str) -> None:
    """Delete all persisted files for a room.

    The subdirectory itself is preserved (so it still exists for future messages).
    Staged audio awaiting this room's message rows is dropped from the registry
    too — the files are being deleted anyway.
    """
    with _HISTORY_LOCK:
        for key in [k for k in _pending_audio if k[0] == room_name]:
            _pending_audio.pop(key, None)

        room = _room_dir(room_name)
        if not room.exists():
            return

        for item in room.iterdir():
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)

    logger.info("Cleared persistence for room '%s'", room_name)
