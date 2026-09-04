"""Tests for app/config.py — model validation and YAML load/save behaviour."""

import logging

import yaml

import pytest
from pydantic import ValidationError

from app.services import persona_store

from app import config as app_config
from app.config import (
    AppSettings,
    ChatRoom,
    ChatRoomsConfig,
    GeneralConfig,
    LLMSettings,
    MCPServerConfig,
    Persona,
    PersonasConfig,
    STTConfig,
    TTSConfig,
)
from tests.factories import make_chatrooms, make_personas, make_settings


# ---------------------------------------------------------------------------
# TTS / STT is_active semantics
# ---------------------------------------------------------------------------

class TestTTSConfigIsActive:
    def test_tts_config_enabled_without_base_url_is_not_active(self):
        assert TTSConfig(enabled=True).is_active is False

    def test_tts_config_disabled_with_base_url_is_not_active(self):
        assert TTSConfig(enabled=False, base_url="http://tts:1").is_active is False

    def test_tts_config_enabled_with_base_url_is_active(self):
        assert TTSConfig(enabled=True, base_url="http://tts:1").is_active is True

    def test_tts_config_blank_base_url_is_normalized_to_none(self):
        cfg = TTSConfig(enabled=True, base_url="   ")
        assert cfg.base_url is None
        assert cfg.is_active is False

    def test_tts_config_trailing_slash_is_stripped_from_base_url(self):
        cfg = TTSConfig(enabled=True, base_url="http://tts:1/")
        assert cfg.base_url == "http://tts:1"
        assert cfg.is_active is True

    def test_tts_config_whitespace_and_trailing_slashes_are_stripped(self):
        cfg = TTSConfig(enabled=True, base_url="  http://tts:1//  ")
        assert cfg.base_url == "http://tts:1"

    def test_tts_config_trailing_slash_of_path_is_stripped_but_path_kept(self):
        cfg = TTSConfig(enabled=True, base_url="http://tts:1/tts/")
        assert cfg.base_url == "http://tts:1/tts"

    def test_tts_config_slash_only_base_url_is_normalized_to_none(self):
        cfg = TTSConfig(enabled=True, base_url="///")
        assert cfg.base_url is None
        assert cfg.is_active is False


class TestSTTConfigIsActive:
    def test_stt_config_enabled_without_base_url_is_not_active(self):
        assert STTConfig(enabled=True).is_active is False

    def test_stt_config_disabled_with_base_url_is_not_active(self):
        assert STTConfig(enabled=False, base_url="http://stt:1").is_active is False

    def test_stt_config_enabled_with_base_url_is_active(self):
        assert STTConfig(enabled=True, base_url="http://stt:1").is_active is True

    def test_stt_config_blank_base_url_is_normalized_to_none(self):
        cfg = STTConfig(enabled=True, base_url="")
        assert cfg.base_url is None
        assert cfg.is_active is False

    def test_stt_config_trailing_slash_is_stripped_from_base_url(self):
        cfg = STTConfig(enabled=True, base_url="http://stt:1/")
        assert cfg.base_url == "http://stt:1"
        assert cfg.is_active is True

    def test_stt_config_whitespace_and_trailing_slashes_are_stripped(self):
        cfg = STTConfig(enabled=True, base_url="  http://stt:1//  ")
        assert cfg.base_url == "http://stt:1"

    def test_stt_config_trailing_slash_of_path_is_stripped_but_path_kept(self):
        cfg = STTConfig(enabled=True, base_url="http://stt:1/stt/")
        assert cfg.base_url == "http://stt:1/stt"

    def test_stt_config_slash_only_base_url_is_normalized_to_none(self):
        cfg = STTConfig(enabled=True, base_url="///")
        assert cfg.base_url is None
        assert cfg.is_active is False


# ---------------------------------------------------------------------------
# GeneralConfig bounds
# ---------------------------------------------------------------------------

