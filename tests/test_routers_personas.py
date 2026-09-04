"""API tests for app/routers/personas.py — persona CRUD, cascades, clone, avatars.

Personas are backed by real directories (make_personas_in_dir), so every
test can assert both the API response and the files on disk. Create/update
are multipart/form-data; the TestClient sends form data as urlencoded when
no files are attached, which FastAPI's Form fields parse identically.
"""

import pytest

import app.config as app_config
from app.services import persona_store
from tests.factories import make_personas_in_dir, rescan_personas


@pytest.fixture
def personas_root(tmp_project_root):
    """Materialize the stock Alex/Luna set as real directories and point the
    in-memory cache at the scan result (persona_dir set, real on-disk paths)."""
    root = tmp_project_root / "Personas"
    app_config.set_personas_cache(make_personas_in_dir(root))
    return root


# ---------------------------------------------------------------------------
# GET /api/personas
# ---------------------------------------------------------------------------

class TestListPersonas:
    def test_returns_all_personas_with_tts_capability_flags(self, client, personas_root):
        resp = client.get("/api/personas")
        assert resp.status_code == 200
        body = resp.json()
        assert [p["name"] for p in body] == ["Alex", "Luna"]
        by_name = {p["name"]: p for p in body}
        assert by_name["Alex"]["tts_capable"] is False   # no reference audio
        assert by_name["Luna"]["tts_capable"] is True    # audio + transcript
        assert by_name["Luna"]["description"] == "A philosophical poet"
        # avatar_image is a presence flag, not a path.
        assert by_name["Alex"]["avatar_image"] is False
        assert by_name["Luna"]["avatar_image"] is False


# ---------------------------------------------------------------------------
# POST /api/personas
# ---------------------------------------------------------------------------

