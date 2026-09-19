"""API tests for app/routers/chatrooms.py — room CRUD, persona assignment, echo chamber.

The fixture config has one room ("TNG" with Alex+Luna) and two personas.
The implicit "default" room is not in chatrooms.yaml.
"""

from app import persistence
from app.models import ChatMessage


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestListChatrooms:
    def test_list_excludes_implicit_default(self, client):
        resp = client.get("/api/chatrooms")
        assert resp.status_code == 200
        rooms = resp.json()
        assert [r["name"] for r in rooms] == ["TNG"]
        assert rooms[0]["persona_names"] == ["Alex", "Luna"]

    def test_list_all_includes_default_with_every_persona(self, client):
        resp = client.get("/api/chatrooms/all")
        assert resp.status_code == 200
        rooms = resp.json()
        assert rooms[0]["name"] == "default"
        assert rooms[0]["persona_names"] == ["Alex", "Luna"]
        assert [r["name"] for r in rooms[1:]] == ["TNG"]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreateChatroom:
    def test_create_empty_room(self, client):
        resp = client.post("/api/chatrooms", json={"name": "Enterprise"})
        assert resp.status_code == 201
        assert resp.json() == {
            "name": "Enterprise", "persona_names": [], "echo_chamber": False,
            "stt_language_policy": None,
        }
        assert [r["name"] for r in client.get("/api/chatrooms").json()] == ["TNG", "Enterprise"]

    def test_create_reserved_default_rejected(self, client):
        assert client.post("/api/chatrooms", json={"name": "default"}).status_code == 409
        assert client.post("/api/chatrooms", json={"name": "Default"}).status_code == 409

    def test_create_blank_name_rejected(self, client):
        resp = client.post("/api/chatrooms", json={"name": "   "})
        assert resp.status_code == 422

    def test_create_invalid_characters_rejected(self, client):
        resp = client.post("/api/chatrooms", json={"name": "bad/name"})
        assert resp.status_code == 422
        assert "only contain" in resp.json()["detail"]

    def test_create_duplicate_rejected_case_insensitively(self, client):
        resp = client.post("/api/chatrooms", json={"name": "tng"})
        assert resp.status_code == 409

    def test_create_too_long_name_rejected(self, client):
        resp = client.post("/api/chatrooms", json={"name": "x" * 21})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------

