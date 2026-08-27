"""Tests for app/session.py — the SessionManager and its LLM message building."""

import json

import pytest

from app import persistence
from app.models import ChatMessage
from app.session import SessionManager


@pytest.fixture
def manager() -> SessionManager:
    """A fresh SessionManager (routers use the global singleton; unit tests
    don't need it). The persistence root is already patched to tmp."""
    return SessionManager()


class TestRoomTracking:
    def test_set_current_room_updates_tracker(self, manager):
        assert manager.current_room == "default"
        manager.set_current_room("TNG")
        assert manager.current_room == "TNG"

    def test_set_current_room_same_room_is_noop(self, manager):
        manager.set_current_room("default")
        assert manager.current_room == "default"


class TestActivePersonas:
    def test_set_active_personas_stores_a_copy(self, manager):
        names = ["Alex", "Luna"]
        manager.set_active_personas(names)
        names.append("Malfait")
        assert manager.active_personas == ["Alex", "Luna"]

    def test_active_personas_returns_a_copy(self, manager):
        manager.set_active_personas(["Alex"])
        manager.active_personas.append("Luna")
        assert manager.active_personas == ["Alex"]


class TestMessages:
    def test_add_user_message_persists_to_current_room(self, manager):
        manager.set_current_room("TNG")
        manager.add_user_message("hello", "uid-1")

        msgs = persistence.load_history("TNG")
        assert msgs == [{"id": "uid-1", "sender": "USER", "text": "hello", "audio": []}]
        assert manager.history == [ChatMessage(role="user", content="hello")]

    def test_add_assistant_message_persists_with_persona(self, manager):
        manager.add_assistant_message("why hello", "Luna", "aid-1")
        msgs = persistence.load_history("default")
        assert msgs[0]["sender"] == "Luna"
        assert msgs[0]["id"] == "aid-1"

    def test_history_returns_a_copy(self, manager):
        manager.add_user_message("hi", "uid-1")
        manager.history.clear()
        assert len(manager.history) == 1


class TestBuildLLMMessages:
    def test_build_llm_messages_empty_history_is_system_only(self, manager):
        messages = manager.build_llm_messages("You are Alex.", "Alex")
        assert messages == [{"role": "system", "content": "You are Alex."}]

    def test_build_llm_messages_reformats_roles(self, manager):
        manager.add_user_message_no_persist("what do you think?")
        manager.add_assistant_message_no_persist("I think, therefore I speak.", "Luna")
        manager.add_assistant_message_no_persist("I agree with Luna.", "Alex")
        manager.add_user_message_no_persist("thanks")

        messages = manager.build_llm_messages("You are Alex.", "Alex")

        assert messages[0] == {"role": "system", "content": "You are Alex."}
        assert messages[1] == {"role": "user", "content": "what do you think?"}
        # Another persona's line becomes a user message, prefixed with the name.
        assert messages[2] == {
            "role": "user",
            "content": "[Luna]: I think, therefore I speak.",
        }
        # The responding persona keeps the assistant role.
        assert messages[3] == {"role": "assistant", "content": "I agree with Luna."}
        assert messages[4] == {"role": "user", "content": "thanks"}

    def test_build_llm_messages_max_turns_keeps_only_last_entries(self, manager):
        for i in range(10):
            manager.add_user_message_no_persist(f"turn {i}")

        messages = manager.build_llm_messages(
            "sys", "Alex", max_turns_for_context=4
        )

        # 1 system + last 4 turns
        assert len(messages) == 5
        assert messages[1]["content"] == "turn 6"
        assert messages[-1]["content"] == "turn 9"

    def test_build_llm_messages_no_max_turns_keeps_everything(self, manager):
        for i in range(10):
            manager.add_user_message_no_persist(f"turn {i}")
        messages = manager.build_llm_messages("sys", "Alex")
        assert len(messages) == 11


class TestResetAndLoadRoom:
    def test_reset_clears_history_persistence_and_personas(self, manager):
        manager.set_current_room("TNG")
        manager.set_active_personas(["Alex"])
        manager.add_user_message("bye", "uid-1")
        assert persistence.load_history("TNG") != []

        manager.reset()

        assert manager.history == []
        assert manager.active_personas == []
        assert persistence.load_history("TNG") == []

    def test_load_room_populates_history_without_repersisting(self, manager):
        persistence.persist_message(
            "TNG", ChatMessage(role="user", content="old hello"), "uid-1"
        )
        persistence.persist_message(
            "TNG",
            ChatMessage(role="assistant", content="old reply", persona="Luna"),
            "aid-1",
        )

        manager.load_room("TNG")

        assert manager.current_room == "TNG"
        assert manager.history == [
            ChatMessage(role="user", content="old hello"),
            ChatMessage(role="assistant", content="old reply", persona="Luna"),
        ]

    def test_load_room_replaces_existing_history(self, manager):
        manager.add_user_message("fresh", "uid-fresh")
        persistence.persist_message("TNG", ChatMessage(role="user", content="old"), "uid-old")

        manager.load_room("TNG")

        assert [m.content for m in manager.history] == ["old"]

    def test_load_room_missing_room_yields_empty_history(self, manager):
        manager.load_room("never-existed")
        assert manager.history == []
        assert manager.current_room == "never-existed"


class TestGetHistoryDicts:
    def test_get_history_dicts_serializable(self, manager):
        manager.add_user_message("hi", "uid-1")
        manager.add_assistant_message("there", "Alex", "aid-1")

        dicts = manager.get_history_dicts()
        json.dumps(dicts)  # must be JSON-serializable
        assert dicts[0]["role"] == "user"
        assert dicts[1]["persona"] == "Alex"
