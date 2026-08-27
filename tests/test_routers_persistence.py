"""API tests for app/routers/persistence.py — audio upload and serving."""

import base64

from app.models import ChatMessage
from app.persistence import persist_message

AUDIO_BYTES = b"RIFF-fake-wav-bytes"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestUploadAudio:
    def test_upload_for_existing_message_gets_indexed_name(self, client):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")

        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "msg-1", "audio_base64": b64(AUDIO_BYTES),
                                 "mime_type": "audio/webm"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "saved", "filename": "msg-1_0.webm"}

    def test_second_upload_increments_index(self, client, persistence_root):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")
        client.post("/api/persist/audio?room=TNG",
                    json={"message_id": "msg-1", "audio_base64": b64(AUDIO_BYTES),
                          "mime_type": "audio/webm"})

        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "msg-1", "audio_base64": b64(AUDIO_BYTES),
                                 "mime_type": "audio/webm"})

        assert resp.json()["filename"] == "msg-1_1.webm"
        # The message's audio list now references both files.
        messages = _load_messages(persistence_root, "TNG")
        assert messages[0]["audio"] == ["msg-1_0.webm", "msg-1_1.webm"]

    def test_upload_before_message_row_is_staged(self, client, persistence_root):
        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "msg-2", "audio_base64": b64(AUDIO_BYTES),
                                 "mime_type": "audio/webm"})
        assert resp.status_code == 200
        filename = resp.json()["filename"]
        # Staged names carry the message id and a "pending" marker.
        assert filename.startswith("msg-2_pending_")
        assert filename.endswith(".webm")
        assert (persistence_root / "TNG" / filename).read_bytes() == AUDIO_BYTES

        # When the row finally lands, the staged file is attached to it.
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-2")
        messages = _load_messages(persistence_root, "TNG")
        assert messages[0]["audio"] == [filename]

    def test_upload_without_mime_type_falls_back_to_bin(self, client, persistence_root):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-3")
        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "msg-3", "audio_base64": b64(AUDIO_BYTES)})
        assert resp.json()["filename"] == "msg-3_0.bin"

    def test_invalid_base64_returns_500(self, client):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-4")
        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "msg-4", "audio_base64": "!!!not-base64!!!"})
        assert resp.status_code == 500

    def test_room_query_parameter_required(self, client):
        resp = client.post("/api/persist/audio",
                           json={"message_id": "msg-5", "audio_base64": b64(AUDIO_BYTES)})
        assert resp.status_code == 422


class TestServeAudio:
    def test_serves_persisted_audio_bytes(self, client, persistence_root):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")
        client.post("/api/persist/audio?room=TNG",
                    json={"message_id": "msg-1", "audio_base64": b64(AUDIO_BYTES),
                          "mime_type": "audio/webm"})

        resp = client.get("/api/persist/audio/TNG/msg-1_0.webm")
        assert resp.status_code == 200
        assert resp.content == AUDIO_BYTES

    def test_missing_audio_file_404(self, client):
        resp = client.get("/api/persist/audio/TNG/nope_0.webm")
        assert resp.status_code == 404

    def test_missing_room_404(self, client):
        resp = client.get("/api/persist/audio/NoRoom/nope_0.webm")
        assert resp.status_code == 404


def _load_messages(persistence_root, room: str):
    from app.persistence import load_history

    return load_history(room)
