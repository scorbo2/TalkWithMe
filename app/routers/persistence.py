"""Chat persistence router — audio upload and audio file serving.

Provides endpoints for the frontend to upload recorded/synthesized audio
files and retrieve them for playback.
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.models import AudioUploadRequest
from app.persistence import _PERSISTENCE_ROOT, persist_audio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/persist", tags=["persistence"])


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
    audio_path = _PERSISTENCE_ROOT / room_name / filename
    if not audio_path.exists() or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found.")
    return FileResponse(audio_path, media_type="audio/*")