class TestGetChatroom:
    def test_get_default_returns_all_personas(self, client):
        resp = client.get("/api/chatrooms/default")
        assert resp.status_code == 200
        assert resp.json()["persona_names"] == ["Alex", "Luna"]

    def test_get_configured_room(self, client):
        resp = client.get("/api/chatrooms/TNG")
        assert resp.status_code == 200
        assert resp.json()["name"] == "TNG"

    def test_get_is_case_insensitive(self, client):
        resp = client.get("/api/chatrooms/tng")
        assert resp.status_code == 200

    def test_get_unknown_room_404(self, client):
        assert client.get("/api/chatrooms/NoSuchRoom").status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDeleteChatroom:
    def test_delete_removes_room(self, client):
        client.post("/api/chatrooms", json={"name": "Enterprise"})
        resp = client.delete("/api/chatrooms/Enterprise")
        assert resp.status_code == 204
        assert [r["name"] for r in client.get("/api/chatrooms").json()] == ["TNG"]

    def test_delete_default_rejected(self, client):
        resp = client.delete("/api/chatrooms/default")
        assert resp.status_code == 400

    def test_delete_unknown_room_404(self, client):
        resp = client.delete("/api/chatrooms/NoSuchRoom")
        assert resp.status_code == 404

    def test_delete_removes_persistence_directory_and_its_contents(
        self, client, persistence_root
    ):
        # GIVEN a room with a persisted message and an audio file on disk:
        client.post("/api/chatrooms", json={"name": "Enterprise"})
        persistence.persist_message(
            "Enterprise", ChatMessage(role="user", content="hi"), "id-1"
        )
        (persistence_root / "Enterprise" / "id-1_0.webm").write_bytes(b"fake-audio")
        room_dir = persistence_root / "Enterprise"
        assert room_dir.exists()

        # WHEN the room is deleted:
        resp = client.delete("/api/chatrooms/Enterprise")

        # THEN the persistence directory is gone, not just the YAML entry:
        assert resp.status_code == 204
        assert not room_dir.exists()

    def test_delete_room_without_persistence_directory_succeeds(
        self, client, persistence_root
    ):
        # A room that never had a message has no directory; delete must not 500.
        client.post("/api/chatrooms", json={"name": "Enterprise"})
        assert not (persistence_root / "Enterprise").exists()
        assert client.delete("/api/chatrooms/Enterprise").status_code == 204

    def test_delete_then_recreate_starts_with_empty_history(self, client):
        # The user-visible regression: re-creating a deleted room must not
        # resurrect the old room's conversation.
        client.post("/api/chatrooms", json={"name": "Enterprise"})
        persistence.persist_message(
            "Enterprise", ChatMessage(role="user", content="secret"), "id-1"
        )
        assert client.delete("/api/chatrooms/Enterprise").status_code == 204

        client.post("/api/chatrooms", json={"name": "Enterprise"})
        history = client.get("/api/session/load-room/Enterprise").json()

        assert history["messages"] == []

    def test_delete_active_room_resets_session_to_default(
        self, client, persistence_root
    ):
        # GIVEN a session sitting in "TNG" with some history — and a
        # persisted conversation in the implicit "default" room as well,
        # so the assertion below can prove the reset LOADED default's
        # history rather than merely clearing the session:
        persistence.persist_message("TNG", ChatMessage(role="user", content="hi"), "id-1")
        persistence.persist_message(
            "default", ChatMessage(role="user", content="default-chat"), "id-d"
        )
        client.get("/api/session/load-room/TNG")
        assert client.get("/api/session").json()["current_room"] == "TNG"

        # WHEN the active room is deleted:
        resp = client.delete("/api/chatrooms/TNG")

        # THEN the session is back in "default" carrying default's persisted
        # history (a bare reset to an empty session would leave the user
        # staring at a blank room even though the conversation is on disk),
        # and the deleted room's persistence is gone:
        assert resp.status_code == 204
        state = client.get("/api/session").json()
        assert state["current_room"] == "default"
        assert [m["content"] for m in state["history"]] == ["default-chat"]
        assert not (persistence_root / "TNG").exists()

    def test_delete_active_room_match_is_case_insensitive(
        self, client, persistence_root
    ):
        persistence.persist_message("TNG", ChatMessage(role="user", content="hi"), "id-1")
        client.get("/api/session/load-room/TNG")

        resp = client.delete("/api/chatrooms/tng")

        assert resp.status_code == 204
        assert client.get("/api/session").json()["current_room"] == "default"
        assert not (persistence_root / "TNG").exists()

    def test_delete_inactive_room_keeps_the_active_session(self, client):
        # GIVEN a session sitting in another room:
        client.post("/api/chatrooms", json={"name": "Enterprise"})
        persistence.persist_message(
            "Enterprise", ChatMessage(role="user", content="hi"), "id-1"
        )
        client.get("/api/session/load-room/Enterprise")

        # WHEN a non-active room is deleted:
        resp = client.delete("/api/chatrooms/TNG")

        # THEN the session is untouched (still in the other room, same history):
        assert resp.status_code == 204
        state = client.get("/api/session").json()
        assert state["current_room"] == "Enterprise"
        assert [m["content"] for m in state["history"]] == ["hi"]


# ---------------------------------------------------------------------------
# Persona assignment
# ---------------------------------------------------------------------------

