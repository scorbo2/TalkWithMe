"""Tests for app/services/tts_client.py and app/services/stt_client.py.

The httpx client is replaced with FakeAsyncClient; no network involved.
Both features are INACTIVE by default in the fixture config — tests that
need an active feature re-point the settings cache.
"""

import asyncio
import logging

import httpx
import pytest

import app.config as app_config
import app.services.stt_client as stt_client
import app.services.tts_client as tts_client
from tests.factories import (
    FakeAsyncClient,
    json_response,
    make_capabilities_doc,
    make_settings,
    make_unexpected_field_422,
)
from app.config import STTConfig, TTSConfig


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_http(monkeypatch, responder):
    monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                        lambda *a, **kw: FakeAsyncClient(responder))
    monkeypatch.setattr(stt_client.httpx, "AsyncClient",
                        lambda *a, **kw: FakeAsyncClient(responder))


def _active_tts(monkeypatch, **tts_kwargs):
    tts = TTSConfig(enabled=True, base_url="http://tts.local:5500", **tts_kwargs)
    monkeypatch.setattr(app_config, "_settings_cache", make_settings(tts=tts))
    return tts


def _active_stt(monkeypatch, **stt_kwargs):
    stt = STTConfig(enabled=True, base_url="http://stt.local:6600", **stt_kwargs)
    monkeypatch.setattr(app_config, "_settings_cache", make_settings(stt=stt))
    return stt


# ---------------------------------------------------------------------------
# TTS health
# ---------------------------------------------------------------------------

class TestCheckTTSHealth:
    def test_inactive_tts_reports_unreachable_without_calling_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        assert _run(tts_client.check_tts_health()) == (False, None)

    def test_200_with_server_type(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch,
                    lambda method, url, **kw: json_response(
                        200, {"status": "ok", "serverType": "f5-tts"}))

        reachable, server_type = _run(tts_client.check_tts_health())

        assert (reachable, server_type) == (True, "f5-tts")

    def test_404_means_server_up_but_no_health_endpoint(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(404, {"detail": "nope"}))

        assert _run(tts_client.check_tts_health()) == (True, None)

    def test_200_without_server_type_field(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(200, {"status": "ok"}))

        assert _run(tts_client.check_tts_health()) == (True, None)

    def test_200_with_non_json_body_is_still_up(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch,
                    lambda method, url, **kw: httpx.Response(
                        200, content=b"<html>hi</html>"))

        assert _run(tts_client.check_tts_health()) == (True, None)

    def test_500_means_down(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(500, {}))

        assert _run(tts_client.check_tts_health()) == (False, None)

    def test_connection_error_means_down(self, monkeypatch):
        _active_tts(monkeypatch)

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(tts_client.check_tts_health()) == (False, None)


# ---------------------------------------------------------------------------
# TTS synthesis (plan T4: doc-driven payload, T6: language fit,
# plan T3: 422 self-heal)
# ---------------------------------------------------------------------------

def _cache_capabilities(monkeypatch, doc, base_url="http://tts.local:5500"):
    """Pre-seed the capabilities cache slot: the next get_capabilities()
    serves `doc` without touching the network."""
    monkeypatch.setattr(tts_client, "_capabilities_base_url", base_url)
    monkeypatch.setattr(tts_client, "_capabilities_cache", doc)


