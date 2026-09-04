"""API tests for app/routers/settings.py — GET/PUT settings.

The PUT contract matters: the `general:` section is a *partial update*.
The Servers dialog sends no `general` section at all, and the router must
preserve every current general value (this is the show_tool_calls
regression — the router must never rebuild GeneralConfig from request
defaults).

TTS generification (M2): the `tts:` section is a full replacement whose
hard-coded parameter fields are replaced by a generic `parameters` map.
Save-time validation (plan T7) 422s garbage parameters — but only when a
capabilities document is cached for the exact base_url being saved, so an
engine switch is never bricked by a stale doc.
"""

from app.config import MCPConfig, MCPServerConfig
from tests.factories import make_capabilities_doc, make_mcp_server, make_settings

import app.config as app_config
import app.services.tts_client as tts_client

TTS_BASE = "http://tts.local:5500"


def base_update(**overrides) -> dict:
    """A full settings payload, as the frontend settings dialog sends it."""
    payload = {
        "llm": {"base_url": "http://llm.local:8080", "model": "test-model",
                "max_tokens": 1024, "temperature": 0.8},
        "tts": {"enabled": True, "base_url": TTS_BASE,
                "timeout": 60.0, "streaming": False},
        "stt": {"enabled": True, "base_url": "http://stt.local:6600",
                "timeout": 30.0},
        "general": {},
    }
    payload.update(overrides)
    return payload


def tts_update(parameters=None, **overrides) -> dict:
    """A tts: section for PUTs, with an optional parameters map."""
    section = {"enabled": True, "base_url": TTS_BASE,
               "timeout": 60.0, "streaming": False}
    if parameters is not None:
        section["parameters"] = parameters
    section.update(overrides)
    return section


def seed_capabilities_cache(monkeypatch, base_url, doc):
    """Populate the tts_client capabilities slot the way a successful
    get_capabilities() fetch would (no network in these tests)."""
    monkeypatch.setattr(tts_client, "_capabilities_base_url", base_url)
    monkeypatch.setattr(tts_client, "_capabilities_cache", doc)


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
        assert body["tts"]["parameters"] == {}
        # The response carries the generic map — the old static fields are
        # gone from the contract.
        for legacy_key in ("num_steps", "guidance_scale", "seed"):
            assert legacy_key not in body["tts"]
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
            tts=tts_update(base_url="   "),
        ))
        assert resp.status_code == 200
        assert resp.json()["tts"]["base_url"] is None

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