class TestCreatePersona:
    def _data(self, **overrides):
        data = {
            "name": "Data",
            "description": "A logic-driven captain",
            "system_prompt": "You are Data.",
            "router_hints": "logic, science",
            "avatar_color": "#4A90D9",
            "reference_audio_language": "en",
            "allow_tool_calls": "false",
            "reference_audio_transcript": "",
            "remove_avatar_image": "false",
            "remove_reference_audio": "false",
        }
        data.update(overrides)
        return data

    def test_create_writes_persona_directory_and_updates_cache(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data())
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Data"
        assert body["description"] == "A logic-driven captain"
        assert body["system_prompt"] == "You are Data."
        assert body["avatar_image"] is False
        assert body["reference_audio"] is False
        assert body["reference_audio_transcript"] is None
        assert body["tts_capable"] is False

        # In-memory list now includes it...
        names = [p["name"] for p in client.get("/api/personas").json()]
        assert names == ["Alex", "Luna", "Data"]
        # ...and it landed in its own directory (not in personas.yaml).
        persona_dir = personas_root / "Data"
        assert (persona_dir / "prompt.md").exists()
        assert (persona_dir / "language.txt").read_text() == "en"
        # No transcript text was sent -> no ref.txt on disk.
        assert not (persona_dir / "ref.txt").exists()

    def test_create_with_file_uploads(self, client, personas_root):
        data = self._data(reference_audio_transcript="Beverage of choice: red or clear.")
        files = {
            "avatar_image": ("data.png", b"PNGDATA", "image/png"),
            "reference_audio": ("data.wav", b"WAVDATA", "audio/wav"),
        }
        resp = client.post("/api/personas", data=data, files=files)
        assert resp.status_code == 201
        body = resp.json()
        assert body["avatar_image"] is True
        assert body["reference_audio"] is True
        assert body["reference_audio_transcript"] == "Beverage of choice: red or clear."
        assert body["tts_capable"] is True

        persona_dir = personas_root / "Data"
        assert (persona_dir / "image.png").read_bytes() == b"PNGDATA"
        assert (persona_dir / "ref.wav").read_bytes() == b"WAVDATA"
        assert (persona_dir / "ref.txt").read_text() == "Beverage of choice: red or clear."

    def test_create_strips_whitespace_in_name(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(name="  Worf  "))
        assert resp.status_code == 201
        assert resp.json()["name"] == "Worf"
        assert (personas_root / "Worf").is_dir()

    def test_create_blank_name_rejected(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(name="   "))
        assert resp.status_code == 422

    def test_create_name_without_usable_directory_chars_rejected(self, client, personas_root):
        # '---' would actually be a legal directory name; '???' sanitizes
        # to nothing and must be rejected.
        resp = client.post("/api/personas", data=self._data(name="???"))
        assert resp.status_code == 422
        assert "letter, number, space, hyphen or underscore" in resp.json()["detail"]

    def test_create_reserved_name_user_rejected(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(name="user"))
        assert resp.status_code == 422
        assert "reserved" in resp.json()["detail"]

    def test_create_reserved_name_case_insensitive(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(name="USER"))
        assert resp.status_code == 422

    def test_create_duplicate_name_rejected_case_insensitively(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(name="alex"))
        assert resp.status_code == 409

    def test_create_validation_rejects_missing_system_prompt(self, client, personas_root):
        data = self._data()
        del data["system_prompt"]
        resp = client.post("/api/personas", data=data)
        assert resp.status_code == 422

    def test_create_validation_rejects_long_name(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(name="x" * 26))
        assert resp.status_code == 422

    def test_create_validation_rejects_bad_language_length(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(reference_audio_language="eng"))
        assert resp.status_code == 422

    def test_create_directory_collision_gets_unique_dir_name(self, client, personas_root):
        # "O'Brien" sanitizes to the "OBrien" directory.
        resp = client.post("/api/personas", data=self._data(name="O'Brien"))
        assert resp.status_code == 201
        assert (personas_root / "OBrien").is_dir()

        # A different persona name sanitizing to the same directory gets a
        # suffixed directory instead of clobbering the first one.
        resp = client.post("/api/personas", data=self._data(name="OBrien"))
        assert resp.status_code == 201
        assert (personas_root / "OBrien_2").is_dir()

    def test_create_unsupported_image_extension_rejected(self, client, personas_root):
        resp = client.post(
            "/api/personas", data=self._data(),
            files={"avatar_image": ("data.bmp", b"BMP", "image/bmp")},
        )
        assert resp.status_code == 422
        assert "Unsupported avatar image" in resp.json()["detail"]

    def test_create_unsupported_audio_extension_rejected(self, client, personas_root):
        resp = client.post(
            "/api/personas", data=self._data(),
            files={"reference_audio": ("data.mp3", b"MP3", "audio/mpeg")},
        )
        assert resp.status_code == 422
        assert "wav" in resp.json()["detail"]

    def test_create_oversized_image_rejected(self, client, personas_root):
        huge = b"\x00" * (persona_store.MAX_IMAGE_BYTES + 1)
        resp = client.post(
            "/api/personas", data=self._data(),
            files={"avatar_image": ("data.png", huge, "image/png")},
        )
        assert resp.status_code == 422
        assert "limit" in resp.json()["detail"]

    def test_create_oversized_audio_rejected(self, client, personas_root):
        huge = b"\x00" * (persona_store.MAX_AUDIO_BYTES + 1)
        resp = client.post(
            "/api/personas", data=self._data(),
            files={"reference_audio": ("data.wav", huge, "audio/wav")},
        )
        assert resp.status_code == 422

    def test_create_memory_size_defaults_to_8192(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data())
        assert resp.status_code == 201
        assert resp.json()["memory_size"] == 8192
        fields, _ = persona_store.parse_frontmatter(
            (personas_root / "Data" / "prompt.md").read_text()
        )
        assert fields["memory_size"] == 8192

    def test_create_memory_size_custom_value_persisted(self, client, personas_root):
        resp = client.post("/api/personas", data=self._data(memory_size="4096"))
        assert resp.status_code == 201
        assert resp.json()["memory_size"] == 4096
        fields, _ = persona_store.parse_frontmatter(
            (personas_root / "Data" / "prompt.md").read_text()
        )
        assert fields["memory_size"] == 4096

    @pytest.mark.parametrize("value", ["-1", "16385", "not-a-number"])
    def test_create_memory_size_out_of_range_rejected(self, client, personas_root, value):
        resp = client.post("/api/personas", data=self._data(memory_size=value))
        assert resp.status_code == 422
        assert not (personas_root / "Data").exists()