class TestSynthesize:
    def test_inactive_tts_returns_none_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        result = _run(tts_client.synthesize("hi", "p", "QUJD"))
        assert result is None

    def test_synthesize_posts_doc_driven_payload_and_returns_body(self, monkeypatch):
        # The payload is built from the capabilities doc (plan T4): the
        # dots.tts snapshot advertises text, audio_base64, reference_text,
        # language, seed, and the engine knobs configured below — all of
        # them, and nothing else, reaches the wire.
        _active_tts(monkeypatch, parameters={"num_steps": 7, "guidance_scale": 2.5, "seed": 42})
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        seen = {}

        def responder(method, url, **kw):
            seen["url"] = url
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hello", "prompt text", "QUJD", language="de"))

        assert result == {"audio_base64": "QUJD", "sample_rate": 24000}
        assert seen["url"] == "http://tts.local:5500/synthesize"
        assert seen["payload"] == {
            "text": "hello",
            "reference_text": "prompt text",
            "audio_base64": "QUJD",
            "language": "de",
            "num_steps": 7,
            "guidance_scale": 2.5,
            "seed": 42,
        }

    def test_synthesize_chatterbox_never_receives_reference_text(self, monkeypatch):
        # The chatterbox snapshot advertises no reference_text parameter,
        # so the persona's transcript must not be sent: it would 422 the
        # whole synthesis under extra="forbid".
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="chatterbox"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        _patch_http(monkeypatch, responder)
        _run(tts_client.synthesize("hi", "the transcript", "QUJD", language="en"))

        assert seen["payload"] == {
            "text": "hi",
            "audio_base64": "QUJD",
            "language": "en",
        }

    def test_synthesize_non_cloning_engine_returns_none_without_posting(self, monkeypatch, caplog):
        # A doc with reference_audio: null describes a non-cloning engine:
        # a text-only payload would be ACCEPTED, in the engine's default
        # voice — the wrong voice for the persona. Fail loudly before
        # spending a /synthesize request, and log the reason. (This is the
        # backstop for the first call after a cache invalidation, when the
        # router's warm-cache 503 cannot fire yet.)
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="omnivoice", reference_audio=None))
        calls = []

        def responder(method, url, **kw):
            calls.append((method, url))
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        _patch_http(monkeypatch, responder)
        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            assert _run(tts_client.synthesize("hi", "p", "QUJD")) is None

        assert calls == []
        assert "cloning" in caplog.text

    def test_synthesize_language_in_enum_is_sent_verbatim(self, monkeypatch):
        # qwen3-tts's language parameter carries an enum that includes
        # "zh": a persona code in the enum goes through unchanged (the
        # server does any mapping — the app never does, plan T6).
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="qwen3-tts"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD"})

        _patch_http(monkeypatch, responder)
        _run(tts_client.synthesize("hi", "p", "QUJD", language="zh"))

        assert seen["payload"]["language"] == "zh"

    def test_synthesize_language_not_in_enum_is_omitted_and_warned(self, monkeypatch, caplog):
        # chatterbox's language enum has no "xx": the code is dropped
        # rather than sent (it would 422) or mapped (forbidden by the
        # code-only contract), and the omission is logged.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="chatterbox"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD"})

        _patch_http(monkeypatch, responder)
        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            _run(tts_client.synthesize("hi", "p", "QUJD", language="xx"))

        assert "language" not in seen["payload"]
        assert "xx" in caplog.text

    def test_synthesize_unadvertised_parameter_is_dropped(self, monkeypatch):
        # 'exaggeration' is a chatterbox knob; the omnivoice engine does
        # not advertise it, so it must not be sent (extra="forbid" would
        # 422 it). 'num_steps' is advertised and goes through.
        _active_tts(monkeypatch, parameters={"exaggeration": 0.9, "num_steps": 16})
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="omnivoice"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD"})

        _patch_http(monkeypatch, responder)
        _run(tts_client.synthesize("hi", "p", "QUJD"))

        assert seen["payload"] == {
            "text": "hi",
            "reference_text": "p",
            "audio_base64": "QUJD",
            "language": "en",
            "num_steps": 16,
        }

    def test_synthesize_none_and_empty_parameter_values_are_omitted(self, monkeypatch):
        # "Not set" is an absent key or an empty string (the UI sends "");
        # neither reaches the wire — the engine applies its own default.
        _active_tts(monkeypatch, parameters={"seed": None, "ode_method": ""})
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD"})

        _patch_http(monkeypatch, responder)
        _run(tts_client.synthesize("hi", "p", "QUJD"))

        assert "seed" not in seen["payload"]
        assert "ode_method" not in seen["payload"]

    def test_synthesize_app_managed_names_never_sourced_from_parameters(self, monkeypatch):
        # A stale hand-edited tts.parameters must not be able to override
        # the persona's text, voice, transcript, or language (plan T4):
        # those four fields come from the request and the persona only.
        _active_tts(monkeypatch, parameters={
            "text": "hijacked",
            "audio_base64": "AAAA",
            "reference_text": "fake transcript",
            "language": "zz",
            "num_steps": 7,
        })
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD"})

        _patch_http(monkeypatch, responder)
        _run(tts_client.synthesize("hello", "real transcript", "QUJD", language="de"))

        assert seen["payload"] == {
            "text": "hello",
            "reference_text": "real transcript",
            "audio_base64": "QUJD",
            "language": "de",
            "num_steps": 7,
        }

    def test_synthesize_no_doc_fallback_sends_unfiltered_with_one_warning(self, monkeypatch, caplog):
        # No capabilities doc (server without /capabilities): the core
        # vocabulary plus ALL configured parameters are sent unfiltered,
        # and the fallback is warned ONCE per base_url — streaming TTS
        # issues one /synthesize per sentence and must not spam the log.
        _active_tts(monkeypatch, parameters={"num_steps": 7, "guidance_scale": 2.5})
        seen = {}
        calls = []

        def responder(method, url, **kw):
            calls.append((method, url))
            if url.endswith("/capabilities"):
                return json_response(404, {"detail": "no such endpoint"})
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        _patch_http(monkeypatch, responder)
        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            assert _run(tts_client.synthesize("hi", "p", "QUJD")) is not None
            assert _run(tts_client.synthesize("hi", "p", "QUJD")) is not None

        assert seen["payload"] == {
            "text": "hi",
            "reference_text": "p",
            "audio_base64": "QUJD",
            "language": "en",
            "num_steps": 7,
            "guidance_scale": 2.5,
        }
        # One capabilities GET (the 404 is negatively cached) and exactly
        # one fallback warning across two syntheses:
        assert calls == [
            ("GET", "http://tts.local:5500/capabilities"),
            ("POST", "http://tts.local:5500/synthesize"),
            ("POST", "http://tts.local:5500/synthesize"),
        ]
        assert caplog.text.count("TTS /capabilities unavailable") == 1

    def test_synthesize_no_doc_fallback_still_protects_app_managed_names(self, monkeypatch):
        # Even without a doc the four app-managed fields are never sourced
        # from tts.parameters: the "all configured parameters" fallback
        # covers engine knobs only.
        _active_tts(monkeypatch, parameters={"language": "zz", "reference_text": "fake"})
        seen = {}

        def responder(method, url, **kw):
            if url.endswith("/capabilities"):
                return json_response(404, {"detail": "no such endpoint"})
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD"})

        _patch_http(monkeypatch, responder)
        _run(tts_client.synthesize("hi", "real", "QUJD", language="de"))

        assert seen["payload"] == {
            "text": "hi",
            "reference_text": "real",
            "audio_base64": "QUJD",
            "language": "de",
        }

    def test_synthesize_422_self_heal_refetches_and_retries_once(self, monkeypatch, caplog):
        # Stale doc: the cache still holds the dots.tts doc, but the
        # server behind base_url is now OmniVoice (which does not
        # advertise speaker_scale). The first payload — built from the
        # stale doc — 422s naming the field; the self-heal invalidates,
        # refetches, rebuilds, and retries exactly once, and the retry
        # succeeds. The 422 detail is logged (it names the field).
        _active_tts(monkeypatch, parameters={"speaker_scale": 1.5})
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        posts = []

        def responder(method, url, **kw):
            if url.endswith("/capabilities"):
                return json_response(200, make_capabilities_doc(engine="omnivoice"))
            posts.append(kw.get("json"))
            if len(posts) == 1:
                return json_response(422, make_unexpected_field_422("speaker_scale", 1.5))
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        _patch_http(monkeypatch, responder)
        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            result = _run(tts_client.synthesize("hi", "p", "QUJD"))

        assert result == {"audio_base64": "QUJD", "sample_rate": 24000}
        assert len(posts) == 2
        assert posts[0]["speaker_scale"] == 1.5  # built from the stale doc
        assert "speaker_scale" not in posts[1]  # rebuilt from the fresh doc
        assert "speaker_scale" in caplog.text   # 422 detail was logged

    def test_synthesize_422_retries_exactly_once_then_gives_up(self, monkeypatch, caplog):
        # A 422 that survives the refetch+retry (genuinely bad input)
        # must not loop: one retry, then None — and both 422 details
        # are logged.
        _active_tts(monkeypatch, parameters={"num_steps": 999})
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="omnivoice"))
        posts = []

        def responder(method, url, **kw):
            if url.endswith("/capabilities"):
                return json_response(200, make_capabilities_doc(engine="omnivoice"))
            posts.append(kw.get("json"))
            return json_response(422, make_unexpected_field_422("num_steps", 999))

        _patch_http(monkeypatch, responder)
        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            assert _run(tts_client.synthesize("hi", "p", "QUJD")) is None

        assert len(posts) == 2
        assert caplog.text.count("rejected the payload (422)") == 2

    def test_synthesize_non_422_error_does_not_self_heal(self, monkeypatch):
        # Non-422 errors say nothing about a stale document: one POST, no
        # refetch, no retry.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="omnivoice"))
        calls = []

        def responder(method, url, **kw):
            calls.append((method, url))
            return json_response(500, {"detail": "boom"})

        _patch_http(monkeypatch, responder)
        assert _run(tts_client.synthesize("hi", "p", "QUJD")) is None
        assert calls == [("POST", "http://tts.local:5500/synthesize")]

    def test_synthesize_connection_error_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="omnivoice"))

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(tts_client.synthesize("hi", "p", "QUJD")) is None


