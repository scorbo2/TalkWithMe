"""API tests for app/routers/personas.py — persona CRUD, cascades, clone, avatars."""

from tests.factories import make_personas


# ---------------------------------------------------------------------------
# GET /api/personas
# ---------------------------------------------------------------------------

class TestListPersonas:
    def test_returns_all_personas_with_tts_capability_flags(self, client):
        resp = client.get("/api/personas")
        assert resp.status_code == 200
        body = resp.json()
        assert [p["name"] for p in body] == ["Alex", "Luna"]
        by_name = {p["name"]: p for p in body}
        assert by_name["Alex"]["tts_capable"] is False   # no reference audio
        assert by_name["Luna"]["tts_capable"] is True    # audio + transcript
        assert by_name["Luna"]["description"] == "A philosophical poet"


# ---------------------------------------------------------------------------
# POST /api/personas
# ---------------------------------------------------------------------------

class TestCreatePersona:
    def _payload(self, **overrides):
        payload = {
            "name": "Data",
            "description": "A logic-driven captain",
            "system_prompt": "You are Data.",
            "router_hints": "logic, science",
        }
        payload.update(overrides)
        return payload

    def test_create_appends_persona_and_persists_to_yaml(self, client, tmp_project_root):
        resp = client.post("/api/personas", json=self._payload())
        assert resp.status_code == 201
        assert resp.json()["name"] == "Data"

        # In-memory list now includes it...
        names = [p["name"] for p in client.get("/api/personas").json()]
        assert names == ["Alex", "Luna", "Data"]
        # ...and it landed in personas.yaml (which the fixture redirects to tmp).
        assert (tmp_project_root / "personas.yaml").exists()

    def test_create_strips_whitespace_in_name(self, client):
        resp = client.post("/api/personas", json=self._payload(name="  Worf  "))
        assert resp.status_code == 201
        assert resp.json()["name"] == "Worf"

    def test_create_blank_name_rejected(self, client):
        resp = client.post("/api/personas", json=self._payload(name="   "))
        assert resp.status_code == 422

    def test_create_reserved_name_user_rejected(self, client):
        resp = client.post("/api/personas", json=self._payload(name="user"))
        assert resp.status_code == 422
        assert "reserved" in resp.json()["detail"]

    def test_create_reserved_name_case_insensitive(self, client):
        resp = client.post("/api/personas", json=self._payload(name="USER"))
        assert resp.status_code == 422

    def test_create_duplicate_name_rejected_case_insensitively(self, client):
        resp = client.post("/api/personas", json=self._payload(name="alex"))
        assert resp.status_code == 409

    def test_create_validation_rejects_missing_system_prompt(self, client):
        payload = self._payload()
        del payload["system_prompt"]
        resp = client.post("/api/personas", json=payload)
        assert resp.status_code == 422

    def test_create_validation_rejects_long_name(self, client):
        resp = client.post("/api/personas", json=self._payload(name="x" * 26))
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/personas/{name}/detail
# ---------------------------------------------------------------------------

class TestGetPersonaDetail:
    def test_returns_all_editable_fields(self, client):
        resp = client.get("/api/personas/Luna/detail")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Luna"
        assert body["system_prompt"] == "You are Luna, a philosophical poet."
        assert body["reference_audio"] == "reference/luna.wav"
        assert body["reference_audio_language"] == "en"
        assert body["tts_capable"] is True

    def test_unknown_persona_404(self, client):
        resp = client.get("/api/personas/NoSuchOne/detail")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/personas/{name}
# ---------------------------------------------------------------------------