# ---------------------------------------------------------------------------
# GET /api/personas/{name}/detail
# ---------------------------------------------------------------------------

class TestGetPersonaDetail:
    def test_returns_all_editable_fields(self, client, personas_root):
        resp = client.get("/api/personas/Luna/detail")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Luna"
        assert body["description"] == "A philosophical poet"
        assert body["system_prompt"] == "You are Luna, a philosophical poet."
        assert body["router_hints"] == "philosophy, feelings"
        assert body["avatar_image"] is False
        assert body["reference_audio"] is True
        # The transcript is file CONTENTS, not a path.
        assert body["reference_audio_transcript"] == "The stars are just pinpricks in the dark."
        assert body["reference_audio_language"] == "en"
        assert body["allow_tool_calls"] is False
        assert body["tts_capable"] is True
        # Stock personas predate the field: no key in frontmatter -> default.
        assert body["memory_size"] == 8192

    def test_detail_reports_custom_memory_size(self, client, personas_root):
        # Rewrite Luna's prompt.md with a custom budget, then refresh the
        # cache by re-scanning (rescan writes nothing — the custom file
        # must survive).
        persona_store.write_prompt_md(
            personas_root / "Luna",
            name="Luna",
            description="A philosophical poet",
            router_hints="philosophy, feelings",
            avatar_color="#888888",
            allow_tool_calls=False,
            system_prompt="You are Luna, a philosophical poet.",
            memory_size=2048,
        )
        app_config.set_personas_cache(rescan_personas(personas_root))

        body = client.get("/api/personas/Luna/detail").json()
        assert body["memory_size"] == 2048

    def test_detail_without_files_reports_absent(self, client, personas_root):
        body = client.get("/api/personas/Alex/detail").json()
        assert body["avatar_image"] is False
        assert body["reference_audio"] is False
        assert body["reference_audio_transcript"] is None
        assert body["tts_capable"] is False

    def test_unknown_persona_404(self, client, personas_root):
        resp = client.get("/api/personas/NoSuchOne/detail")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/personas/{name}
# ---------------------------------------------------------------------------