class TestAssignPersonas:
    def test_assign_adds_personas_without_duplicates(self, client):
        resp = client.put("/api/chatrooms/TNG/personas", json={"persona_names": ["Luna", "Alex"]})
        assert resp.status_code == 200
        # Already assigned — order preserved, no duplicates.
        assert resp.json()["persona_names"] == ["Alex", "Luna"]

    def test_assign_appends_new_persona(self, client):
        import app.config as app_config
        from app.config import Persona

        personas = app_config.get_personas()
        personas.personas.append(
            Persona(name="Data", system_prompt="You are Data.", router_hints="logic"))
        resp = client.put("/api/chatrooms/TNG/personas", json={"persona_names": ["Data"]})
        assert resp.status_code == 200
        assert resp.json()["persona_names"] == ["Alex", "Luna", "Data"]

    def test_assign_default_room_rejected(self, client):
        resp = client.put("/api/chatrooms/default/personas", json={"persona_names": ["Alex"]})
        assert resp.status_code == 400

    def test_assign_unknown_room_404(self, client):
        resp = client.put("/api/chatrooms/NoSuchRoom/personas", json={"persona_names": ["Alex"]})
        assert resp.status_code == 404

    def test_assign_nonexistent_persona_422(self, client):
        resp = client.put("/api/chatrooms/TNG/personas", json={"persona_names": ["Q"]})
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    def test_assign_empty_list_rejected_by_model(self, client):
        resp = client.put("/api/chatrooms/TNG/personas", json={"persona_names": []})
        assert resp.status_code == 422


class TestRemovePersonaFromRoom:
    def test_remove_persona(self, client):
        resp = client.delete("/api/chatrooms/TNG/personas/Luna")
        assert resp.status_code == 200
        assert resp.json()["persona_names"] == ["Alex"]

    def test_remove_persona_not_in_room_is_noop(self, client):
        resp = client.delete("/api/chatrooms/TNG/personas/Q")
        assert resp.status_code == 200
        assert resp.json()["persona_names"] == ["Alex", "Luna"]

    def test_remove_from_default_room_rejected(self, client):
        resp = client.delete("/api/chatrooms/default/personas/Alex")
        assert resp.status_code == 400

    def test_remove_unknown_room_404(self, client):
        resp = client.delete("/api/chatrooms/NoSuchRoom/personas/Alex")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Echo chamber
# ---------------------------------------------------------------------------

class TestEchoChamber:
    def test_enable_echo_chamber(self, client):
        resp = client.put("/api/chatrooms/TNG/echo-chamber", json={"echo_chamber": True})
        assert resp.status_code == 200
        assert resp.json()["echo_chamber"] is True

    def test_disable_echo_chamber_preserves_personas(self, client):
        client.put("/api/chatrooms/TNG/echo-chamber", json={"echo_chamber": True})
        resp = client.put("/api/chatrooms/TNG/echo-chamber", json={"echo_chamber": False})
        assert resp.json()["echo_chamber"] is False
        assert resp.json()["persona_names"] == ["Alex", "Luna"]

    def test_echo_chamber_default_room_rejected(self, client):
        resp = client.put("/api/chatrooms/default/echo-chamber", json={"echo_chamber": True})
        assert resp.status_code == 400

    def test_echo_chamber_unknown_room_404(self, client):
        resp = client.put("/api/chatrooms/NoSuchRoom/echo-chamber", json={"echo_chamber": True})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# STT language policy override
# ---------------------------------------------------------------------------