class TestUpdatePersona:
    def _payload(self, **overrides):
        payload = {
            "name": "Alex",
            "description": "Updated description",
            "system_prompt": "You are Alex, but updated.",
            "router_hints": "general questions",
        }
        payload.update(overrides)
        return payload

    def test_update_fields(self, client):
        resp = client.put("/api/personas/Alex", json=self._payload())
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"
        assert resp.json()["system_prompt"] == "You are Alex, but updated."

    def test_rename_cascades_to_chatrooms(self, client):
        resp = client.put("/api/personas/Alex", json=self._payload(name="Alexander"))
        assert resp.status_code == 200
        assert resp.json()["name"] == "Alexander"

        rooms = client.get("/api/chatrooms").json()
        tng = next(r for r in rooms if r["name"] == "TNG")
        assert "Alexander" in tng["persona_names"]
        assert "Alex" not in tng["persona_names"]
        assert tng["persona_names"] == ["Alexander", "Luna"]

    def test_rename_to_existing_name_rejected(self, client):
        resp = client.put("/api/personas/Alex", json=self._payload(name="luna"))
        assert resp.status_code == 409

    def test_rename_to_reserved_user_rejected(self, client):
        resp = client.put("/api/personas/Alex", json=self._payload(name="User"))
        assert resp.status_code == 422

    def test_blank_name_rejected(self, client):
        resp = client.put("/api/personas/Alex", json=self._payload(name="  "))
        assert resp.status_code == 422

    def test_unknown_persona_404(self, client):
        resp = client.put("/api/personas/NoSuchOne", json=self._payload(name="NoSuchOne"))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/personas/{name}
# ---------------------------------------------------------------------------

class TestDeletePersona:
    def test_delete_removes_persona_and_cascades_to_chatrooms(self, client):
        resp = client.delete("/api/personas/Luna")
        assert resp.status_code == 204

        names = [p["name"] for p in client.get("/api/personas").json()]
        assert names == ["Alex"]
        tng = next(r for r in client.get("/api/chatrooms").json() if r["name"] == "TNG")
        assert tng["persona_names"] == ["Alex"]

    def test_delete_unknown_persona_404(self, client):
        resp = client.delete("/api/personas/NoSuchOne")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/personas/{name}/clone
# ---------------------------------------------------------------------------

class TestClonePersona:
    def test_clone_appends_numeric_suffix_and_copies_fields(self, client):
        resp = client.post("/api/personas/Alex/clone")
        assert resp.status_code == 201
        clone = resp.json()
        assert clone["name"] == "Alex_2"
        assert clone["system_prompt"] == "You are Alex, a friendly assistant."
        assert clone["description"] == "A friendly assistant"

    def test_clone_skips_taken_suffixes(self, client):
        client.post("/api/personas/Alex/clone")  # creates Alex_2
        resp = client.post("/api/personas/Alex/clone")
        assert resp.status_code == 201
        assert resp.json()["name"] == "Alex_3"

    def test_clone_of_tts_capable_keeps_reference_files(self, client):
        resp = client.post("/api/personas/Luna/clone")
        assert resp.status_code == 201
        clone = resp.json()
        assert clone["name"] == "Luna_2"
        assert clone["reference_audio"] == "reference/luna.wav"
        assert clone["tts_capable"] is True

    def test_clone_unknown_persona_404(self, client):
        resp = client.post("/api/personas/NoSuchOne/clone")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/personas/{name}/avatar
# ---------------------------------------------------------------------------

class TestGetAvatar:
    def test_no_avatar_configured_404(self, client):
        resp = client.get("/api/personas/Alex/avatar")
        assert resp.status_code == 404

    def test_avatar_file_missing_404(self, client, monkeypatch):
        import app.config as app_config

        personas = make_personas()
        personas.personas[0].avatar_image = "/nonexistent/avatar.png"
        monkeypatch.setattr(app_config, "_personas_cache", personas)

        resp = client.get("/api/personas/Alex/avatar")
        assert resp.status_code == 404

    def test_serves_avatar_bytes(self, client, monkeypatch, tmp_path):
        import app.config as app_config

        avatar = tmp_path / "alex.png"
        avatar.write_bytes(b"\x89PNG fake bytes")
        personas = make_personas()
        personas.personas[0].avatar_image = str(avatar)
        monkeypatch.setattr(app_config, "_personas_cache", personas)

        resp = client.get("/api/personas/Alex/avatar")
        assert resp.status_code == 200
        assert resp.content == b"\x89PNG fake bytes"

    def test_unknown_persona_avatar_404(self, client):
        resp = client.get("/api/personas/NoSuchOne/avatar")
        assert resp.status_code == 404