class TestUpdatePersona:
    def _data(self, **overrides):
        data = {
            "name": "Alex",
            "description": "Updated description",
            "system_prompt": "You are Alex, but updated.",
            "router_hints": "general questions",
            "avatar_color": "#4A90D9",
            "reference_audio_language": "en",
            "allow_tool_calls": "false",
            "reference_audio_transcript": "",
            # REQUIRED on update (no server-side default): an omitted value
            # must 422 rather than silently reset the memory budget.
            "memory_size": "8192",
            "remove_avatar_image": "false",
            "remove_reference_audio": "false",
        }
        data.update(overrides)
        return data

    def test_update_fields(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data())
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"
        assert resp.json()["system_prompt"] == "You are Alex, but updated."
        # The changes are on disk, not just in memory.
        prompt = (personas_root / "Alex" / "prompt.md").read_text()
        assert "You are Alex, but updated." in prompt
        assert "Updated description" in prompt

    def test_rename_updates_frontmatter_cascades_and_keeps_directory(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data(name="Alexander"))
        assert resp.status_code == 200
        assert resp.json()["name"] == "Alexander"

        # The directory keeps its original name; the frontmatter carries
        # the new one (renaming directories would break external paths).
        assert (personas_root / "Alex").is_dir()
        prompt = (personas_root / "Alex" / "prompt.md").read_text()
        assert "name: Alexander" in prompt

        rooms = client.get("/api/chatrooms").json()
        tng = next(r for r in rooms if r["name"] == "TNG")
        assert tng["persona_names"] == ["Alexander", "Luna"]

    def test_rename_to_existing_name_rejected(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data(name="luna"))
        assert resp.status_code == 409

    def test_rename_to_reserved_user_rejected(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data(name="User"))
        assert resp.status_code == 422

    def test_blank_name_rejected(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data(name="  "))
        assert resp.status_code == 422

    def test_unknown_persona_404(self, client, personas_root):
        resp = client.put("/api/personas/NoSuchOne", data=self._data(name="NoSuchOne"))
        assert resp.status_code == 404

    def test_update_with_new_image_replaces_existing(self, client, personas_root):
        # Give Alex a png avatar, then update with a webp: the png must go.
        persona_store.write_avatar_file(personas_root / "Alex", b"OLDPNG", ".png")
        app_config.set_personas_cache(rescan_personas(personas_root))

        resp = client.put(
            "/api/personas/Alex", data=self._data(),
            files={"avatar_image": ("alex.webp", b"NEWWEBP", "image/webp")},
        )
        assert resp.status_code == 200
        assert resp.json()["avatar_image"] is True
        assert not (personas_root / "Alex" / "image.png").exists()
        assert (personas_root / "Alex" / "image.webp").read_bytes() == b"NEWWEBP"

    def test_update_with_new_audio_replaces_existing(self, client, personas_root):
        resp = client.put(
            "/api/personas/Luna",
            data=self._data(name="Luna"),
            files={"reference_audio": ("luna.wav", b"NEWWAV", "audio/wav")},
        )
        assert resp.status_code == 200
        assert (personas_root / "Luna" / "ref.wav").read_bytes() == b"NEWWAV"

    def test_update_remove_image_flag_removes_file(self, client, personas_root):
        persona_store.write_avatar_file(personas_root / "Alex", b"PNGDATA", ".png")
        app_config.set_personas_cache(rescan_personas(personas_root))

        resp = client.put("/api/personas/Alex", data=self._data(remove_avatar_image="true"))
        assert resp.status_code == 200
        assert resp.json()["avatar_image"] is False
        assert not (personas_root / "Alex" / "image.png").exists()

    def test_update_remove_audio_flag_removes_file(self, client, personas_root):
        resp = client.put(
            "/api/personas/Luna",
            data=self._data(name="Luna", remove_reference_audio="true"),
        )
        assert resp.status_code == 200
        assert resp.json()["reference_audio"] is False
        assert resp.json()["tts_capable"] is False  # no audio -> not TTS-capable
        assert not (personas_root / "Luna" / "ref.wav").exists()

    def test_update_blank_transcript_removes_ref_txt(self, client, personas_root):
        resp = client.put(
            "/api/personas/Luna",
            data=self._data(name="Luna", reference_audio_transcript="   "),
        )
        assert resp.status_code == 200
        assert resp.json()["reference_audio_transcript"] is None
        assert resp.json()["tts_capable"] is False
        assert not (personas_root / "Luna" / "ref.txt").exists()

    def test_update_new_transcript_written(self, client, personas_root):
        resp = client.put(
            "/api/personas/Luna",
            data=self._data(name="Luna", reference_audio_transcript="A new transcript."),
        )
        assert resp.status_code == 200
        assert resp.json()["reference_audio_transcript"] == "A new transcript."
        assert (personas_root / "Luna" / "ref.txt").read_text() == "A new transcript."

    def test_update_unsupported_image_extension_rejected(self, client, personas_root):
        resp = client.put(
            "/api/personas/Alex", data=self._data(),
            files={"avatar_image": ("alex.bmp", b"BMP", "image/bmp")},
        )
        assert resp.status_code == 422
        # The existing files are untouched by a failed update.
        assert not (personas_root / "Alex" / "image.bmp").exists()

    def test_update_requires_memory_size(self, client, personas_root):
        data = self._data()
        del data["memory_size"]
        resp = client.put("/api/personas/Alex", data=data)
        assert resp.status_code == 422

    def test_update_memory_size_persisted_to_frontmatter(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data(memory_size="4096"))
        assert resp.status_code == 200
        assert resp.json()["memory_size"] == 4096
        fields, _ = persona_store.parse_frontmatter(
            (personas_root / "Alex" / "prompt.md").read_text()
        )
        assert fields["memory_size"] == 4096

    def test_update_out_of_range_memory_size_rejected(self, client, personas_root):
        resp = client.put("/api/personas/Alex", data=self._data(memory_size="16385"))
        assert resp.status_code == 422

    def test_update_preserves_memories_within_new_limit(self, client, personas_root):
        memories = personas_root / "Alex" / "memories.txt"
        memories.write_text("abc\n")  # 4 bytes, well within 8192
        resp = client.put("/api/personas/Alex", data=self._data(memory_size="8192"))
        assert resp.status_code == 200
        assert memories.read_text() == "abc\n"

    def test_update_lowered_memory_size_purges_oldest_first(self, client, personas_root):
        memories = personas_root / "Alex" / "memories.txt"
        memories.write_text("a1\na2\na3\na4\n")  # 12 bytes
        resp = client.put("/api/personas/Alex", data=self._data(memory_size="6"))
        assert resp.status_code == 200
        assert resp.json()["memory_size"] == 6
        # Oldest lines dropped until under the new budget; newest survives.
        assert memories.read_text() == "a4\n"

    def test_update_memory_size_zero_deletes_memories(self, client, personas_root):
        memories = personas_root / "Alex" / "memories.txt"
        memories.write_text("stale\n")
        resp = client.put("/api/personas/Alex", data=self._data(memory_size="0"))
        assert resp.status_code == 200
        assert resp.json()["memory_size"] == 0
        assert not memories.exists()

    def test_update_clear_memories_flag_deletes_file(self, client, personas_root):
        memories = personas_root / "Alex" / "memories.txt"
        memories.write_text("the user likes tea\n")
        resp = client.put(
            "/api/personas/Alex",
            data=self._data(memory_size="8192", clear_memories="true"),
        )
        assert resp.status_code == 200
        assert not memories.exists()
        # The persona itself is still fine — only the memories went.
        assert resp.json()["name"] == "Alex"