class TestSTTLanguagePolicy:
    def test_room_has_no_override_by_default(self, client):
        resp = client.get("/api/chatrooms/TNG")
        assert resp.json()["stt_language_policy"] is None

    def test_set_policy_returns_it_in_response(self, client):
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={
            "mode": "primary_fallback", "primary_language": "it",
            "fallback_language": "en", "fallback_threshold": 0.8,
        })
        assert resp.status_code == 200
        assert resp.json()["stt_language_policy"] == {
            "mode": "primary_fallback", "primary_language": "it",
            "fallback_language": "en", "fallback_threshold": 0.8,
        }

    # -- required-field validation (fixed / primary_fallback) --------------

    def test_fixed_mode_without_primary_language_rejected_422(self, client):
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "fixed"})
        assert resp.status_code == 422
        assert "primary_language" in resp.text

    def test_primary_fallback_without_fallback_language_rejected_422(self, client):
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={
            "mode": "primary_fallback", "primary_language": "it",
        })
        assert resp.status_code == 422
        assert "fallback_language" in resp.text

    def test_primary_fallback_without_primary_language_rejected_422(self, client):
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={
            "mode": "primary_fallback", "fallback_language": "en",
        })
        assert resp.status_code == 422
        assert "primary_language" in resp.text

    def test_primary_fallback_equal_languages_rejected_422(self, client):
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={
            "mode": "primary_fallback", "primary_language": "it", "fallback_language": "it",
        })
        assert resp.status_code == 422
        assert "must differ" in resp.text

    def test_auto_mode_without_any_language_is_valid(self, client):
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "auto"})
        assert resp.status_code == 200

    def test_rejected_policy_does_not_overwrite_existing_override(self, client):
        # An invalid PUT must not touch the room's currently saved policy.
        client.put("/api/chatrooms/TNG/stt-language-policy",
                   json={"mode": "fixed", "primary_language": "it"})
        resp = client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "fixed"})
        assert resp.status_code == 422

        room = client.get("/api/chatrooms/TNG").json()
        assert room["stt_language_policy"] == {
            "mode": "fixed", "primary_language": "it",
            "fallback_language": None, "fallback_threshold": 0.80,
        }

    def test_set_policy_persists_and_is_visible_on_get(self, client):
        client.put("/api/chatrooms/TNG/stt-language-policy", json={
            "mode": "fixed", "primary_language": "it",
        })
        resp = client.get("/api/chatrooms/TNG")
        assert resp.json()["stt_language_policy"]["mode"] == "fixed"
        assert resp.json()["stt_language_policy"]["primary_language"] == "it"

    def test_clear_policy_reverts_to_none(self, client):
        client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "fixed", "primary_language": "it"})
        resp = client.delete("/api/chatrooms/TNG/stt-language-policy")
        assert resp.status_code == 200
        assert resp.json()["stt_language_policy"] is None

    def test_default_room_rejected_on_set(self, client):
        resp = client.put("/api/chatrooms/default/stt-language-policy",
                          json={"mode": "fixed", "primary_language": "it"})
        assert resp.status_code == 400

    def test_default_room_rejected_on_clear(self, client):
        resp = client.delete("/api/chatrooms/default/stt-language-policy")
        assert resp.status_code == 400

    def test_unknown_room_404_on_set(self, client):
        resp = client.put("/api/chatrooms/NoSuchRoom/stt-language-policy",
                          json={"mode": "fixed", "primary_language": "it"})
        assert resp.status_code == 404

    def test_unknown_room_404_on_clear(self, client):
        resp = client.delete("/api/chatrooms/NoSuchRoom/stt-language-policy")
        assert resp.status_code == 404

    def test_policy_survives_persona_assignment(self, client):
        # A previous bug: reconstructing ChatRoom by hand in assign_personas
        # dropped any field it didn't explicitly carry over.
        client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "fixed", "primary_language": "it"})
        resp = client.put("/api/chatrooms/TNG/personas", json={"persona_names": ["Alex"]})
        assert resp.json()["stt_language_policy"]["mode"] == "fixed"

    def test_policy_survives_persona_removal(self, client):
        client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "fixed", "primary_language": "it"})
        resp = client.delete("/api/chatrooms/TNG/personas/Alex")
        assert resp.json()["stt_language_policy"]["mode"] == "fixed"

    def test_policy_survives_echo_chamber_toggle(self, client):
        client.put("/api/chatrooms/TNG/stt-language-policy", json={"mode": "fixed", "primary_language": "it"})
        resp = client.put("/api/chatrooms/TNG/echo-chamber", json={"echo_chamber": True})
        assert resp.json()["stt_language_policy"]["mode"] == "fixed"
