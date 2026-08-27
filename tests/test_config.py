"""Tests for app/config.py — model validation and YAML load/save behaviour."""

import yaml

import pytest
from pydantic import ValidationError

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


# ---------------------------------------------------------------------------
# Loading: missing files fall back to defaults
# ---------------------------------------------------------------------------

class TestLoadingFallbacks:
    def test_load_settings_missing_file_returns_defaults(self, tmp_path):
        settings = app_config.load_settings(tmp_path / "nope.yaml")
        assert settings.llm.base_url == "http://localhost:8080"
        assert settings.mcp.servers == []

    def test_load_personas_missing_file_returns_empty(self, tmp_path):
        cfg = app_config.load_personas(tmp_path / "nope.yaml")
        assert cfg.personas == []

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

    def test_load_personas_migrates_legacy_language_key(self, tmp_path):
        path = tmp_path / "personas.yaml"
        path.write_text(
            """
personas:
  - name: Alex
    system_prompt: You are Alex.
    language: de
"""
        )
        cfg = app_config.load_personas(path)
        assert cfg.personas[0].reference_audio_language == "de"

    def test_load_personas_keeps_explicit_reference_audio_language(self, tmp_path):
        # If both keys exist, the new key wins and the legacy one is ignored.
        path = tmp_path / "personas.yaml"
        path.write_text(
            """
personas:
  - name: Alex
    system_prompt: You are Alex.
    language: de
    reference_audio_language: es
"""
        )
        cfg = app_config.load_personas(path)
        assert cfg.personas[0].reference_audio_language == "es"

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

    def test_save_personas_round_trip(self, tmp_path):
        path = tmp_path / "personas.yaml"
        cfg = make_personas()
        app_config.save_personas(cfg, path)
        reloaded = app_config.load_personas(path)
        assert [p.name for p in reloaded.personas] == ["Alex", "Luna"]
        assert reloaded.personas[1].reference_audio == "reference/luna.wav"

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