# ---------------------------------------------------------------------------
# DELETE /api/personas/{name}
# ---------------------------------------------------------------------------

class TestDeletePersona:
    def test_delete_removes_directory_cache_entry_and_cascades(self, client, personas_root):
        resp = client.delete("/api/personas/Luna")
        assert resp.status_code == 204

        names = [p["name"] for p in client.get("/api/personas").json()]
        assert names == ["Alex"]
        tng = next(r for r in client.get("/api/chatrooms").json() if r["name"] == "TNG")
        assert tng["persona_names"] == ["Alex"]
        # The whole directory is gone.
        assert not (personas_root / "Luna").exists()

    def test_delete_unknown_persona_404(self, client, personas_root):
        resp = client.delete("/api/personas/NoSuchOne")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/personas/{name}/clone
# ---------------------------------------------------------------------------

class TestClonePersona:
    def test_clone_copies_files_and_keeps_system_prompt(self, client, personas_root):
        resp = client.post("/api/personas/Luna/clone")
        assert resp.status_code == 201
        clone = resp.json()
        assert clone["name"] == "Luna_2"
        assert clone["system_prompt"] == "You are Luna, a philosophical poet."
        assert clone["description"] == "A philosophical poet"
        assert clone["reference_audio"] is True
        assert clone["tts_capable"] is True

        new_dir = personas_root / "Luna_2"
        # Files were copied, not merely referenced.
        assert (new_dir / "ref.wav").read_bytes() == b"RIFF-fake-wav"
        assert (new_dir / "ref.txt").read_text() == "The stars are just pinpricks in the dark."
        # The clone's prompt.md still carries the persona's prompt.
        assert "You are Luna, a philosophical poet." in (new_dir / "prompt.md").read_text()

    def test_clone_of_frontmatter_named_persona_rewrites_name_field(self, client, personas_root):
        # "O'Brien" lives in the "OBrien" directory, so its prompt.md carries
        # an explicit `name:` field. A raw copytree would leave the clone
        # claiming the source's name — the rewrite must fix that.
        create_data = {
            "name": "O'Brien",
            "description": "A gruff counselor",
            "system_prompt": "You are O'Brien.",
            "router_hints": "feelings",
            "avatar_color": "#FF0000",
            "reference_audio_language": "en",
            "allow_tool_calls": "false",
            "reference_audio_transcript": "",
            "remove_avatar_image": "false",
            "remove_reference_audio": "false",
        }
        assert client.post("/api/personas", data=create_data).status_code == 201
        assert (personas_root / "OBrien" / "prompt.md").exists()

        resp = client.post("/api/personas/O'Brien/clone")
        assert resp.status_code == 201
        assert resp.json()["name"] == "O'Brien_2"

        fields, _ = persona_store.parse_frontmatter(
            (personas_root / "OBrien_2" / "prompt.md").read_text()
        )
        assert fields["name"] == "O'Brien_2"

    def test_clone_carries_over_memory_size(self, client, personas_root):
        create_data = {
            "name": "Data",
            "description": "A logic-driven captain",
            "system_prompt": "You are Data.",
            "router_hints": "logic, science",
            "avatar_color": "#4A90D9",
            "reference_audio_language": "en",
            "allow_tool_calls": "false",
            "reference_audio_transcript": "",
            "memory_size": "4096",
            "remove_avatar_image": "false",
            "remove_reference_audio": "false",
        }
        assert client.post("/api/personas", data=create_data).status_code == 201

        resp = client.post("/api/personas/Data/clone")
        assert resp.status_code == 201
        clone = resp.json()
        assert clone["name"] == "Data_2"
        assert clone["memory_size"] == 4096
        # ...and it is on disk in the clone's own frontmatter, not just in
        # the response (a lost key would fall back to the default on reload).
        fields, _ = persona_store.parse_frontmatter(
            (personas_root / "Data_2" / "prompt.md").read_text()
        )
        assert fields["memory_size"] == 4096

    def test_clone_skips_taken_suffixes(self, client, personas_root):
        client.post("/api/personas/Alex/clone")  # creates Alex_2
        resp = client.post("/api/personas/Alex/clone")
        assert resp.status_code == 201
        assert resp.json()["name"] == "Alex_3"

    def test_clone_unknown_persona_404(self, client, personas_root):
        resp = client.post("/api/personas/NoSuchOne/clone")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/personas/{name}/avatar