class TestLanguageFit:
    """Plan T6 is a pure enum-membership check: the app never maps codes."""

    def test_language_fit_no_parameter_means_not_sent(self):
        assert tts_client._language_value_is_accepted(None, "en") is False

    def test_language_fit_empty_value_means_not_sent(self):
        assert tts_client._language_value_is_accepted({"enum": None}, "") is False

    def test_language_fit_free_form_accepts_any_code(self):
        # dots.tts / omnivoice style: a language parameter without an enum.
        spec = {"name": "language", "type": "string", "enum": None}
        assert tts_client._language_value_is_accepted(spec, "de") is True
        assert tts_client._language_value_is_accepted(spec, "xx") is True

    def test_language_fit_enum_accepts_listed_code(self):
        spec = {"name": "language", "enum": ["en", "de", "zh"]}
        assert tts_client._language_value_is_accepted(spec, "zh") is True

    def test_language_fit_enum_rejects_unlisted_code(self):
        spec = {"name": "language", "enum": ["en", "de", "zh"]}
        assert tts_client._language_value_is_accepted(spec, "xx") is False


# ---------------------------------------------------------------------------
# TTS capabilities: settings-save parameter validation (plan T7)
# ---------------------------------------------------------------------------

class TestValidateTTSParameters:
    """Pure validation of tts.parameters against a capabilities doc.

    The router turns a non-None return into a 422 on settings save; the
    doc used here is the real dots.tts snapshot from factories.py.
    """

    DOC = make_capabilities_doc(engine="dots.tts")

    def test_validate_tts_parameters_all_valid_returns_none(self):
        values = {
            "num_steps": 16,
            "guidance_scale": 0.1,
            "speaker_scale": 1.5,
            "ode_method": "rk4",
            "seed": 7,
        }
        assert tts_client.validate_tts_parameters(self.DOC, values) is None

    def test_validate_tts_parameters_empty_values_passes(self):
        assert tts_client.validate_tts_parameters(self.DOC, {}) is None

    def test_validate_tts_parameters_unknown_parameter_is_named(self):
        error = tts_client.validate_tts_parameters(self.DOC, {"bogus_knob": 1})
        assert error is not None and "bogus_knob" in error

    def test_validate_tts_parameters_integer_above_max_rejected(self):
        # dots.tts num_steps: min 1, max 64
        error = tts_client.validate_tts_parameters(self.DOC, {"num_steps": 100})
        assert "num_steps" in error and "64" in error

    def test_validate_tts_parameters_integer_below_min_rejected(self):
        error = tts_client.validate_tts_parameters(self.DOC, {"num_steps": 0})
        assert "num_steps" in error and "1" in error

    def test_validate_tts_parameters_number_above_max_rejected(self):
        # dots.tts guidance_scale: min 0, max 5
        error = tts_client.validate_tts_parameters(self.DOC, {"guidance_scale": 7.5})
        assert "guidance_scale" in error and "5" in error

    def test_validate_tts_parameters_non_enum_string_rejected_and_names_allowed_values(self):
        error = tts_client.validate_tts_parameters(self.DOC, {"ode_method": "runge-kutta"})
        assert "ode_method" in error and "rk4" in error

    def test_validate_tts_parameters_wrong_type_rejected(self):
        error = tts_client.validate_tts_parameters(self.DOC, {"num_steps": "ten"})
        assert "num_steps" in error and "str" in error

    def test_validate_tts_parameters_bool_is_not_an_integer(self):
        # JSON true must not silently become seed 1.
        error = tts_client.validate_tts_parameters(self.DOC, {"seed": True})
        assert "seed" in error

    def test_validate_tts_parameters_integer_valued_float_is_accepted(self):
        # A hand-edited "num_steps: 10.0" in YAML: tts-serve's lax pydantic
        # models coerce it, so the save path must not 422 it.
        assert tts_client.validate_tts_parameters(self.DOC, {"num_steps": 10.0}) is None

    def test_validate_tts_parameters_non_integer_valued_float_rejected(self):
        error = tts_client.validate_tts_parameters(self.DOC, {"num_steps": 4.5})
        assert "num_steps" in error

    def test_validate_tts_parameters_none_values_are_skipped(self):
        # "Not set" is an absent key; an explicit null is treated the same.
        assert tts_client.validate_tts_parameters(self.DOC, {"seed": None, "num_steps": None}) is None

    def test_validate_tts_parameters_multiple_errors_are_all_named(self):
        error = tts_client.validate_tts_parameters(
            self.DOC, {"bogus": 1, "num_steps": 100, "ode_method": "nope"})
        assert "bogus" in error and "num_steps" in error and "ode_method" in error

    def test_validate_tts_parameters_unrecognized_type_is_skipped(self):
        # The doc is forward-compatible: a spec with a type we don't
        # understand is not our call to make — the server's own 422 is the
        # backstop (same stance as the frontend's raw-JSON escape hatch).
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(
            {"name": "flux", "type": "quaternion", "min": None, "max": None, "enum": None})
        assert tts_client.validate_tts_parameters(doc, {"flux": [1, 2, 3, 4]}) is None


