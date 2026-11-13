"""API tests for app/routers/persistence.py — audio upload, serving, deletion."""

import base64

from app.models import ChatMessage
from app.persistence import persist_message
from app.session import session

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

    def test_upload_with_dotdot_room_returns_422_and_writes_nothing(self, client, persistence_root):
        # room is a QUERY param, so no client-side path normalization can
        # save it: "../pwn" arrives intact and would otherwise mkdir() and
        # write outside the persistence root.
        resp = client.post("/api/persist/audio?room=../pwn",
                           json={"message_id": "m1", "audio_base64": b64(AUDIO_BYTES),
                                 "mime_type": "audio/webm"})

        assert resp.status_code == 422
        assert not (persistence_root.parent / "pwn").exists()

    def test_upload_with_traversal_message_id_returns_422_and_writes_nothing(self, client, persistence_root):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "m1")
        # The message_id is interpolated into the on-disk filename; with a
        # row present the unguarded file would land at
        # chatrooms/TNG/../../pwn_0.webm — i.e. the tmp root's parent level.
        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "../../pwn", "audio_base64": b64(AUDIO_BYTES),
                                 "mime_type": "audio/webm"})

        assert resp.status_code == 422
        assert not (persistence_root.parent / "pwn_0.webm").exists()


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

    def test_history_json_is_not_serveable_as_audio(self, client, persistence_root):
        # GIVEN a room directory holding its history.json — a perfectly plain
        # filename by the join-safety rule, which is why the audio endpoint
        # used to hand the room's whole transcript back with a confident
        # media_type="audio/*" on it.
        persist_message("TNG", ChatMessage(role="user", content="TOP SECRET transcript"), "msg-1")
        assert (persistence_root / "TNG" / "history.json").exists()

        # WHEN the audio endpoint is asked for it by name,
        resp = client.get("/api/persist/audio/TNG/history.json")

        # THEN it is a missing clip as far as this endpoint is concerned, and
        # none of the transcript leaks on the way to the 404.
        assert resp.status_code == 404
        assert "TOP SECRET" not in resp.text

    def test_symlink_out_of_the_room_dir_is_not_serveable(self, client, persistence_root):
        # GIVEN a symlink inside the room directory pointing at a file outside
        # it: the name is plain and join-safe, and FileResponse follows
        # symlinks, so the containment check has to happen after resolve().
        try:
            secret = persistence_root / "secret.txt"
            secret.write_bytes(b"not audio, not yours")
            room = persistence_root / "TNG"
            room.mkdir(parents=True, exist_ok=True)
            (room / "link.wav").symlink_to(secret)
        except OSError:
            # Platforms without symlink support (unprivileged Windows): the
            # containment rule is unexercisable here, not broken.
            print("  [symlink] filesystem does not support symlinks; containment case unexercised")
            return

        # WHEN the audio endpoint is asked for the link,
        resp = client.get("/api/persist/audio/TNG/link.wav")

        # THEN it 404s and never reads the target.
        assert resp.status_code == 404
        assert b"not audio, not yours" not in resp.content

    def test_raw_asgi_get_serves_a_legit_file(self, client, persistence_root, raw_asgi_get):
        # Sanity check for the raw_asgi_get fixture itself: a normal path
        # must work through the raw scope, proving the harness drives the
        # real endpoint (not a 404-by-accident test suite).
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")
        client.post("/api/persist/audio?room=TNG",
                    json={"message_id": "msg-1", "audio_base64": b64(AUDIO_BYTES),
                          "mime_type": "audio/webm"})

        status, body = raw_asgi_get("/api/persist/audio/TNG/msg-1_0.webm")

        assert status == 200
        assert body == AUDIO_BYTES

    def test_raw_asgi_get_serves_a_file_larger_than_one_chunk(self, client, persistence_root, raw_asgi_get):
        # A file bigger than FileResponse's 64 KiB chunk size arrives as
        # several http.response.body messages, only the LAST one with
        # more_body=False. The raw harness must hold the "disconnect" until
        # that final chunk: reporting it earlier makes starlette's
        # disconnect-watching task group cancel the transfer mid-file, and a
        # truncated body still reports 200 — so every assertion below is
        # load-bearing.
        big = b"RIFF" + (b"\x00\x01\x02\x03" * 40960)  # 160 KiB: three chunks
        assert len(big) > 64 * 1024

        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-big")
        resp = client.post("/api/persist/audio?room=TNG",
                           json={"message_id": "msg-big", "audio_base64": b64(big),
                                 "mime_type": "audio/webm"})
        assert resp.status_code == 200
        filename = resp.json()["filename"]

        status, body = raw_asgi_get(f"/api/persist/audio/TNG/{filename}")

        assert status == 200
        assert len(body) == len(big)
        assert body == big

    def test_serve_with_dotdot_room_segment_returns_422(self, persistence_root, raw_asgi_get):
        # A file OUTSIDE the persistence root that an unvalidated room
        # segment of ".." would let the endpoint read:
        sentinel = persistence_root.parent / "sentinel.txt"
        sentinel.write_bytes(b"top secret")

        status, body = raw_asgi_get("/api/persist/audio/../sentinel.txt")

        assert status == 422
        assert b"top secret" not in body

    def test_serve_with_encoded_dotdot_room_segment_returns_422(self, persistence_root, raw_asgi_get):
        # The encoded spelling: uvicorn's unquote() turns "..%2Fx" into
        # "../x" BEFORE routing, so it arrives as the same traversal. The
        # scope carries the encoded raw_path and the decoded path, exactly
        # as uvicorn's h11 protocol builds them.
        sentinel = persistence_root.parent / "sentinel.txt"
        sentinel.write_bytes(b"top secret")

        status, body = raw_asgi_get(
            "/api/persist/audio/../sentinel.txt",
            raw_path=b"/api/persist/audio/..%2Fsentinel.txt",
        )

        assert status == 422
        assert b"top secret" not in body

    def test_serve_with_dotdot_filename_returns_404(self, raw_asgi_get):
        # A filename of ".." resolves to the persistence root itself —
        # rejected as a non-file (404, same as a missing file).
        status, body = raw_asgi_get("/api/persist/audio/TNG/..")
        assert status == 404
        assert b"top secret" not in body

    def test_serve_with_backslash_filename_returns_404(self, raw_asgi_get):
        # Backslashes are legal URL path characters and are traversal
        # separators on Windows — rejected everywhere for uniformity.
        status, _ = raw_asgi_get("/api/persist/audio/TNG/..\\sentinel.txt")
        assert status == 404


