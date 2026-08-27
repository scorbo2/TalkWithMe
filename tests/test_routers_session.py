"""API tests for app/routers/session.py — session state, reset, personas, room loading."""

from pathlib import Path

from app.models import ChatMessage
from app.persistence import persist_message


def _add_exchange(room: str, user_text: str, reply_text: str):
    """Add a user + assistant message straight to the global session (persisted)."""
    from app.session import session

    session.set_current_room(room)
    session.add_user_message(user_text, "id-u1")
    session.add_assistant_message(reply_text, "Alex", "id-a1")


class TestGetSession:
    def test_returns_fresh_state(self, client):
        resp = client.get("/api/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["history"] == []
        assert body["current_room"] == "default"

    def test_reflects_messages_in_history(self, client):
        _add_exchange("default", "hello", "hi there")
        body = client.get("/api/session").json()
        assert [m["role"] for m in body["history"]] == ["user", "assistant"]
        assert body["history"][0]["content"] == "hello"


class TestNewSession:
    def test_new_clears_history_and_persistence(self, client, persistence_root):
        _add_exchange("TNG", "hello", "hi there")
        history_file = persistence_root / "TNG" / "history.json"
        assert history_file.exists()

        resp = client.post("/api/session/new")
        assert resp.status_code == 200
        assert resp.json() == {"status": "cleared"}

        assert client.get("/api/session").json()["history"] == []
        # The room directory survives, but its files are gone.
        assert (persistence_root / "TNG").is_dir()
        assert not history_file.exists()


class TestUpdateActivePersonas:
    def test_valid_names_applied(self, client):
        resp = client.post("/api/session/personas", json={"active_personas": ["Luna"]})
        assert resp.status_code == 200
        assert resp.json()["active_personas"] == ["Luna"]
        assert client.get("/api/session").json()["active_personas"] == ["Luna"]

    def test_unknown_names_silently_dropped(self, client):
        resp = client.post("/api/session/personas",
                           json={"active_personas": ["Alex", "Q"]})
        assert resp.status_code == 200
        assert resp.json()["active_personas"] == ["Alex"]

    def test_empty_list_rejected_by_model(self, client):
        resp = client.post("/api/session/personas", json={"active_personas": []})
        assert resp.status_code == 422


class TestLoadRoom:
    def _seed_room(self, room: str, persistence_root: Path):
        """Write a couple of messages straight to disk, as an earlier session would have."""
        persist_message(room, ChatMessage(role="user", content="earlier question"), "id-u1")
        persist_message(room, ChatMessage(role="assistant", content="earlier answer",
                                          persona="Alex"), "id-a1")

    def test_load_room_populates_session_and_returns_messages(self, client, persistence_root):
        self._seed_room("TNG", persistence_root)

        resp = client.get("/api/session/load-room/TNG")
        assert resp.status_code == 200
        body = resp.json()
        assert body["room"] == "TNG"
        assert [m["sender"] for m in body["messages"]] == ["USER", "Alex"]
        assert [m["text"] for m in body["messages"]] == ["earlier question", "earlier answer"]

        # The in-memory session now carries the loaded history too.
        session_state = client.get("/api/session").json()
        assert session_state["current_room"] == "TNG"
        assert [m["role"] for m in session_state["history"]] == ["user", "assistant"]

    def test_load_room_replaces_previous_history(self, client, persistence_root):
        self._seed_room("TNG", persistence_root)
        _add_exchange("Solo", "fresh question", "fresh answer")

        # Switching to TNG must not leak Solo's messages.
        client.get("/api/session/load-room/TNG")
        history = client.get("/api/session").json()["history"]
        assert [m["content"] for m in history] == ["earlier question", "earlier answer"]

    def test_load_room_with_no_history_returns_empty(self, client):
        resp = client.get("/api/session/load-room/EmptyRoom")
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"] == []
        assert body["datetime"] is None