class TestGeneralConfigBounds:
    @pytest.mark.parametrize("value", [0, 5, -1])
    def test_general_config_max_persona_replies_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            GeneralConfig(max_persona_replies=value)

    @pytest.mark.parametrize("value", [1, 4])
    def test_general_config_max_persona_replies_in_range_accepted(self, value):
        assert GeneralConfig(max_persona_replies=value).max_persona_replies == value

    @pytest.mark.parametrize("value", [0, 51])
    def test_general_config_max_turns_for_context_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            GeneralConfig(max_turns_for_context=value)


class TestGeneralConfigEnablePersonaMemories:
    """general.enable_persona_memories is a STRICT boolean (docs/
    feature_persona_memory.md): pydantic's lax coercion would silently turn
    the hand-edited string "false" into False, so the validator intercepts
    non-booleans first — warning and falling back to the default (true)."""

    def test_enable_persona_memories_defaults_to_true(self):
        assert GeneralConfig().enable_persona_memories is True

    @pytest.mark.parametrize("value", [True, False])
    def test_enable_persona_memories_valid_booleans_preserved(self, value):
        assert GeneralConfig(enable_persona_memories=value).enable_persona_memories is value

    @pytest.mark.parametrize("value", ["false", "true", "0", "1", 0, 1, None, "yes", []])
    def test_enable_persona_memories_non_bool_warns_and_falls_back_to_true(self, value, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = GeneralConfig(enable_persona_memories=value)
        assert cfg.enable_persona_memories is True
        assert "invalid general.enable_persona_memories" in caplog.text

    def test_enable_persona_memories_key_absent_keeps_default_without_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = GeneralConfig(show_tool_calls=False)
        assert cfg.enable_persona_memories is True
        assert "enable_persona_memories" not in caplog.text


# ---------------------------------------------------------------------------
# MCP server config validation
# ---------------------------------------------------------------------------

class TestMCPServerConfig:
    def test_mcp_server_config_schemeless_url_rejected(self):
        with pytest.raises(ValidationError, match="must start with http"):
            MCPServerConfig(name="broken", url="localhost:9000")

    @pytest.mark.parametrize("url", ["http://mcp:9000", "https://mcp.example.com/rpc"])
    def test_mcp_server_config_valid_schemes_accepted(self, url):
        assert MCPServerConfig(name="ok", url=url).url == url

    @pytest.mark.parametrize("timeout", [0, -1, 301])
    def test_mcp_server_config_timeout_out_of_range_rejected(self, timeout):
        with pytest.raises(ValidationError):
            MCPServerConfig(name="ok", url="http://mcp:9000", timeout=timeout)

    def test_mcp_server_config_default_timeout_is_ten_seconds(self):
        assert MCPServerConfig(name="ok", url="http://mcp:9000").timeout == 10.0


# ---------------------------------------------------------------------------
# Persona model
# ---------------------------------------------------------------------------

class TestPersona:
    def test_persona_tts_capable_requires_audio_and_transcript(self):
        assert Persona(name="A", system_prompt="p", reference_audio="a.wav").tts_capable is False
        assert (
            Persona(name="A", system_prompt="p", reference_audio="a.wav",
                    reference_audio_transcript="a.txt")
            .tts_capable
            is True
        )


class TestPersonaMemorySize:
    """Persona.memory_size bounds: 0 is a legal "memory disabled" value,
    16384 is the hard cap (docs/feature_persona_memory.md). The ge/le
    constraints are a safety net — persona_store sanitizes frontmatter and
    the router validates form input before this model is constructed."""

    def test_persona_memory_size_defaults_to_8192(self):
        assert Persona(name="A", system_prompt="p").memory_size == 8192

    @pytest.mark.parametrize("value", [0, 1, 4096, 8192, 16384])
    def test_persona_memory_size_in_range_accepted(self, value):
        assert Persona(name="A", system_prompt="p", memory_size=value).memory_size == value

    @pytest.mark.parametrize("value", [-1, 16385])
    def test_persona_memory_size_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            Persona(name="A", system_prompt="p", memory_size=value)


# ---------------------------------------------------------------------------
# Personas directory resolution
# ---------------------------------------------------------------------------

class TestGetPersonasDirectory:
    def test_defaults_to_project_root_personas(self, tmp_project_root):
        # The autouse settings cache has no personas_directory configured.
        assert app_config.get_personas_directory() == tmp_project_root / "Personas"

    def test_relative_path_resolves_against_project_root(self, tmp_project_root, monkeypatch):
        monkeypatch.setattr(
            app_config, "_settings_cache",
            make_settings(general=GeneralConfig(personas_directory="custom/personas")),
        )
        assert app_config.get_personas_directory() == tmp_project_root / "custom/personas"

    def test_absolute_path_used_verbatim(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            app_config, "_settings_cache",
            make_settings(general=GeneralConfig(personas_directory=str(tmp_path / "elsewhere"))),
        )
        assert app_config.get_personas_directory() == tmp_path / "elsewhere"


# ---------------------------------------------------------------------------
# load_personas() startup decision matrix
# ---------------------------------------------------------------------------

class TestLoadPersonasDecisionMatrix:
    """personas.yaml vs the Personas directory: which one wins, when?"""

    def test_legacy_yaml_only_is_migrated_once(self, tmp_project_root):
        (tmp_project_root / "personas.yaml").write_text(
            "personas:\n  - name: Fresh\n    system_prompt: p\n"
        )
        cfg = app_config.load_personas()
        assert [p.name for p in cfg.personas] == ["Fresh"]
        # The YAML is renamed (never deleted) so the migration runs once.
        assert not (tmp_project_root / "personas.yaml").exists()
        assert (tmp_project_root / "personas.yaml.bak").exists()
        assert (tmp_project_root / "Personas" / "Fresh" / "prompt.md").exists()

    def test_directory_and_yaml_prefers_directory_and_warns(self, tmp_project_root, caplog):
        (tmp_project_root / "personas.yaml").write_text(
            "personas:\n  - name: Stale\n    system_prompt: p\n"
        )
        persona_dir = tmp_project_root / "Personas" / "Fresh"
        persona_dir.mkdir(parents=True)
        persona_dir.joinpath("prompt.md").write_text(
            "---\ndescription: fresh\nrouter_hints: h\navatar_color: '#888888'\n"
            "allow_tool_calls: false\n---\n\nYou are Fresh.\n"
        )
        with caplog.at_level(logging.WARNING):
            cfg = app_config.load_personas()
        assert [p.name for p in cfg.personas] == ["Fresh"]
        # The YAML is IGNORED, not renamed or deleted.
        assert (tmp_project_root / "personas.yaml").exists()
        assert not (tmp_project_root / "personas.yaml.bak").exists()
        assert "IGNORED" in caplog.text

    def test_malformed_legacy_yaml_aborts_startup(self, tmp_project_root):
        (tmp_project_root / "personas.yaml").write_text("personas: [\n")
        with pytest.raises(persona_store.PersonaMigrationError):
            app_config.load_personas()
        # The YAML survives for the next attempt.
        assert (tmp_project_root / "personas.yaml").exists()
        # And no partial directory is left behind.
        assert not (tmp_project_root / "Personas").exists()


# ---------------------------------------------------------------------------
# Loading: missing files fall back to defaults
# ---------------------------------------------------------------------------

class TestLoadingFallbacks:
    def test_load_settings_missing_file_returns_defaults(self, tmp_path):
        settings = app_config.load_settings(tmp_path / "nope.yaml")
        assert settings.llm.base_url == "http://localhost:8080"
        assert settings.mcp.servers == []

    def test_load_personas_no_yaml_no_directory_creates_empty_dir(self, tmp_project_root):
        # Neither personas.yaml nor a Personas directory exists: the app must
        # not crash — it logs an error, creates the directory, and starts empty.
        cfg = app_config.load_personas()
        assert cfg.personas == []
        assert (tmp_project_root / "Personas").is_dir()

    def test_load_chatrooms_missing_file_returns_empty(self, tmp_path):
        cfg = app_config.load_chatrooms(tmp_path / "nope.yaml")
        assert cfg.chat_rooms == []

    def test_load_settings_empty_file_returns_defaults(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        assert app_config.load_settings(path) == AppSettings()


# ---------------------------------------------------------------------------
# Loading: content and migration
# ---------------------------------------------------------------------------

class TestLoadingContent:
    def test_load_settings_parses_all_sections(self, tmp_path):
        path = tmp_path / "settings.yaml"
        path.write_text(
            """
llm:
  base_url: http://custom:1234
  model: custom-model
tts:
  enabled: true
  base_url: http://tts:1
general:
  show_tool_calls: false
mcp:
  servers:
    - name: my-server
      url: http://mcp:9000
"""
        )
        settings = app_config.load_settings(path)
        assert settings.llm.base_url == "http://custom:1234"
        assert settings.tts.is_active is True
        assert settings.general.show_tool_calls is False
        assert settings.mcp.servers[0].name == "my-server"

    def test_load_chatrooms_parses_rooms(self, tmp_path):
        path = tmp_path / "chatrooms.yaml"
        path.write_text(
            """
chat_rooms:
  - name: TNG
    persona_names: [Alex]
    echo_chamber: true
"""
        )
        cfg = app_config.load_chatrooms(path)
        assert cfg.chat_rooms[0] == ChatRoom(name="TNG", persona_names=["Alex"], echo_chamber=True)


# ---------------------------------------------------------------------------
# Save/load round-trips
# ---------------------------------------------------------------------------

class TestSaveLoadRoundTrip:
    def test_save_settings_round_trip(self, tmp_path):
        path = tmp_path / "settings.yaml"
        settings = make_settings(
            general=GeneralConfig(max_persona_replies=3, show_tool_calls=False),
        )
        app_config.save_settings(settings, path)
        assert path.exists()
        reloaded = yaml.safe_load(path.read_text())
        assert reloaded["general"]["max_persona_replies"] == 3
        assert reloaded["general"]["show_tool_calls"] is False

    def test_save_chatrooms_round_trip(self, tmp_path):
        path = tmp_path / "chatrooms.yaml"
        cfg = make_chatrooms()
        app_config.save_chatrooms(cfg, path)
        reloaded = app_config.load_chatrooms(path)
        assert [r.name for r in reloaded.chat_rooms] == ["TNG"]


# ---------------------------------------------------------------------------
# Cache semantics
# ---------------------------------------------------------------------------

class TestCaching:
    def test_get_settings_returns_cache_without_reloading(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.yaml"
        settings = make_settings()
        monkeypatch.setattr(app_config, "_settings_cache", settings)
        # Mutate the file after caching: get_settings must not see it.
        path.write_text("llm:\n  base_url: http://changed:1\n")
        assert app_config.get_settings() is settings

    def test_get_personas_returns_cache_without_reloading(self, tmp_path, monkeypatch):
        cfg = make_personas()
        monkeypatch.setattr(app_config, "_personas_cache", cfg)
        assert app_config.get_personas() is cfg

    def test_reload_all_replaces_caches(self, tmp_path, monkeypatch):
        # Point the module's project root at tmp so reload_all reads tmp files.
        monkeypatch.setattr(app_config, "_PROJECT_ROOT", tmp_path)
        (tmp_path / "settings.yaml").write_text("llm:\n  base_url: http://reloaded:1\n")
        (tmp_path / "personas.yaml").write_text(
            "personas:\n  - name: Fresh\n    system_prompt: p\n"
        )
        (tmp_path / "chatrooms.yaml").write_text(
            "chat_rooms:\n  - name: NewRoom\n"
        )
        app_config.reload_all()
        assert app_config.get_settings().llm.base_url == "http://reloaded:1"
        assert [p.name for p in app_config.get_personas().personas] == ["Fresh"]
        assert [r.name for r in app_config.get_chatrooms().chat_rooms] == ["NewRoom"]