class TestDeleteMessage:
    def test_delete_returns_200_and_removes_row_and_audio(self, client, persistence_root):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")
        client.post("/api/persist/audio?room=TNG",
                    json={"message_id": "msg-1", "audio_base64": b64(AUDIO_BYTES),
                          "mime_type": "audio/webm"})

        resp = client.delete("/api/persist/message/TNG/msg-1")

        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted"}
        assert _load_messages(persistence_root, "TNG") == []
        assert not (persistence_root / "TNG" / "msg-1_0.webm").exists()

    def test_delete_unknown_id_returns_404_and_changes_nothing(self, client, persistence_root):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")
        before = (persistence_root / "TNG" / "history.json").read_bytes()

        resp = client.delete("/api/persist/message/TNG/no-such-id")

        assert resp.status_code == 404
        assert (persistence_root / "TNG" / "history.json").read_bytes() == before
        assert [m["id"] for m in _load_messages(persistence_root, "TNG")] == ["msg-1"]

    def test_delete_current_room_also_removes_from_in_memory_session(self, client):
        # The session holds the room's history in memory (LLM context):
        session.set_current_room("TNG")
        session.add_user_message("to be deleted", "uid-del")
        session.add_assistant_message("you too", "Luna", "aid-keep")

        resp = client.delete("/api/persist/message/TNG/uid-del")

        assert resp.status_code == 200
        assert [m["id"] for m in _load_messages(None, "TNG")] == ["aid-keep"]
        # In-memory history reconciled too — the deleted message stops
        # reaching the LLM from the next turn:
        assert [m.id for m in session.history] == ["aid-keep"]

    def test_delete_non_current_room_leaves_in_memory_session_untouched(self, client):
        session.set_current_room("TNG")
        session.add_user_message("stays here", "uid-keep")

        # A different room, on disk only:
        persist_message("Elsewhere", ChatMessage(role="user", content="goes"), "uid-gone")

        resp = client.delete("/api/persist/message/Elsewhere/uid-gone")

        assert resp.status_code == 200
        assert _load_messages(None, "Elsewhere") == []
        # The in-memory session (current room's history) is untouched:
        assert [m.id for m in session.history] == ["uid-keep"]

    def test_delete_with_invalid_room_name_returns_422(self, client):
        persist_message("TNG", ChatMessage(role="user", content="hi"), "msg-1")

        # Dots are not in the room-name alphabet — also blocks traversal:
        resp = client.delete("/api/persist/message/bad..room/msg-1")

        assert resp.status_code == 422
        assert [m["id"] for m in _load_messages(None, "TNG")] == ["msg-1"]


class TestRoomHistoryCount:
    def test_returns_persisted_message_count(self, client):
        persist_message("TNG", ChatMessage(role="user", content="one"), "msg-1")
        persist_message("TNG", ChatMessage(role="assistant", content="two",
                                           persona="Alex"), "msg-2")

        resp = client.get("/api/persist/history/TNG")

        assert resp.status_code == 200
        assert resp.json() == {"room": "TNG", "message_count": 2}

    def test_room_without_history_returns_zero(self, client):
        resp = client.get("/api/persist/history/TNG")

        assert resp.status_code == 200
        assert resp.json() == {"room": "TNG", "message_count": 0}

    def test_read_only_does_not_touch_the_session(self, client):
        # The whole point of this endpoint: probing a room's history must
        # not switch the session to it (unlike load-room):
        persist_message("TNG", ChatMessage(role="user", content="one"), "msg-1")
        session.set_current_room("default")

        resp = client.get("/api/persist/history/TNG")

        assert resp.status_code == 200
        assert session.current_room == "default"
        assert [m.id for m in session.history] == []

    def test_invalid_room_name_returns_422(self, client):
        # Dots are not in the room-name alphabet — also blocks traversal:
        resp = client.get("/api/persist/history/bad..room")

        assert resp.status_code == 422


def _load_messages(persistence_root, room: str):
    from app.persistence import load_history

    return load_history(room)