class TestUpdateTTSParameters:
    """M2: the generic tts.parameters map round-trips through PUT/GET, and
    T7 validation 422s garbage only when a capabilities doc is cached for
    the exact base_url being saved. The doc is the real dots.tts snapshot."""

    # -- round-trip -----------------------------------------------------------

    def test_parameters_are_saved_and_returned(self, client):
        parameters = {"num_steps": 16, "guidance_scale": 1.5, "seed": 1337}

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters=parameters)))

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == parameters
        # And it persisted to settings.yaml (redirected to tmp by the fixture).
        assert client.get("/api/settings").json()["tts"]["parameters"] == parameters

    def test_empty_parameters_saved_as_empty_object(self, client):
        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={})))

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == {}

    def test_omitted_parameters_default_to_empty_object(self, client):
        # Full-replacement semantics: no parameters sent -> none stored.
        resp = client.put("/api/settings", json=base_update())

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == {}

    def test_old_shape_put_degrades_without_error(self, client):
        # A pre-generification client still sends the three static fields.
        # Pydantic drops them: the save succeeds with an empty parameters
        # map (engine defaults) instead of erroring.
        resp = client.put("/api/settings", json=base_update(
            tts={"enabled": True, "base_url": TTS_BASE,
                 "num_steps": 10, "guidance_scale": 1.5, "seed": 0,
                 "timeout": 60.0, "streaming": False}))

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == {}

    # -- T7: 422 on garbage, judged by the cached doc -------------------------

    def test_unknown_parameter_rejected_422_and_named(self, client, monkeypatch):
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1})))

        assert resp.status_code == 422
        assert "bogus_knob" in resp.json()["detail"]

    def test_integer_above_max_rejected_422(self, client, monkeypatch):
        # dots.tts num_steps: min 1, max 64.
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"num_steps": 100})))

        assert resp.status_code == 422
        assert "num_steps" in resp.json()["detail"]

    def test_number_below_min_rejected_422(self, client, monkeypatch):
        # dots.tts guidance_scale: min 0.
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"guidance_scale": -1.0})))

        assert resp.status_code == 422
        assert "guidance_scale" in resp.json()["detail"]

    def test_wrong_type_rejected_422(self, client, monkeypatch):
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"num_steps": "ten"})))

        assert resp.status_code == 422
        assert "num_steps" in resp.json()["detail"]

    def test_non_enum_string_rejected_422(self, client, monkeypatch):
        # dots.tts ode_method: enum [euler, midpoint, rk4].
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"ode_method": "runge-kutta"})))

        assert resp.status_code == 422
        assert "ode_method" in resp.json()["detail"]

    def test_valid_parameters_pass_validation_and_persist(self, client, monkeypatch):
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))
        parameters = {"num_steps": 16, "ode_method": "rk4", "seed": 7}

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters=parameters)))

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == parameters

    # -- T7: the judge must be the right judge ---------------------------------

    def test_validation_skipped_when_cached_doc_is_for_another_engine(self, client, monkeypatch):
        # The cache holds a doc for the OLD server; the save targets a new
        # one. The stale doc cannot judge the new parameters — and a 422
        # here would brick the switch (T4 makes it safe: unadvertised
        # fields are never sent once the new doc is fetched).
        seed_capabilities_cache(monkeypatch, "http://stale.local:9999",
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1})))

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == {"bogus_knob": 1}

    def test_validation_skipped_on_negative_cache(self, client, monkeypatch):
        # A cached fetch FAILURE (doc is None) means we simply don't know:
        # the save goes through, the server's own 422 is the backstop.
        seed_capabilities_cache(monkeypatch, TTS_BASE, None)

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1})))

        assert resp.status_code == 200

    def test_validation_skipped_on_empty_cache(self, client):
        # No fetch has happened yet (the fixture leaves the slot empty):
        # the save path stays synchronous and offline-safe.
        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1})))

        assert resp.status_code == 200

    def test_validation_skipped_when_saved_base_url_is_blank(self, client, monkeypatch):
        # No server to validate against (TTS disabled/blank) -> nothing to
        # judge; the (meaningless) parameters are stored as sent.
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1}, base_url="   ")))

        assert resp.status_code == 200
        assert resp.json()["tts"]["parameters"] == {"bogus_knob": 1}

    def test_validation_runs_when_base_url_differs_only_by_trailing_slash(self, client, monkeypatch):
        # The cache key is the NORMALIZED base_url (strip + rstrip("/"), per
        # the config model). The request's raw URL must be normalized the
        # same way before the T7 comparison, or a same-server save spelled
        # with a trailing slash would be judged "a different engine" and
        # silently skip validation.
        seed_capabilities_cache(monkeypatch, TTS_BASE,
                                make_capabilities_doc(engine="dots.tts"))

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1},
                           base_url=TTS_BASE + "/")))

        assert resp.status_code == 422
        assert "bogus_knob" in resp.json()["detail"]

    # -- cache invalidation on save --------------------------------------------

    def test_successful_save_invalidates_capabilities_cache(self, client, monkeypatch):
        doc = make_capabilities_doc(engine="dots.tts")
        seed_capabilities_cache(monkeypatch, TTS_BASE, doc)

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"num_steps": 16})))

        assert resp.status_code == 200
        # Slot cleared (no inline refetch — that would be network in a sync
        # endpoint); the next get_capabilities() call refetches.
        assert tts_client.cached_capabilities() == (None, None)

    def test_failed_validation_does_not_invalidate_cache(self, client, monkeypatch):
        doc = make_capabilities_doc(engine="dots.tts")
        seed_capabilities_cache(monkeypatch, TTS_BASE, doc)

        resp = client.put("/api/settings", json=base_update(
            tts=tts_update(parameters={"bogus_knob": 1})))

        assert resp.status_code == 422
        # The doc still describes the live server: keep it.
        assert tts_client.cached_capabilities() == (TTS_BASE, doc)
