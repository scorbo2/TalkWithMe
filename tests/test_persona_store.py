"""Tests for app/services/persona_store.py — prompt.md frontmatter,
directory scanning, file helpers, and the legacy personas.yaml migration.

These are unit tests: they touch real files under pytest's tmp_path but
never the app's config cache (that is the router tests' job).
"""

import pytest
import yaml
from pydantic import ValidationError

from app.services import persona_store
from app.services.persona_store import (
    PersonaMigrationError,
    PersonaStorageError,
    build_prompt_md,
    find_avatar_file,
    load_persona_from_dir,
    load_personas_yaml,
    migrate_from_legacy_yaml,
    parse_frontmatter,
    read_language_file,
    scan_personas_directory,
    sanitize_persona_dirname,
    unique_persona_dirname,
    write_avatar_file,
    write_language_file,
    write_prompt_md,
    write_transcript_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_persona_dir(root, name="Alex", **overrides):
    """Create one minimal valid persona directory and return its path."""
    fields = dict(
        description="A friendly assistant",
        router_hints="general questions",
        avatar_color="#888888",
        allow_tool_calls=False,
        system_prompt="You are Alex, a friendly assistant.",
    )
    fields.update(overrides)
    persona_dir = root / name
    persona_dir.mkdir(parents=True)
    write_prompt_md(persona_dir, name=name, **fields)
    return persona_dir


def write_legacy_yaml(path, personas) -> "path":
    """Write a legacy-format personas.yaml and return its path."""
    path.write_text(yaml.dump({"personas": personas}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# prompt.md frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_wellformed_frontmatter_splits_fields_and_body(self):
        text = "---\nname: Alex\ndescription: A test\n---\n\nYou are Alex.\n"
        fields, body = parse_frontmatter(text)
        assert fields == {"name": "Alex", "description": "A test"}
        assert body == "You are Alex."

    def test_no_frontmatter_returns_whole_file_as_body(self):
        text = "Just a prompt, no frontmatter.\n"
        assert parse_frontmatter(text) == ({}, "Just a prompt, no frontmatter.")

    def test_unterminated_frontmatter_falls_back_to_whole_file(self):
        text = "---\nname: Alex\nYou are Alex.\n"
        assert parse_frontmatter(text) == ({}, text.strip("\n"))

    def test_non_mapping_frontmatter_falls_back_to_whole_file(self):
        text = "---\n- a\n- b\n---\nBody\n"
        # Malformed frontmatter never costs the prompt: the whole file
        # degrades to the system prompt body.
        assert parse_frontmatter(text) == ({}, text.strip("\n"))

    def test_malformed_yaml_frontmatter_falls_back_to_whole_file(self):
        text = "---\nname: [unclosed\n---\nBody\n"
        assert parse_frontmatter(text) == ({}, text.strip("\n"))


class TestBuildPromptMd:
    def test_round_trip_preserves_all_fields(self):
        built = build_prompt_md(
            "Alex",
            name="Alex",
            description="A friendly assistant",
            router_hints="general questions",
            avatar_color="#4A90D9",
            allow_tool_calls=True,
            system_prompt="You are Alex.",
        )
        fields, body = parse_frontmatter(built)
        assert fields == {
            "description": "A friendly assistant",
            "router_hints": "general questions",
            "avatar_color": "#4A90D9",
            "allow_tool_calls": True,
        }
        assert body == "You are Alex."

    def test_name_field_omitted_when_same_as_directory(self):
        built = build_prompt_md(
            "Alex", name="Alex", description="", router_hints="h",
            avatar_color="#fff", allow_tool_calls=False, system_prompt="p",
        )
        # The directory name is the fallback identity, so no `name:` noise.
        assert "name:" not in built.split("---")[1]

    def test_name_field_present_when_different_from_directory(self):
        built = build_prompt_md(
            "OBrien", name="O'Brien", description="", router_hints="h",
            avatar_color="#fff", allow_tool_calls=False, system_prompt="p",
        )
        fields, _ = parse_frontmatter(built)
        assert fields["name"] == "O'Brien"


# ---------------------------------------------------------------------------
# Directory names
# ---------------------------------------------------------------------------

class TestSanitizeDirname:
    def test_strips_directory_hostile_characters(self):
        assert sanitize_persona_dirname("O'Brien") == "OBrien"
        # Stripping deletes characters; it never inserts separators.
        assert sanitize_persona_dirname("a/b\\c.d") == "abcd"

    def test_allows_letters_numbers_spaces_hyphens_underscores(self):
        assert sanitize_persona_dirname("My Room-1_x2") == "My Room-1_x2"

    def test_all_hostile_name_returns_empty_string(self):
        assert sanitize_persona_dirname("???") == ""


class TestUniqueDirname:
    def test_free_name_used_as_is(self, tmp_path):
        assert unique_persona_dirname(tmp_path, "Alex") == "Alex"

    def test_collision_gets_incrementing_suffix(self, tmp_path):
        (tmp_path / "Alex").mkdir()
        assert unique_persona_dirname(tmp_path, "Alex") == "Alex_2"
        (tmp_path / "Alex_2").mkdir()
        assert unique_persona_dirname(tmp_path, "Alex") == "Alex_3"


# ---------------------------------------------------------------------------
# Loading (read-only)
# ---------------------------------------------------------------------------

class TestReadLanguageFile:
    def test_missing_file_defaults_to_en(self, tmp_path, caplog):
        assert read_language_file(tmp_path, "Alex") == "en"
        assert "defaulting to 'en'" in caplog.text

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / "language.txt").write_text("  de  \n")
        assert read_language_file(tmp_path, "Alex") == "de"

    def test_invalid_length_defaults_to_en(self, tmp_path):
        (tmp_path / "language.txt").write_text("eng")
        assert read_language_file(tmp_path, "Alex") == "en"


class TestFindAvatarFile:
    def test_no_files_returns_none(self, tmp_path):
        assert find_avatar_file(tmp_path, "Alex") is None

    def test_returns_the_image_file(self, tmp_path):
        (tmp_path / "image.webp").write_bytes(b"x")
        assert find_avatar_file(tmp_path, "Alex").name == "image.webp"

    def test_multiple_images_warns_and_picks_first_alphabetically(self, tmp_path, caplog):
        (tmp_path / "image.webp").write_bytes(b"x")
        (tmp_path / "image.png").write_bytes(b"y")
        assert find_avatar_file(tmp_path, "Alex").name == "image.png"
        assert "multiple image files" in caplog.text

    def test_unsupported_extension_ignored(self, tmp_path):
        (tmp_path / "image.bmp").write_bytes(b"x")
        assert find_avatar_file(tmp_path, "Alex") is None


class TestLoadPersonaFromDir:
    def test_loads_all_fields_from_files(self, tmp_path):
        d = make_persona_dir(tmp_path, "Alex", description="A friend")
        (d / "language.txt").write_text("de")
        (d / "ref.wav").write_bytes(b"wav")
        (d / "ref.txt").write_text("transcript here")
        (d / "image.png").write_bytes(b"png")

        persona = load_persona_from_dir(d)
        assert persona.name == "Alex"
        assert persona.description == "A friend"
        assert persona.system_prompt == "You are Alex, a friendly assistant."
        assert persona.reference_audio_language == "de"
        assert persona.reference_audio == str(d / "ref.wav")
        assert persona.reference_audio_transcript == str(d / "ref.txt")
        assert persona.avatar_image == str(d / "image.png")
        assert persona.persona_dir == d
        assert persona.tts_capable is True

    def test_missing_prompt_md_raises(self, tmp_path):
        d = tmp_path / "Broken"
        d.mkdir()
        with pytest.raises(PersonaStorageError, match="prompt.md"):
            load_persona_from_dir(d)

    def test_frontmatter_name_overrides_directory_name(self, tmp_path):
        d = tmp_path / "OBrien"
        d.mkdir()
        write_prompt_md(
            d, name="O'Brien", description="", router_hints="h",
            avatar_color="#fff", allow_tool_calls=False, system_prompt="p",
        )
        assert load_persona_from_dir(d).name == "O'Brien"

    def test_empty_transcript_makes_persona_not_tts_capable(self, tmp_path, caplog):
        d = make_persona_dir(tmp_path)
        (d / "ref.wav").write_bytes(b"wav")
        (d / "ref.txt").write_text("   \n")

        persona = load_persona_from_dir(d)
        assert persona.reference_audio is not None
        assert persona.reference_audio_transcript is None
        assert persona.tts_capable is False
        assert "empty" in caplog.text


class TestScanPersonasDirectory:
    def test_missing_root_returns_empty_list(self, tmp_path):
        assert scan_personas_directory(tmp_path / "nope") == []

    def test_returns_personas_in_directory_order(self, tmp_path):
        make_persona_dir(tmp_path, "Zed")
        make_persona_dir(tmp_path, "Ann")
        names = [p.name for p in scan_personas_directory(tmp_path)]
        assert names == ["Ann", "Zed"]

    def test_ignores_stray_files(self, tmp_path):
        make_persona_dir(tmp_path, "Alex")
        (tmp_path / ".DS_Store").write_bytes(b"junk")
        (tmp_path / "notes.txt").write_text("not a persona")
        names = [p.name for p in scan_personas_directory(tmp_path)]
        assert names == ["Alex"]

    def test_skips_broken_directories_with_warning(self, tmp_path, caplog):
        make_persona_dir(tmp_path, "Alex")
        (tmp_path / "Broken").mkdir()  # no prompt.md
        names = [p.name for p in scan_personas_directory(tmp_path)]
        assert names == ["Alex"]
        assert "Skipping persona directory Broken" in caplog.text


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

class TestWriteTranscriptFile:
    def test_writes_stripped_content(self, tmp_path):
        write_transcript_file(tmp_path, "  hello \n")
        assert (tmp_path / "ref.txt").read_text() == "hello"

    def test_blank_text_removes_existing_file(self, tmp_path):
        write_transcript_file(tmp_path, "hello")
        write_transcript_file(tmp_path, "   ")
        assert not (tmp_path / "ref.txt").exists()

    def test_blank_text_with_no_file_is_noop(self, tmp_path):
        write_transcript_file(tmp_path, "")
        assert not (tmp_path / "ref.txt").exists()


class TestAvatarFileOps:
    def test_write_replaces_different_extension(self, tmp_path):
        write_avatar_file(tmp_path, b"one", ".png")
        write_avatar_file(tmp_path, b"two", ".webp")
        assert not (tmp_path / "image.png").exists()
        assert (tmp_path / "image.webp").read_bytes() == b"two"

    def test_remove_returns_false_when_absent(self, tmp_path):
        assert persona_store.remove_avatar_file(tmp_path) is False

    def test_remove_returns_true_when_present(self, tmp_path):
        write_avatar_file(tmp_path, b"x", ".gif")
        assert persona_store.remove_avatar_file(tmp_path) is True
        assert not (tmp_path / "image.gif").exists()


# ---------------------------------------------------------------------------
# Legacy personas.yaml parsing
# ---------------------------------------------------------------------------

class TestLoadPersonasYaml:
    def test_parses_personas(self, tmp_path):
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p"},
        ])
        assert [p.name for p in load_personas_yaml(path)] == ["Alex"]

    def test_missing_personas_key_returns_empty_list(self, tmp_path):
        (tmp_path / "personas.yaml").write_text("{}")
        assert load_personas_yaml(tmp_path / "personas.yaml") == []

    def test_legacy_language_key_migrated(self, tmp_path):
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p", "language": "de"},
        ])
        assert load_personas_yaml(path)[0].reference_audio_language == "de"

    def test_explicit_reference_audio_language_wins_over_legacy_key(self, tmp_path):
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p",
             "language": "de", "reference_audio_language": "es"},
        ])
        assert load_personas_yaml(path)[0].reference_audio_language == "es"

    def test_top_level_not_mapping_raises(self, tmp_path):
        (tmp_path / "personas.yaml").write_text("- a\n- b\n")
        with pytest.raises(PersonaMigrationError, match="top-level mapping"):
            load_personas_yaml(tmp_path / "personas.yaml")

    def test_personas_not_a_list_raises(self, tmp_path):
        (tmp_path / "personas.yaml").write_text("personas: scalar\n")
        with pytest.raises(PersonaMigrationError, match="must be a list"):
            load_personas_yaml(tmp_path / "personas.yaml")

    def test_entry_not_a_mapping_raises(self, tmp_path):
        (tmp_path / "personas.yaml").write_text("personas:\n  - just-a-string\n")
        with pytest.raises(PersonaMigrationError, match="must be a mapping"):
            load_personas_yaml(tmp_path / "personas.yaml")

    def test_invalid_persona_raises_validation_error(self, tmp_path):
        (tmp_path / "personas.yaml").write_text("personas:\n  - name: Alex\n")
        with pytest.raises(ValidationError):
            load_personas_yaml(tmp_path / "personas.yaml")  # missing system_prompt


