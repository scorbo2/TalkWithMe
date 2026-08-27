"""Tests for app/persistence.py — history + audio storage on disk.

The persistence root is patched to tmp_path by the autouse fixture in
conftest.py; nothing here touches the real chatrooms/ directory.
"""

import base64
import json
import threading

import pytest

from app import persistence
from app.models import ChatMessage

B64_AUDIO = base64.b64encode(b"RIFF-fake-wav-bytes").decode("ascii")


# ---------------------------------------------------------------------------
# Message persistence
# ---------------------------------------------------------------------------

class TestPersistMessage:
    def test_persist_message_creates_history_file_with_user_sender(self):
        persistence.persist_message(
            "room1", ChatMessage(role="user", content="hello"), "id-1"
        )

        data = json.loads((persistence_root_path() / "room1" / "history.json").read_text())
        assert data["datetime"] is not None
        assert data["messages"] == [
            {"id": "id-1", "sender": "USER", "text": "hello", "audio": []}
        ]

    def test_persist_message_uses_persona_name_as_sender(self):
        persistence.persist_message(
            "room1",
            ChatMessage(role="assistant", content="hi there", persona="Luna"),
            "id-2",
        )
        msgs = persistence.load_history("room1")
        assert msgs[0]["sender"] == "Luna"

    def test_persist_message_assistant_without_persona_is_unknown(self):
        persistence.persist_message(
            "room1", ChatMessage(role="assistant", content="hi"), "id-3"
        )
        msgs = persistence.load_history("room1")
        assert msgs[0]["sender"] == "unknown"

    def test_persist_message_appends_in_order(self):
        persistence.persist_message("room1", ChatMessage(role="user", content="a"), "id-1")
        persistence.persist_message("room1", ChatMessage(role="assistant", content="b", persona="Alex"), "id-2")
        persistence.persist_message("room1", ChatMessage(role="user", content="c"), "id-3")
        msgs = persistence.load_history("room1")
        assert [m["id"] for m in msgs] == ["id-1", "id-2", "id-3"]
        assert [m["sender"] for m in msgs] == ["USER", "Alex", "USER"]

    def test_persist_message_returns_the_message_id(self):
        assert persistence.persist_message(
            "room1", ChatMessage(role="user", content="x"), "keep-me"
        ) == "keep-me"

    def test_persist_message_updates_datetime(self):
        persistence.persist_message("room1", ChatMessage(role="user", content="a"), "id-1")
        first = persistence.load_history_with_metadata("room1")["datetime"]
        persistence.persist_message("room1", ChatMessage(role="user", content="b"), "id-2")
        second = persistence.load_history_with_metadata("room1")["datetime"]
        assert second >= first  # ISO-8601 strings compare chronologically here


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class TestLoadHistory:
    def test_load_history_missing_room_returns_empty_list(self):
        assert persistence.load_history("no-such-room") == []

    def test_load_history_with_metadata_missing_room_returns_skeleton(self):
        assert persistence.load_history_with_metadata("no-such-room") == {
            "datetime": None,
            "messages": [],
        }


