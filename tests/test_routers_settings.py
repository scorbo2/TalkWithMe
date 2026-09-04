"""API tests for app/routers/settings.py — GET/PUT settings.

The PUT contract matters: the `general:` section is a *partial update*.
The Servers dialog sends no `general` section at all, and the router must
preserve every current general value (this is the show_tool_calls
regression — the router must never rebuild GeneralConfig from request
defaults).
"""

from app.config import MCPConfig, MCPServerConfig
from tests.factories import make_mcp_server, make_settings

import app.config as app_config


def base_update(**overrides) -> dict:
    """A full settings payload, as the frontend settings dialog sends it."""
    payload = {
        "llm": {"base_url": "http://llm.local:8080", "model": "test-model",
                "max_tokens": 1024, "temperature": 0.8},
        "tts": {"enabled": True, "base_url": "http://tts.local:5500",
                "num_steps": 10, "guidance_scale": 1.5, "seed": 0,
                "timeout": 60.0, "streaming": False},
        "stt": {"enabled": True, "base_url": "http://stt.local:6600",
                "timeout": 30.0},
        "general": {},
    }
    payload.update(overrides)
    return payload


class TestGetSettings:
    def test_returns_current_settings(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm"]["base_url"] == "http://llm.local:8080"
        assert body["llm"]["model"] == "test-model"
        # Fixture default: TTS/STT inactive
        assert body["tts"]["enabled"] is False
        assert body["tts"]["base_url"] is None
        assert body["general"] == {
            "persona_name_mentions": True,
            "max_persona_replies": 1,
            "max_turns_for_context": 6,
            "show_tool_calls": True,
            "enable_persona_memories": True,
        }


class TestUpdateSettings:
    def test_full_update_applies_every_section(self, client):
        resp = client.put("/api/settings", json=base_update())
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm"]["model"] == "test-model"
        assert body["tts"]["base_url"] == "http://tts.local:5500"
        assert body["stt"]["timeout"] == 30.0

        # And it persisted to settings.yaml (redirected to tmp by the fixture).
        assert client.get("/api/settings").json()["tts"]["base_url"] == "http://tts.local:5500"

    def test_blank_base_urls_normalized_to_none(self, client):
        resp = client.put("/api/settings", json=base_update(
            tts={"enabled": True, "base_url": "   ", "num_steps": 10,
                 "guidance_scale": 1.5, "seed": 0, "timeout": 60.0, "streaming": False},
        ))
        assert resp.status_code == 200
        assert resp.json()["tts"]["base_url"] is None

    def test_seed_zero_normalized_to_none(self, client):
        resp = client.put("/api/settings", json=base_update(
            tts={"enabled": True, "base_url": "http://tts.local:5500", "num_steps": 10,
                 "guidance_scale": 1.5, "seed": 0, "timeout": 60.0, "streaming": False},
        ))
        assert resp.json()["tts"]["seed"] is None

    def test_nonzero_seed_preserved(self, client):
        resp = client.put("/api/settings", json=base_update(
            tts={"enabled": True, "base_url": "http://tts.local:5500", "num_steps": 10,
                 "guidance_scale": 1.5, "seed": 1337, "timeout": 60.0, "streaming": False},
        ))
        assert resp.json()["tts"]["seed"] == 1337

    # -- partial-update semantics for the general section -------------------

    def test_partial_general_update_preserves_omitted_fields(self, client, monkeypatch):
        """Send ONLY show_tool_calls; the other general fields must keep
        their current (non-default) values."""
        current = make_settings()
        current.general.persona_name_mentions = False
        current.general.max_persona_replies = 3
        current.general.max_turns_for_context = 12
        current.general.show_tool_calls = True
        current.general.enable_persona_memories = False  # non-default: preserved?
        monkeypatch.setattr(app_config, "_settings_cache", current)

        resp = client.put("/api/settings", json=base_update(
            general={"show_tool_calls": False}))

        assert resp.status_code == 200
        assert resp.json()["general"] == {
            "persona_name_mentions": False,   # preserved
            "max_persona_replies": 3,         # preserved
            "max_turns_for_context": 12,      # preserved
            "show_tool_calls": False,         # updated
            "enable_persona_memories": False, # preserved
        }

    def test_missing_general_section_preserves_everything(self, client, monkeypatch):
        """The Servers dialog sends NO general section. None of the general
        values may reset to defaults (the show_tool_calls regression)."""
        current = make_settings()
        current.general.persona_name_mentions = False
        current.general.max_persona_replies = 4
        current.general.max_turns_for_context = 9
        current.general.show_tool_calls = False
        current.general.enable_persona_memories = False  # must not reset to True
        monkeypatch.setattr(app_config, "_settings_cache", current)

        payload = base_update()
        del payload["general"]

        resp = client.put("/api/settings", json=payload)

        assert resp.status_code == 200
        assert resp.json()["general"] == {
            "persona_name_mentions": False,
            "max_persona_replies": 4,
            "max_turns_for_context": 9,
            "show_tool_calls": False,
            "enable_persona_memories": False,
        }

    def test_enable_persona_memories_round_trip(self, client):
        """The General settings dialog sends the whole general section:
        turning the feature off must stick across a re-read."""
        resp = client.put("/api/settings", json=base_update(
            general={"enable_persona_memories": False}))
        assert resp.status_code == 200
        assert resp.json()["general"]["enable_persona_memories"] is False

        # And it persisted to settings.yaml (redirected to tmp by the fixture).
        assert client.get("/api/settings").json()["general"]["enable_persona_memories"] is False

    def test_mcp_section_carried_over_from_current_config(self, client, monkeypatch):
        """The mcp: section is yaml-only. A UI save must not wipe it."""
        current = make_settings()
        current.mcp = MCPConfig(servers=[make_mcp_server("keep-me", "http://mcp.local:9000")],
                                max_tool_iterations=5)
        monkeypatch.setattr(app_config, "_settings_cache", current)

        resp = client.put("/api/settings", json=base_update())
        assert resp.status_code == 200

        saved = app_config.get_settings()
        assert [s.name for s in saved.mcp.servers] == ["keep-me"]
        assert saved.mcp.max_tool_iterations == 5

    def test_general_update_rejects_out_of_bounds_values(self, client):
        resp = client.put("/api/settings", json=base_update(
            general={"max_persona_replies": 99}))
        assert resp.status_code == 422

    def test_llm_temperature_out_of_bounds_rejected(self, client):
        resp = client.put("/api/settings", json=base_update(
            llm={"base_url": "http://llm.local:8080", "model": "m",
                 "max_tokens": 1024, "temperature": 1.5}))
        assert resp.status_code == 422