# ---------------------------------------------------------------------------
# One-time migration
# ---------------------------------------------------------------------------

class TestMigrateFromLegacyYaml:
    def test_success_renames_yaml_to_backup_and_creates_dirs(self, tmp_path):
        ref_wav = tmp_path / "luna.wav"
        ref_wav.write_bytes(b"wav-bytes")
        ref_txt = tmp_path / "luna.txt"
        ref_txt.write_text("a transcript")
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Luna", "system_prompt": "You are Luna.",
             "reference_audio": str(ref_wav),
             "reference_audio_transcript": str(ref_txt)},
        ])
        root = tmp_path / "Personas"
        migrate_from_legacy_yaml(path, root)

        backup = tmp_path / "personas.yaml.bak"
        assert not path.exists()
        assert backup.exists()
        assert yaml.safe_load(backup.read_text())["personas"][0]["name"] == "Luna"

        luna_dir = root / "Luna"
        assert (luna_dir / "prompt.md").exists()
        assert (luna_dir / "language.txt").read_text() == "en"
        assert (luna_dir / "ref.wav").read_bytes() == b"wav-bytes"
        assert (luna_dir / "ref.txt").read_text() == "a transcript"

    def test_missing_referenced_file_is_a_minor_error(self, tmp_path, caplog):
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Luna", "system_prompt": "p",
             "reference_audio": str(tmp_path / "gone.wav")},
        ])
        root = tmp_path / "Personas"
        migrate_from_legacy_yaml(path, root)  # must not raise

        assert (root / "Luna" / "prompt.md").exists()
        assert not (root / "Luna" / "ref.wav").exists()
        assert "could not be read" in caplog.text
        assert (tmp_path / "personas.yaml.bak").exists()

    def test_supported_image_is_copied(self, tmp_path):
        png = tmp_path / "avatar.png"
        png.write_bytes(b"png-bytes")
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p", "avatar_image": str(png)},
        ])
        migrate_from_legacy_yaml(path, tmp_path / "Personas")
        assert (tmp_path / "Personas" / "Alex" / "image.png").read_bytes() == b"png-bytes"

    def test_unsupported_image_extension_skipped_with_warning(self, tmp_path, caplog):
        bmp = tmp_path / "avatar.bmp"
        bmp.write_bytes(b"bmp")
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p", "avatar_image": str(bmp)},
        ])
        migrate_from_legacy_yaml(path, tmp_path / "Personas")
        assert "unsupported image file" in caplog.text
        assert not (tmp_path / "Personas" / "Alex" / "image.bmp").exists()

    def test_unsupported_audio_extension_skipped_with_warning(self, tmp_path, caplog):
        mp3 = tmp_path / "luna.mp3"
        mp3.write_bytes(b"mp3")
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Luna", "system_prompt": "p", "reference_audio": str(mp3)},
        ])
        migrate_from_legacy_yaml(path, tmp_path / "Personas")
        assert "unsupported audio file" in caplog.text
        assert not (tmp_path / "Personas" / "Luna" / "ref.wav").exists()

    def test_blank_transcript_not_written(self, tmp_path):
        ref_txt = tmp_path / "t.txt"
        ref_txt.write_text("   ")
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Luna", "system_prompt": "p",
             "reference_audio_transcript": str(ref_txt)},
        ])
        migrate_from_legacy_yaml(path, tmp_path / "Personas")
        assert not (tmp_path / "Personas" / "Luna" / "ref.txt").exists()

    def test_name_without_usable_directory_chars_falls_back_to_persona(self, tmp_path):
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "???", "system_prompt": "p"},
        ])
        migrate_from_legacy_yaml(path, tmp_path / "Personas")
        d = tmp_path / "Personas" / "persona"
        assert d.is_dir()
        # The frontmatter keeps the real name.
        assert load_persona_from_dir(d).name == "???"

    def test_migrated_directory_collision_gets_suffix(self, tmp_path):
        (tmp_path / "Personas" / "Alex").mkdir(parents=True)
        (tmp_path / "Personas" / "Alex" / "prompt.md").write_text("stale")
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p"},
        ])
        migrate_from_legacy_yaml(path, tmp_path / "Personas")
        assert (tmp_path / "Personas" / "Alex_2" / "prompt.md").exists()

    def test_malformed_yaml_raises_and_leaves_everything_untouched(self, tmp_path):
        path = tmp_path / "personas.yaml"
        path.write_text("personas: [\n")  # unclosed flow sequence
        root = tmp_path / "Personas"
        with pytest.raises(PersonaMigrationError):
            migrate_from_legacy_yaml(path, root)
        assert path.exists()          # the YAML is the only copy; never touched
        assert not root.exists()      # no partial directory left behind

    def test_uncreatable_root_raises_and_leaves_yaml_untouched(self, tmp_path):
        path = write_legacy_yaml(tmp_path / "personas.yaml", [
            {"name": "Alex", "system_prompt": "p"},
        ])
        blocker = tmp_path / "Personas"
        blocker.write_text("I am a file, not a directory")
        with pytest.raises(PersonaMigrationError):
            migrate_from_legacy_yaml(path, blocker)
        assert path.exists()
        assert blocker.is_file()  # rmtree cannot remove a file; left in place