class TestHistoryFileEncodingMigration:
    def test_load_history_migrates_cp1252_file_to_utf8(self, monkeypatch):
        """A cp1252-written history.json (pre-encoding-fix Windows file)
        must be read via the preferred-encoding fallback and rewritten UTF-8."""
        import locale

        monkeypatch.setattr(locale, "getpreferredencoding", lambda strict: "cp1252")

        room_dir = persistence_root_path() / "legacy"
        room_dir.mkdir(parents=True)
        # The é is a real character here: encoded as cp1252 it is the single
        # byte 0xE9, which is NOT valid UTF-8 — that's what trips the fallback.
        legacy_bytes = (
            '{"datetime": null, "messages": [{"id": "1", "sender": "USER", '
            '"text": "café au lait", "audio": []}]}'
        ).encode("cp1252")
        with pytest.raises(UnicodeDecodeError):
            legacy_bytes.decode("utf-8")  # sanity: the fixture really is non-UTF-8
        (room_dir / "history.json").write_bytes(legacy_bytes)

        msgs = persistence.load_history("legacy")
        assert msgs[0]["text"] == "café au lait"

        # One-shot migration: the file is now valid UTF-8.
        raw = (room_dir / "history.json").read_bytes()
        raw.decode("utf-8")  # raises if the rewrite did not happen

    def test_load_history_rejects_invalid_json(self):
        room_dir = persistence_root_path() / "broken"
        room_dir.mkdir(parents=True)
        (room_dir / "history.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            persistence.load_history("broken")


# ---------------------------------------------------------------------------
# Audio persistence
# ---------------------------------------------------------------------------

class TestAudioFilenames:
    def test_audio_file_extension_from_mime(self):
        assert persistence._audio_file_extension("audio/webm") == ".webm"
        assert persistence._audio_file_extension("audio/ogg") == ".ogg"

    def test_audio_file_extension_plus_suffix_ignored(self):
        assert persistence._audio_file_extension("application/problem+json") == ".problem"

    @pytest.mark.parametrize("mime,expected", [(None, ".bin"), ("", ".bin")])
    def test_audio_file_extension_missing_mime_falls_back_to_bin(self, mime, expected):
        assert persistence._audio_file_extension(mime) == expected

    def test_audio_filename_is_deterministic(self):
        assert persistence._audio_filename("abc", 1, "audio/webm") == "abc_1.webm"


class TestPersistAudio:
    def test_persist_audio_existing_row_names_by_index(self):
        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")

        name = persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")

        assert name == "m-1_0.webm"
        assert (persistence_root_path() / "room1" / name).read_bytes() == b"RIFF-fake-wav-bytes"
        msgs = persistence.load_history("room1")
        assert msgs[0]["audio"] == [name]

    def test_persist_audio_second_upload_gets_next_index(self):
        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")
        persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")
        name = persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")
        assert name == "m-1_1.webm"
        msgs = persistence.load_history("room1")
        assert msgs[0]["audio"] == ["m-1_0.webm", "m-1_1.webm"]

    def test_persist_audio_no_row_stages_with_pending_name(self):
        name = persistence.persist_audio("room1", "m-future", B64_AUDIO, "audio/webm")

        assert "_pending_" in name
        assert name.startswith("m-future_")
        # The file is on disk even though the row does not exist yet.
        assert (persistence_root_path() / "room1" / name).read_bytes() == b"RIFF-fake-wav-bytes"
        # And no history file was created for the row.
        assert persistence.load_history("room1") == []

    def test_persist_audio_no_row_never_overwrites_other_staged_uploads(self):
        first = persistence.persist_audio("room1", "m-future", B64_AUDIO, "audio/webm")
        second = persistence.persist_audio("room1", "m-future", B64_AUDIO, "audio/webm")
        assert first != second
        assert (persistence_root_path() / "room1" / first).exists()
        assert (persistence_root_path() / "room1" / second).exists()

    def test_persist_message_attaches_staged_audio(self):
        staged = persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")

        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")

        msgs = persistence.load_history("room1")
        assert msgs[0]["audio"] == [staged]

    def test_persist_message_then_direct_audio_uses_staged_count_as_index(self):
        staged = persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")
        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")

        name = persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")

        msgs = persistence.load_history("room1")
        assert msgs[0]["audio"] == [staged, name]
        assert name == "m-1_1.webm"

    def test_persist_audio_staging_does_not_leak_between_rooms(self):
        persistence.persist_audio("roomA", "m-1", B64_AUDIO, "audio/webm")
        persistence.persist_message("roomB", ChatMessage(role="user", content="hi"), "m-1")
        assert persistence.load_history("roomB")[0]["audio"] == []

    def test_persist_message_only_drains_its_own_message_id(self):
        persistence.persist_audio("room1", "other-id", B64_AUDIO, "audio/webm")
        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")
        assert persistence.load_history("room1")[0]["audio"] == []

    def test_persist_audio_invalid_base64_raises(self):
        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")
        with pytest.raises(Exception):
            persistence.persist_audio("room1", "m-1", "abc", "audio/webm")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_persist_message_no_lost_updates(self):
        """The history file is read-modify-written under a lock; parallel
        writes must not clobber each other."""
        workers = 8
        errors = []

        def worker(index: int):
            try:
                persistence.persist_message(
                    "race",
                    ChatMessage(role="user", content=f"msg-{index}"),
                    f"id-{index}",
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        ids = {m["id"] for m in persistence.load_history("race")}
        assert ids == {f"id-{i}" for i in range(workers)}


# ---------------------------------------------------------------------------
# clear_room
# ---------------------------------------------------------------------------

class TestClearRoom:
    def test_clear_room_deletes_files_but_keeps_directory(self):
        persistence.persist_message("room1", ChatMessage(role="user", content="a"), "id-1")
        persistence.persist_audio("room1", "id-1", B64_AUDIO, "audio/webm")

        persistence.clear_room("room1")

        room_dir = persistence_root_path() / "room1"
        assert room_dir.exists()
        assert list(room_dir.iterdir()) == []

    def test_clear_room_drops_staged_audio_from_registry(self):
        staged = persistence.persist_audio("room1", "m-1", B64_AUDIO, "audio/webm")

        persistence.clear_room("room1")

        # Staged file is gone from disk...
        assert not (persistence_root_path() / "room1" / staged).exists()
        # ...and a subsequent row for the same ID must not resurrect it.
        persistence.persist_message("room1", ChatMessage(role="user", content="hi"), "m-1")
        assert persistence.load_history("room1")[0]["audio"] == []

    def test_clear_room_missing_room_is_a_noop(self):
        persistence.clear_room("never-existed")  # must not raise


def persistence_root_path():
    """The (patched) persistence root for this test run."""
    return persistence._PERSISTENCE_ROOT