# ---------------------------------------------------------------------------

class TestGetAvatar:
    def test_no_avatar_configured_404(self, client, personas_root):
        resp = client.get("/api/personas/Alex/avatar")
        assert resp.status_code == 404

    def test_avatar_file_missing_on_disk_404(self, client, personas_root):
        persona_store.write_avatar_file(personas_root / "Alex", b"PNGDATA", ".png")
        app_config.set_personas_cache(rescan_personas(personas_root))
        (personas_root / "Alex" / "image.png").unlink()  # cache still points at it

        resp = client.get("/api/personas/Alex/avatar")
        assert resp.status_code == 404

    def test_serves_avatar_bytes(self, client, personas_root):
        persona_store.write_avatar_file(personas_root / "Alex", b"\x89PNG fake bytes", ".png")
        app_config.set_personas_cache(rescan_personas(personas_root))

        resp = client.get("/api/personas/Alex/avatar")
        assert resp.status_code == 200
        assert resp.content == b"\x89PNG fake bytes"

    def test_unknown_persona_avatar_404(self, client, personas_root):
        resp = client.get("/api/personas/NoSuchOne/avatar")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/personas/{name}/reference-audio
# ---------------------------------------------------------------------------

class TestGetReferenceAudio:
    def test_serves_ref_wav(self, client, personas_root):
        resp = client.get("/api/personas/Luna/reference-audio")
        assert resp.status_code == 200
        assert resp.content == b"RIFF-fake-wav"
        assert resp.headers["content-type"] == "audio/wav"

    def test_no_reference_audio_404(self, client, personas_root):
        resp = client.get("/api/personas/Alex/reference-audio")
        assert resp.status_code == 404

    def test_reference_audio_missing_on_disk_404(self, client, personas_root):
        (personas_root / "Luna" / "ref.wav").unlink()  # cache still points at it
        resp = client.get("/api/personas/Luna/reference-audio")
        assert resp.status_code == 404

    def test_unknown_persona_404(self, client, personas_root):
        resp = client.get("/api/personas/NoSuchOne/reference-audio")
        assert resp.status_code == 404