class TestCachedCapabilities:
    def test_cached_capabilities_empty_slot_returns_none_pair(self):
        # The autouse isolation fixture invalidates the slot, so a fresh
        # test sees the "never fetched" state.
        assert tts_client.cached_capabilities() == (None, None)

    def test_cached_capabilities_returns_the_slot_it_holds(self, monkeypatch):
        doc = make_capabilities_doc(engine="dots.tts")
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://tts.local:5500")
        monkeypatch.setattr(tts_client, "_capabilities_cache", doc)

        assert tts_client.cached_capabilities() == ("http://tts.local:5500", doc)


# ---------------------------------------------------------------------------
# TTS capabilities: fetch
# ---------------------------------------------------------------------------

class TestFetchCapabilities:
    def test_fetch_capabilities_success_returns_parsed_doc(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch,
                    lambda method, url, **kw: json_response(
                        200, make_capabilities_doc(engine="dots.tts")))

        doc = _run(tts_client.fetch_capabilities())

        assert doc == make_capabilities_doc(engine="dots.tts")

    def test_fetch_capabilities_uses_capabilities_path_and_short_timeout(self, monkeypatch):
        _active_tts(monkeypatch)
        seen = {}

        def client_factory(*args, **kwargs):
            seen["kwargs"] = kwargs
            return FakeAsyncClient(
                lambda method, url, **kw: (seen.__setitem__("url", url),
                                           json_response(200, make_capabilities_doc()))[1])

        monkeypatch.setattr(tts_client.httpx, "AsyncClient", client_factory)
        _run(tts_client.fetch_capabilities())

        assert seen["url"] == "http://tts.local:5500/capabilities"
        assert seen["kwargs"] == {"timeout": 3.0}

    def test_fetch_capabilities_inactive_tts_returns_none_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        assert _run(tts_client.fetch_capabilities()) is None

    def test_fetch_capabilities_404_returns_none_and_warns(self, monkeypatch, caplog):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch,
                    lambda method, url, **kw: json_response(404, {"detail": "no such endpoint"}))

        with caplog.at_level(logging.WARNING):
            result = _run(tts_client.fetch_capabilities())

        assert result is None
        assert "/capabilities" in caplog.text
        assert "404" in caplog.text

    def test_fetch_capabilities_500_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(500, {"detail": "boom"}))

        assert _run(tts_client.fetch_capabilities()) is None

    def test_fetch_capabilities_connection_error_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(tts_client.fetch_capabilities()) is None

    def test_fetch_capabilities_non_json_body_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch,
                    lambda method, url, **kw: httpx.Response(200, content=b"<html>hi</html>"))

        assert _run(tts_client.fetch_capabilities()) is None

    def test_fetch_capabilities_non_object_json_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(200, ["not", "a", "dict"]))

        assert _run(tts_client.fetch_capabilities()) is None


