"""Chat persistence router — audio upload, audio file serving, message deletion.

Provides endpoints for the frontend to upload recorded/synthesized audio
files, retrieve them for playback, and delete individual persisted
messages (row + audio) from a room.
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.config import is_valid_room_name
from app.models import AudioUploadRequest
from app.persistence import (
    _PERSISTENCE_ROOT,
    _is_plain_filename,
    delete_message,
    is_serveable_audio_filename,
    load_history,
    persist_audio,
)
from app.session import session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/persist", tags=["persistence"])


def _require_valid_room_name(room_name: str) -> None:
    """Raise 422 when *room_name* is outside the room-name alphabet.

    uvicorn percent-decodes the request target BEFORE routing, so both a
    literal ".." segment and an encoded "..%2Fx" arrive here as a perfectly
    legal single path segment — only an alphabet check can stop them from
    being joined onto _PERSISTENCE_ROOT.
    """
    if not is_valid_room_name(room_name):
        raise HTTPException(
            status_code=422,
            detail="Room name may only contain letters, numbers, spaces, hyphens, and underscores.",
        )


@router.post("/audio")
def upload_audio(
    req: AudioUploadRequest,
    room: str = Query(..., description="The chat room this audio belongs to"),
):
    """Upload an audio file for a persisted message.

    The frontend calls this after capturing STT recordings or TTS output.
    The audio is saved to the chat room's persistence directory and the
    message's audio list is updated in history.json.
    """
    # Both values end up in on-disk paths (the room directory is created,
    # the message_id is interpolated into the filename), so neither is
    # trusted: an unvalidated room of "../x" would mkdir outside the
    # persistence root, and a message_id with separators would land the
    # audio file wherever the caller pointed it.
    _require_valid_room_name(room)
    if not _is_plain_filename(req.message_id):
        raise HTTPException(
            status_code=422,
            detail="Message ID may not contain path separators.",
        )

    try:
        filename = persist_audio(room, req.message_id, req.audio_base64, req.mime_type)
        return {"status": "saved", "filename": filename}
    except Exception as exc:
        logger.error("Failed to persist audio for message %s: %s", req.message_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to save audio: {exc}")


@router.get("/audio/{room_name}/{filename}")
def serve_audio(room_name: str, filename: str):
    """Serve a persisted audio file for playback.

    The frontend uses this to replay audio from previous messages.
    """
    _require_valid_room_name(room_name)
    if not is_serveable_audio_filename(filename):
        # 404, not 422: an unmatchable filename is indistinguishable from
        # a missing one, and there is no reason to reveal which check
        # tripped. This also covers the room's own history.json, which is
        # join-safe but is emphatically not a clip to replay.
        raise HTTPException(status_code=404, detail="Audio file not found.")
    audio_path = _PERSISTENCE_ROOT / room_name / filename
    # A plain name is not the same as a contained file: a symlink inside the
    # room directory (or a hand-edited history.json audio entry pointing at
    # one) resolves outside it, and FileResponse would follow it happily.
    room_dir = (_PERSISTENCE_ROOT / room_name).resolve()
    resolved = audio_path.resolve()
    if not resolved.is_relative_to(room_dir) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found.")
    return FileResponse(resolved, media_type="audio/*")


@router.delete("/message/{room_name}/{message_id}")
def delete_message_endpoint(room_name: str, message_id: str):
    """Delete a single persisted message and all of its audio files.

    Removes the message row from the room's history.json and unlinks its
    audio files (attached, staged, or late-arriving — the persistence core
    sweeps all three under the history lock). If the room is the session's
    current room, the in-memory history entry is removed as well, so the
    deleted message stops reaching the LLM on the next turn.

    200 when the message existed and was deleted; 404 when the room has no
    message with that ID (nothing was deleted).
    """
    _require_valid_room_name(room_name)

    deleted = delete_message(room_name, message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No such message in this room.")

    # Keep the in-memory session in sync when it holds this room's history.
    # Case-insensitive on purpose: room names are case-insensitive app-wide,
    # and two distinct rooms can never differ only by case (creation rejects
    # such duplicates), so this can only ever match the same room.
    if room_name.lower() == session.current_room.lower():
        session.remove_message_by_id(message_id)

    return {"status": "deleted"}


@router.get("/history/{room_name}")
def room_history_count(room_name: str):
    """Return the number of persisted messages for a room.

    Read-only on purpose: the frontend's room-deletion confirmation uses it
    to warn about the history that is about to be deleted. The natural
    alternative, GET /api/session/load-room/{room_name}, cannot serve that
    job — it also switches the backend session to the room, which would
    reset the user's active session when the deletion goes ahead.
    """
    _require_valid_room_name(room_name)
    return {"room": room_name, "message_count": len(load_history(room_name))}