# ---------------------------------------------------------------------------
# TTS capabilities: cache
# ---------------------------------------------------------------------------

class TestCapabilitiesCache:
    def test_get_capabilities_inactive_tts_returns_none_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        assert _run(tts_client.get_capabilities()) is None

    def test_get_capabilities_same_base_url_fetches_once(self, monkeypatch):
        _active_tts(monkeypatch)
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(200, make_capabilities_doc(engine="omnivoice"))

        _patch_http(monkeypatch, responder)
        first = _run(tts_client.get_capabilities())
        second = _run(tts_client.get_capabilities())

        # Two calls, one fetch: the slot was populated for this base_url.
        assert first == second == make_capabilities_doc(engine="omnivoice")
        assert calls == ["http://tts.local:5500/capabilities"]

    def test_get_capabilities_refetches_when_base_url_changes(self, monkeypatch):
        _active_tts(monkeypatch)
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(200, make_capabilities_doc(engine="omnivoice"))

        _patch_http(monkeypatch, responder)
        _run(tts_client.get_capabilities())

        # Point the app at a *different* TTS server: the cached doc belongs
        # to the old base_url and must not be served for the new one.
        monkeypatch.setattr(
            app_config, "_settings_cache",
            make_settings(tts=TTSConfig(enabled=True, base_url="http://tts.other:5501")),
        )
        _run(tts_client.get_capabilities())

        assert calls == ["http://tts.local:5500/capabilities",
                         "http://tts.other:5501/capabilities"]

    def test_get_capabilities_negative_result_is_cached(self, monkeypatch):
        _active_tts(monkeypatch)
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(404, {"detail": "no /capabilities (old pre-ported script?)"})

        _patch_http(monkeypatch, responder)
        assert _run(tts_client.get_capabilities()) is None
        assert _run(tts_client.get_capabilities()) is None
        assert _run(tts_client.get_capabilities()) is None

        # One fetch for three calls: the failure is cached, so a dead
        # endpoint is never retried across a stream of syntheses.
        assert calls == ["http://tts.local:5500/capabilities"]

    def test_invalidate_capabilities_forces_refetch_and_clears_negative(self, monkeypatch):
        _active_tts(monkeypatch)
        calls = []
        mode = {"status": 404}

        def responder(method, url, **kw):
            calls.append(url)
            if mode["status"] == 200:
                return json_response(200, make_capabilities_doc(engine="omnivoice"))
            return json_response(404, {"detail": "down"})

        _patch_http(monkeypatch, responder)

        # First fetch fails and the failure is cached:
        assert _run(tts_client.get_capabilities()) is None
        assert _run(tts_client.get_capabilities()) is None
        assert len(calls) == 1

        # Invalidation (e.g. the user just saved new settings) forces a
        # refetch, and the recovered server's doc is then cached:
        tts_client.invalidate_capabilities()
        mode["status"] = 200
        assert _run(tts_client.get_capabilities()) == make_capabilities_doc(engine="omnivoice")
        assert _run(tts_client.get_capabilities()) == make_capabilities_doc(engine="omnivoice")
        # Two fetches in total: the initial 404 and the post-invalidation 200;
        # both "get again" pairs were served from the cache.
        assert len(calls) == 2

    def test_ensure_capabilities_warms_cache_and_logs_engine_slug(self, monkeypatch, caplog):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch,
                    lambda method, url, **kw: json_response(
                        200, make_capabilities_doc(engine="dots.tts")))

        with caplog.at_level(logging.INFO, logger="app.services.tts_client"):
            _run(tts_client.ensure_capabilities())

        assert "dots.tts" in caplog.text

        # The warm cache now serves without another request:
        def dead(*a, **kw):
            raise AssertionError("the capabilities cache should have been warm")

        _patch_http(monkeypatch, dead)
        assert _run(tts_client.get_capabilities()) == make_capabilities_doc(engine="dots.tts")

    def test_ensure_capabilities_inactive_tts_is_a_noop(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        _run(tts_client.ensure_capabilities())  # must not raise

    def test_ensure_capabilities_never_raises_when_fetch_raises(self, monkeypatch):
        _active_tts(monkeypatch)

        async def boom():
            raise RuntimeError("fetch blew up")

        monkeypatch.setattr(tts_client, "get_capabilities", boom)
        _run(tts_client.ensure_capabilities())  # must not raise


# ---------------------------------------------------------------------------
# TTS reference audio helpers (pure file I/O)
# ---------------------------------------------------------------------------

class TestReferenceAudioHelpers:
    def test_encode_reference_audio_none_path(self, tmp_path):
        assert tts_client.encode_reference_audio(None) is None

    def test_encode_reference_audio_missing_file(self, tmp_path):
        assert tts_client.encode_reference_audio(str(tmp_path / "nope.wav")) is None

    def test_encode_reference_audio_returns_base64(self, tmp_path):
        wav = tmp_path / "ref.wav"
        wav.write_bytes(b"RIFF-header-bytes")
        import base64

        assert tts_client.encode_reference_audio(str(wav)) == \
            base64.b64encode(b"RIFF-header-bytes").decode("ascii")

    def test_read_transcript_none_path(self, tmp_path):
        assert tts_client.read_transcript(None) is None

    def test_read_transcript_missing_file(self, tmp_path):
        assert tts_client.read_transcript(str(tmp_path / "nope.txt")) is None

    def test_read_transcript_strips_whitespace(self, tmp_path):
        txt = tmp_path / "ref.txt"
        txt.write_text("  a sample transcript\n\n", encoding="utf-8")
        assert tts_client.read_transcript(str(txt)) == "a sample transcript"


# ---------------------------------------------------------------------------
# STT health
# ---------------------------------------------------------------------------

class TestCheckSTTHealth:
    def test_inactive_stt_reports_down_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when STT is inactive")

        _patch_http(monkeypatch, fail)
        assert _run(stt_client.check_stt_health()) is False

    def test_200_means_up(self, monkeypatch):
        _active_stt(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(200, {"ok": True}))
        assert _run(stt_client.check_stt_health()) is True

    def test_404_means_up(self, monkeypatch):
        _active_stt(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(404, {}))
        assert _run(stt_client.check_stt_health()) is True

    def test_500_means_down(self, monkeypatch):
        _active_stt(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(500, {}))
        assert _run(stt_client.check_stt_health()) is False

    def test_connection_error_means_down(self, monkeypatch):
        _active_stt(monkeypatch)

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(stt_client.check_stt_health()) is False


# ---------------------------------------------------------------------------
# STT transcription
# ---------------------------------------------------------------------------

class TestTranscribeAudio:
    def test_inactive_stt_returns_none_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when STT is inactive")

        _patch_http(monkeypatch, fail)
        assert _run(stt_client.transcribe_audio(b"fake-audio")) is None

    def test_empty_audio_returns_none(self, monkeypatch):
        _active_stt(monkeypatch)

        def fail(*a, **kw):
            raise AssertionError("no audio bytes, no request")

        _patch_http(monkeypatch, fail)
        assert _run(stt_client.transcribe_audio(b"")) is None

    def test_success_parses_response_with_defaults_applied(self, monkeypatch):
        _active_stt(monkeypatch)
        seen = {}

        def responder(method, url, **kw):
            seen["url"] = url
            seen["files"] = kw.get("files")
            seen["data"] = kw.get("data")
            return json_response(200, {"text": "hello world", "language": "en",
                                       "language_probability": 0.97})

        _patch_http(monkeypatch, responder)
        result = _run(stt_client.transcribe_audio(b"fake-audio", mime_type="audio/webm"))

        assert result == {"text": "hello world", "language": "en",
                          "language_probability": 0.97}
        assert seen["url"] == "http://stt.local:6600/v1/audio/transcriptions"
        # Multipart: filename derived from the MIME type, raw bytes in the body.
        filename, audio_bytes, mime = seen["files"]["file"]
        assert filename == "audio.webm"
        assert audio_bytes == b"fake-audio"
        assert mime == "audio/webm"
        assert seen["data"] == {"response_format": "json"}

    def test_missing_text_gets_placeholder(self, monkeypatch):
        _active_stt(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(200, {}))

        result = _run(stt_client.transcribe_audio(b"x"))

        assert result["text"] == "No response received from STT server"
        assert result["language"] == "en"  # default when server omits it
        assert result["language_probability"] is None

    def test_mime_parameters_are_stripped(self, monkeypatch):
        _active_stt(monkeypatch)
        seen = {}

        def responder(method, url, **kw):
            seen["files"] = kw.get("files")
            return json_response(200, {"text": "ok"})

        _patch_http(monkeypatch, responder)
        _run(stt_client.transcribe_audio(b"x", mime_type="audio/ogg;rate=44100"))

        filename, _, mime = seen["files"]["file"]
        assert mime == "audio/ogg"
        assert filename == "audio.oga"  # mimetypes knows ogg as .oga

    def test_connect_error_returns_none(self, monkeypatch):
        _active_stt(monkeypatch)

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(stt_client.transcribe_audio(b"x")) is None

    def test_http_error_returns_none(self, monkeypatch):
        _active_stt(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(500, {}))
        assert _run(stt_client.transcribe_audio(b"x")) is None


# ---------------------------------------------------------------------------
# _mime_to_extension
# ---------------------------------------------------------------------------

class TestMimeToExtension:
    @pytest.mark.parametrize("mime,expected", [
        ("audio/webm", "webm"),       # mimetypes has no mapping here: subtype fallback
        ("audio/ogg", "oga"),         # mimetypes' actual mapping, not ".ogg"
        ("audio/wav", "wav"),
        ("audio/utterly-unknown", "utterly-unknown"),  # subtype fallback
        ("garbage-no-slash", "bin"),
        ("", "bin"),
        ("audio/ogg;rate=44100", "oga"),  # parameters stripped
    ])
    def test_mime_to_extension(self, mime, expected):
        assert stt_client._mime_to_extension(mime) == expected

    def test_mime_to_extension_none_input(self):
        assert stt_client._mime_to_extension(None) == "bin"
