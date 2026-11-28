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
from app.services.tts_client import SynthesisOutcome, SynthesisResult
from tests.factories import (
    FakeAsyncClient,
    json_response,
    make_capabilities_doc,
    make_minimal_capabilities_doc,
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
    def test_inactive_tts_returns_no_audio_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        result = _run(tts_client.synthesize("hi", "p", "QUJD"))
        assert result.audio is None

    def test_synthesize_inactive_reports_the_inactive_outcome_with_no_engine_status(
        self, monkeypatch
    ):
        # The router's 503 reads this outcome (issue #171). Two facts the
        # classification depends on: the outcome is INACTIVE, and no
        # engine_status rides along — this request never reached an engine, so
        # it must not arrive at the router dressed as an engine verdict (which
        # is the bucket that answers 502, "ask again").
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        result = _run(tts_client.synthesize("hi", "p", "QUJD"))

        assert result.outcome is SynthesisOutcome.INACTIVE
        assert result.succeeded is False
        assert result.engine_status is None
        assert result.reason()  # the human sentence is still what the user reads

    def test_synthesize_non_cloning_backstop_reports_local_failure_not_inactive(
        self, monkeypatch
    ):
        # INACTIVE means "the FEATURE is off", not "no audio came back". The
        # cold-doc non-cloning backstop consulted a real engine document, so it
        # reports LOCAL_FAILURE — the router's 502, with the router's own 503 as
        # the real answer once the warm cache can say why (pinned in
        # tests/test_routers_tts_stt.py).
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch,
                            make_capabilities_doc(engine="omnivoice", reference_audio=None))
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(
            200, {"audio_base64": "QUJD", "sample_rate": 24000}))

        result = _run(tts_client.synthesize("hi", "p", "QUJD"))

        assert result.outcome is SynthesisOutcome.LOCAL_FAILURE
        assert result.succeeded is False

    # -- the outcome/field invariants ---------------------------------------
    #
    # The router derives the caller's status from `outcome` alone, so a result
    # carrying a rival signal (an engine status on a local outcome, audio on a
    # failure) must be impossible to build: "which field wins" cannot be allowed
    # to become a runtime question.

    def test_synthesis_result_with_audio_but_no_audio_payload_raises(self):
        with pytest.raises(ValueError, match="requires audio"):
            SynthesisResult(outcome=SynthesisOutcome.AUDIO)

    def test_synthesis_result_failure_carrying_audio_raises(self):
        with pytest.raises(ValueError, match="must not carry audio"):
            SynthesisResult(
                outcome=SynthesisOutcome.LOCAL_FAILURE,
                audio={"audio_base64": "QUJD", "sample_rate": 24000},
            )

    def test_synthesis_result_engine_verdict_without_a_status_raises(self):
        with pytest.raises(ValueError, match="requires engine_status"):
            SynthesisResult(
                outcome=SynthesisOutcome.ENGINE_VERDICT, detail="no sentence, no number"
            )

    def test_synthesis_result_local_outcome_carrying_an_engine_status_raises(self):
        # The exact confusion issue #171 was heading towards: a locally-decided
        # failure smuggling a number would let the router's engine rule answer
        # for a request that never reached an engine.
        with pytest.raises(ValueError, match="must not carry an engine status"):
            SynthesisResult(
                outcome=SynthesisOutcome.LOCAL_REFUSAL, engine_status=422, detail="nope"
            )

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

        assert result.audio == {"audio_base64": "QUJD", "sample_rate": 24000}
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
            assert _run(tts_client.synthesize("hi", "p", "QUJD")).audio is None

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
            assert _run(tts_client.synthesize("hi", "p", "QUJD")).audio is not None
            assert _run(tts_client.synthesize("hi", "p", "QUJD")).audio is not None

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

        assert result.audio == {"audio_base64": "QUJD", "sample_rate": 24000}
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
            assert _run(tts_client.synthesize("hi", "p", "QUJD")).audio is None

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
        assert _run(tts_client.synthesize("hi", "p", "QUJD")).audio is None
        assert calls == [("POST", "http://tts.local:5500/synthesize")]

    def test_synthesize_connection_error_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="omnivoice"))

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(tts_client.synthesize("hi", "p", "QUJD")).audio is None

    # -----------------------------------------------------------------------
    # Content rejections — the 422 self-heal exists for a STALE DOCUMENT, not
    # for a request the engine refuses to voice.
    #
    # The discriminator is the 422 body, and it is written the narrow way
    # round: only a JSON object with a STRING `detail` is a tts-serve content
    # pre-flight (LuxTTS text its tokenizer empties, VoxCPM transcript-without-
    # clip, PocketTTS clip+voice, Breeze cfg_scale-without-instruction).
    # Everything else keeps the self-heal it always had, because "the cached
    # document may describe a retired engine" is the only theory worth acting
    # on for them: FastAPI validation (detail is a LIST of {type, loc, msg,
    # input}), a reverse proxy's HTML error page (not JSON at all), and a
    # non-tts-serve server's own envelope ({"error": {...}}, {}, a bare list).
    #
    # Why it matters: streaming TTS issues one /synthesize per sentence. The
    # self-heal invalidates the capabilities cache, so a retried content 422
    # costs two synthesis slots on an engine whose synthesis is serialized AND
    # a /capabilities refetch for every remaining sentence of the reply.
    # -----------------------------------------------------------------------

    UNSPEAKABLE_TEXT_DETAIL = (
        "text contains nothing this engine can speak: LuxTTS keeps only ASCII "
        "letters, Chinese characters and <pinyin>/[tag] parts, and drops "
        "punctuation, digits, emoji and every other script (Cyrillic, Greek, "
        "Japanese, Korean, Arabic, ...).  Send at least one English or Chinese "
        "word."
    )
    NO_SPEECH_IN_CLIP_DETAIL = (
        "reference audio contains no recognizable speech in its first 5 s: the "
        "engine transcribes only that window with Whisper and conditions on "
        "the transcript, so silence, music, or speech other than English / "
        "Chinese cannot be cloned.  Start the clip with speech, or raise "
        "prompt_duration."
    )

    def test_synthesize_422_with_string_detail_does_not_refetch_or_retry(self, monkeypatch, caplog):
        # GIVEN a 422 that is a content verdict (string detail), not a
        # field-shape complaint:
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(422, {"detail": self.UNSPEAKABLE_TEXT_DETAIL})

        _patch_http(monkeypatch, responder)
        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            result = _run(tts_client.synthesize("...", "p", "QUJD"))

        # THEN exactly one POST and no /capabilities refetch...
        assert calls == ["http://tts.local:5500/synthesize"]
        # ...the cached document survives, so the reply's next sentence does
        # not refetch it (an invalidated cache makes every sentence pay)...
        assert tts_client._capabilities_cache is not None
        # ...the engine's own verdict reaches the caller...
        assert result.audio is None
        assert result.outcome is SynthesisOutcome.ENGINE_VERDICT
        assert result.engine_status == 422
        assert "nothing this engine can speak" in result.detail
        # ...and the two 422 classes read differently in the log.
        assert "refused the CONTENT" in caplog.text
        assert "rejected the payload (422)" not in caplog.text

    def test_synthesize_422_with_list_detail_still_self_heals(self, monkeypatch):
        # The list shape is the self-heal's trigger and stays untouched: a
        # content-only change must not silence the stale-document recovery.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        posts = []

        def responder(method, url, **kw):
            if url.endswith("/capabilities"):
                return json_response(200, make_capabilities_doc(engine="dots.tts"))
            posts.append(kw.get("json"))
            if len(posts) == 1:
                return json_response(422, make_unexpected_field_422("speaker_scale", 1.5))
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 48000})

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hello", "p", "QUJD"))

        assert result.audio == {"audio_base64": "QUJD", "sample_rate": 48000}
        assert len(posts) == 2

    def test_synthesize_reference_clip_without_speech_keeps_the_engine_400_detail(self, monkeypatch):
        # The 400 is the persona's reference clip, and the engine names the
        # window the user can act on: that sentence must reach the caller.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))

        def responder(method, url, **kw):
            return json_response(400, {"detail": self.NO_SPEECH_IN_CLIP_DETAIL})

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hello there", "p", "QUJD"))

        assert result.audio is None
        assert result.outcome is SynthesisOutcome.ENGINE_VERDICT
        assert result.engine_status == 400
        assert "prompt_duration" in result.detail
        # reason() is what the browser prints under the reply, so it is the
        # engine's sentence VERBATIM: no "TTS engine (HTTP 400):" prefix, and
        # no JSON quoting of a string detail.
        assert result.reason() == self.NO_SPEECH_IN_CLIP_DETAIL

    def test_content_refusal_recognizes_the_string_sentence_only(self):
        # GIVEN the responses a 422 can arrive as:
        schema_422 = json_response(422, make_unexpected_field_422("speaker_scale", 1.5))
        content_422 = json_response(422, {"detail": self.UNSPEAKABLE_TEXT_DETAIL})
        bodyless_422 = json_response(422, {})
        envelope_422 = json_response(422, {"error": {"message": "nope"}})
        html_422 = httpx.Response(
            422, text="<html>502 proxy</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("POST", "http://tts.local:5500/synthesize"),
        )

        # THEN only the JSON object with a string detail is a content refusal:
        # every other shape keeps the stale-document self-heal, so a proxy that
        # rewrites the body or a non-tts-serve engine cannot quietly lose it.
        assert tts_client._is_content_refusal(content_422) is True
        assert tts_client._is_content_refusal(schema_422) is False
        assert tts_client._is_content_refusal(bodyless_422) is False
        assert tts_client._is_content_refusal(envelope_422) is False
        assert tts_client._is_content_refusal(html_422) is False

    def test_synthesize_422_from_a_proxy_html_body_still_self_heals(self, monkeypatch):
        # A reverse proxy in front of the engine turns the 422 body into HTML.
        # That is not a tts-serve content sentence, so the self-heal must run:
        # losing it here is how a stale document becomes sticky behind a proxy.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        posts = []

        def responder(method, url, **kw):
            if url.endswith("/capabilities"):
                return json_response(200, make_capabilities_doc(engine="dots.tts"))
            posts.append(kw.get("json"))
            if len(posts) == 1:
                return httpx.Response(
                    422, text="<html>gateway rejected the request</html>",
                    headers={"content-type": "text/html"},
                    request=httpx.Request("POST", url),
                )
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 48000})

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hello", "p", "QUJD"))

        assert result.audio == {"audio_base64": "QUJD", "sample_rate": 48000}
        assert len(posts) == 2

    def test_synthesize_422_from_a_non_fastapi_server_still_self_heals(self, monkeypatch):
        # Same reasoning for a server that answers in its own envelope: the
        # payload came from the cached document, so refetch it and retry once.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        posts = []

        def responder(method, url, **kw):
            if url.endswith("/capabilities"):
                return json_response(200, make_capabilities_doc(engine="dots.tts"))
            posts.append(kw.get("json"))
            if len(posts) == 1:
                return json_response(422, {"error": {"message": "unexpected field"}})
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 48000})

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hello", "p", "QUJD"))

        assert result.audio == {"audio_base64": "QUJD", "sample_rate": 48000}
        assert len(posts) == 2

    def test_string_detail_is_not_json_quoted_for_the_user(self):
        # The router shows this string in the UI. json.dumps'ing it makes the
        # bubble read '"text contains nothing this engine can speak: ..."'.
        response = json_response(422, {"detail": self.UNSPEAKABLE_TEXT_DETAIL})

        assert tts_client._response_detail(response) == self.UNSPEAKABLE_TEXT_DETAIL

        listed = json_response(422, make_unexpected_field_422("num_steps", 999))
        assert "num_steps" in tts_client._response_detail(listed)

    # -- the surviving shape rejection: log the list, show a sentence --------
    #
    # A FastAPI validation failure is the one engine failure body that is not
    # written for a human reader, and by the time one reaches the caller the
    # stale-document self-heal has already rebuilt the payload from a fresh
    # capabilities document and asked again. Serializing that list into the
    # .tts-note is the symptom issue #173 set out to remove, re-entering
    # through the ENGINE_VERDICT path rather than through the status code.

    def test_synthesize_shape_rejection_surviving_the_self_heal_returns_a_sentence(
        self, monkeypatch, caplog
    ):
        # GIVEN an engine whose request model rejects our payload twice (a
        # tts.parameters entry the current engine never advertised):
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
            result = _run(tts_client.synthesize("hi", "p", "QUJD"))

        # THEN the caller (and through it the user's bubble) gets a sentence
        # naming where to fix it, not a serialized field list:
        assert len(posts) == 2, "the self-heal must still run first"
        assert result.outcome is SynthesisOutcome.ENGINE_VERDICT
        assert result.engine_status == 422
        assert result.reason() == tts_client.PAYLOAD_REJECTION_DETAIL
        assert "loc" not in result.detail and "num_steps" not in result.detail, (
            f"the bubble must not quote a validation list: {result.detail}"
        )
        # ...and the field name is NOT lost: it is in the log line, which is
        # the only place it was ever readable.
        assert "num_steps" in caplog.text

    def test_payload_rejection_detail_only_fires_for_a_validation_list(self):
        # GIVEN each body a failed /synthesize can arrive as,
        string_detail = json_response(422, {"detail": self.UNSPEAKABLE_TEXT_DETAIL})
        list_detail = json_response(422, make_unexpected_field_422("speaker_scale", 1.5))
        bodyless = json_response(422, {})
        foreign_envelope = json_response(422, {"error": {"message": "bad payload"}})
        html = httpx.Response(
            422, text="<html>gateway rejected the request</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("POST", "http://tts.local:5500/synthesize"),
        )

        # THEN only the list — the one body provably not written for a human —
        # is replaced. The engine's own sentence, a foreign envelope, and a
        # proxy's page all keep their words: we can't improve on them, and
        # inventing a cause we cannot see would be a worse lie than quoting
        # what the box actually said.
        assert tts_client._payload_rejection_detail(list_detail) == (
            tts_client.PAYLOAD_REJECTION_DETAIL
        )
        assert tts_client._payload_rejection_detail(string_detail) is None
        assert tts_client._payload_rejection_detail(bodyless) is None
        assert tts_client._payload_rejection_detail(foreign_envelope) is None
        assert tts_client._payload_rejection_detail(html) is None

    def test_payload_rejection_detail_is_a_sentence_not_a_field_dump(self):
        # This string reaches a bubble. It must read as advice and name the
        # place to fix, while claiming only the likeliest cause.
        sentence = tts_client.PAYLOAD_REJECTION_DETAIL

        assert sentence.startswith("the connected engine")
        assert "parameters" in sentence and "Servers dialog" in sentence
        assert not sentence.startswith(("{", "["))
        assert "loc" not in sentence and "msg" not in sentence


class TestReferenceTextRequirement:
    """Whose transcript is it anyway? (issue #164)

    The persona supplies the clip; the ENGINE decides whether a transcript of
    it is mandatory. These tests are the fence between the two axes: too
    strict and every LuxTTS persona is permanently mute, too lax and a
    Breeze-Blue persona pays a doomed synthesis per sentence.
    """

    @staticmethod
    def _required_doc(engine="breeze-blue"):
        """Synthetic doc marking reference_text required (see the router suite
        for why no shipped snapshot can be used here)."""
        doc = make_minimal_capabilities_doc(engine=engine)
        for spec in doc["parameters"]:
            if spec["name"] == "reference_text":
                spec["required"] = True
        return doc

    # -- the predicate ------------------------------------------------------

    def test_doc_requires_reference_text_optional_advertisement_is_false(self):
        assert tts_client.doc_requires_reference_text(
            make_capabilities_doc(engine="dots.tts")) is False

    def test_doc_requires_reference_text_engine_without_the_field_is_false(self):
        # chatterbox / luxTTS / indexTTS / pocketTTS: the transcript is not
        # merely optional, it is unsendable. Absent spec can never mean
        # "mandatory".
        assert tts_client.doc_requires_reference_text(
            make_capabilities_doc(engine="chatterbox")) is False

    def test_doc_requires_reference_text_required_advertisement_is_true(self):
        assert tts_client.doc_requires_reference_text(self._required_doc()) is True

    def test_doc_requires_reference_text_without_a_document_is_false(self):
        # "No evidence" is never "known requirement" — the same polarity rule
        # as doc_supports_reference_audio, and the reason an engine with no
        # /capabilities endpoint still gets asked.
        assert tts_client.doc_requires_reference_text(None) is False

    def test_doc_requires_reference_text_non_boolean_flag_is_false(self):
        # The document comes from a foreign server and is only validated as a
        # dict: a truthy-but-not-True flag is a malformed doc, and "skip what
        # we cannot judge" means the engine decides, not us.
        doc = self._required_doc()
        for spec in doc["parameters"]:
            if spec["name"] == "reference_text":
                spec["required"] = "yes please"
        assert tts_client.doc_requires_reference_text(doc) is False

    # -- the sentence -------------------------------------------------------

    def test_reference_text_refusal_is_silent_when_a_transcript_exists(self):
        assert tts_client.reference_text_refusal(
            "Alex", self._required_doc(), "the words") is None

    def test_reference_text_refusal_is_silent_when_the_engine_does_not_require_it(self):
        assert tts_client.reference_text_refusal(
            "Alex", make_capabilities_doc(engine="chatterbox"), None) is None

    def test_reference_text_refusal_is_silent_without_a_document(self):
        assert tts_client.reference_text_refusal("Alex", None, None) is None

    def test_reference_text_refusal_names_persona_engine_and_fix(self):
        refusal = tts_client.reference_text_refusal("Alex", self._required_doc(), None)

        assert refusal is not None
        # Rendered verbatim under the reply as
        # "Voice unavailable for this reply: <this>", so it has to read as
        # advice: who, what, and where to fix it.
        assert "Alex" in refusal
        assert "breeze-blue" in refusal
        assert "transcript" in refusal

    def test_reference_text_refusal_survives_a_document_without_an_engine_slug(self):
        # fetch_capabilities_url validates "a dict", nothing more; a doc with
        # no `engine` key must not TypeError the refusal path.
        doc = self._required_doc()
        del doc["engine"]

        refusal = tts_client.reference_text_refusal("Alex", doc, "")

        assert refusal is not None and "connected TTS engine" in refusal

    # -- the synthesis-side pre-flight --------------------------------------

    def test_synthesize_refuses_transcriptless_persona_without_posting_when_required(self, monkeypatch):
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, self._required_doc())
        urls = []

        def responder(method, url, **kw):
            urls.append(url)
            raise AssertionError("no request may be spent on a doomed synthesis")

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hi", None, "QUJD", persona_name="Alex"))

        assert urls == []  # neither /capabilities (cached) nor /synthesize
        assert result.succeeded is False
        # A refusal decided HERE, from the document in hand, is reported as a
        # local refusal: no engine number attached (none exists), and the
        # router answers it with the 422 the browser reads as "stop asking for
        # this reply, show the reason".
        assert result.outcome is SynthesisOutcome.LOCAL_REFUSAL
        assert result.engine_status is None
        assert "Alex" in result.reason()

    def test_synthesize_sends_no_reference_text_when_the_engine_does_not_require_it(self, monkeypatch):
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        seen = {}

        def responder(method, url, **kw):
            seen["payload"] = kw.get("json")
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        _patch_http(monkeypatch, responder)
        result = _run(tts_client.synthesize("hi", None, "QUJD", persona_name="Luna"))

        assert result.succeeded is True
        assert "reference_text" not in seen["payload"]  # nothing invented

    def test_synthesize_warns_once_per_persona_and_engine(self, monkeypatch, caplog):
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, self._required_doc())
        _patch_http(monkeypatch, lambda *a, **kw: json_response(200, {}))

        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            for _ in range(3):
                _run(tts_client.synthesize("hi", None, "QUJD", persona_name="Alex"))

        assert sum("No TTS for persona" in r.message for r in caplog.records) == 1

    def test_synthesize_warns_again_after_an_engine_switch(self, monkeypatch, caplog):
        # The verdict is about the (engine, persona) pair: point the same
        # persona at a second transcript-mandatory engine and it is news again.
        _active_tts(monkeypatch)
        _cache_capabilities(monkeypatch, self._required_doc())
        _patch_http(monkeypatch, lambda *a, **kw: json_response(200, {}))

        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            _run(tts_client.synthesize("hi", None, "QUJD", persona_name="Alex"))
            tts_client.invalidate_capabilities()
            _cache_capabilities(monkeypatch, self._required_doc(engine="ming3-tts-moe"))
            _run(tts_client.synthesize("hi", None, "QUJD", persona_name="Alex"))

        assert sum("No TTS for persona" in r.message for r in caplog.records) == 2


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

    def test_validate_tts_parameters_non_numeric_min_max_are_skipped(self):
        # The doc is external and only validated as a dict: a malformed
        # bound must be skipped, not TypeError the synchronous save path
        # (which would 500 PUT /api/settings). 100 is deliberately above
        # the real max (64): with the bound intact it would be rejected.
        doc = make_capabilities_doc(engine="dots.tts")
        self._set_bounds(doc, "num_steps", "low", "high")
        assert tts_client.validate_tts_parameters(doc, {"num_steps": 100}) is None

    def test_validate_tts_parameters_bool_min_max_are_not_bounds(self):
        # bool is an int subclass in Python: without the explicit exclusion
        # True/False would compare as 1/0 and silently mis-judge (100 >
        # False). A JSON true/false in a bound slot is malformed, not 1/0.
        doc = make_capabilities_doc(engine="dots.tts")
        self._set_bounds(doc, "num_steps", True, False)
        assert tts_client.validate_tts_parameters(doc, {"num_steps": 100}) is None

    def test_validate_tts_parameters_malformed_min_still_checks_intact_max(self):
        # Partial degradation: only the broken bound is skipped — a value
        # beyond the intact max is still rejected.
        doc = make_capabilities_doc(engine="dots.tts")
        self._set_bounds(doc, "num_steps", "low", 64)
        error = tts_client.validate_tts_parameters(doc, {"num_steps": 100})
        assert "num_steps" in error and "64" in error

    def test_validate_tts_parameters_array_conforming_items_pass(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec())
        # The indexTTS shape: a fixed 8-item number vector. Note the
        # per-item [0, 1] range is NOT in the doc (only the item type and
        # the count are), so out-of-range values pass here — the engine's
        # own validator is the backstop for those.
        assert tts_client.validate_tts_parameters(
            doc, {"emotion_vector": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]}) is None

    def test_validate_tts_parameters_array_not_a_list_rejected(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec())
        # A stale hand-edited settings.yaml holding a string instead:
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": "0.1 0.2"})
        assert "emotion_vector" in error and "array" in error and "str" in error

    def test_validate_tts_parameters_array_too_few_items_rejected(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec())
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [0.5]})
        assert "emotion_vector" in error and "8" in error

    def test_validate_tts_parameters_array_too_many_items_rejected(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec())
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [0.5] * 9})
        assert "emotion_vector" in error and "8" in error

    def test_validate_tts_parameters_array_wrong_item_type_rejected_and_indexed(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(item_type="integer"))
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [1, 1.5]})
        # The offending item is named by index, mirroring the frontend:
        assert "emotion_vector[1]" in error and "integer" in error

    def test_validate_tts_parameters_array_bool_item_is_not_a_number(self):
        # bool is an int subclass in Python: a JSON true inside a numeric
        # vector must not silently pass as 1.
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec())
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [True, 0.5]})
        assert "emotion_vector[0]" in error and "number" in error

    def test_validate_tts_parameters_array_integer_item_accepts_integer_valued_float(self):
        # A hand-edited "2.0" in YAML: tts-serve's lax models coerce it, so
        # the save path must not 422 a value the server takes (same stance
        # as the top-level integer check).
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(item_type="integer"))
        assert tts_client.validate_tts_parameters(
            doc, {"emotion_vector": [1, 2.0, 3, 4, 5, 6, 7, 8]}) is None

    def test_validate_tts_parameters_array_string_item_rejects_non_string(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(item_type="string"))
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": ["a", 2]})
        assert "emotion_vector[1]" in error and "string" in error

    def test_validate_tts_parameters_array_boolean_item_rejects_non_boolean(self):
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(item_type="boolean"))
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [True, 1]})
        assert "emotion_vector[1]" in error and "boolean" in error

    def test_validate_tts_parameters_array_unknown_item_type_checks_structure_only(self):
        # tts-serve cannot emit this (DerivationError at server import); a
        # malformed doc still gets structure + count checks, and the
        # server's own 422 is the backstop for per-item conformance.
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(item_type=None))
        assert tts_client.validate_tts_parameters(
            doc, {"emotion_vector": ["whatever"] * 8}) is None
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [1, 2]})
        assert "emotion_vector" in error and "8" in error

    def test_validate_tts_parameters_array_malformed_item_bounds_are_skipped(self):
        # The doc is external: a malformed item-count bound must be skipped,
        # not TypeError the synchronous save path (which would 500
        # PUT /api/settings) — the same policy as min/max.
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(min_items="low", max_items="high"))
        assert tts_client.validate_tts_parameters(
            doc, {"emotion_vector": [0.5] * 100}) is None

    def test_validate_tts_parameters_array_non_integral_float_item_bounds_are_skipped(self):
        # min_items/max_items are item-COUNT bounds: a non-integral float
        # (8.5) is a malformed doc field and must be skipped, not enforced —
        # with the old float comparison, 7 items 422'd with "at least 8.5
        # items" and 13 with "at most 12.5 items" (and 8 items, despite
        # satisfying the obvious intent, failed min_items 8.5 as well).
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(min_items=8.5, max_items=12.5))
        for length in (0, 1, 7, 8, 9, 12, 13, 100):
            assert tts_client.validate_tts_parameters(
                doc, {"emotion_vector": [0.5] * length}) is None, \
                f"malformed count bounds must not judge a {length}-item array"

    def test_validate_tts_parameters_array_bool_item_bounds_are_not_counts(self):
        # bool is an int subclass in Python: a JSON true/false in an
        # item-count slot is malformed, not 1/0 (same stance as
        # _numeric_bound for min/max).
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(min_items=True, max_items=False))
        for length in (0, 1, 2, 8, 100):
            assert tts_client.validate_tts_parameters(
                doc, {"emotion_vector": [0.5] * length}) is None

    def test_validate_tts_parameters_array_integral_float_item_bounds_coerce_to_int(self):
        # 8.0 is an unambiguous count: it is enforced like 8 (not skipped),
        # and the error names the count as 8, not 8.0 — mirroring the
        # frontend, where JS has no int/float split (8.0 === 8).
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(min_items=8.0, max_items=8.0))
        assert tts_client.validate_tts_parameters(
            doc, {"emotion_vector": [0.5] * 8}) is None
        for length in (7, 9):
            error = tts_client.validate_tts_parameters(
                doc, {"emotion_vector": [0.5] * length})
            assert error is not None and "8" in error and "8.0" not in error

    def test_validate_tts_parameters_item_labels_are_presentational_only(self):
        # item_labels exist for the frontend's one-labeled-row-per-item
        # rendering and are NOT part of the save-time contract: a well-formed
        # list changes nothing about validation, the count/item rules still
        # apply, and a malformed list (the frontend degrades one to name[i]
        # labels) must not 422 the synchronous save path.
        good = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        doc = make_capabilities_doc(engine="dots.tts")
        doc["parameters"].append(self._array_spec(item_labels=[
            "happy", "angry", "sad", "afraid", "disgusted",
            "melancholic", "surprised", "calm"]))
        assert tts_client.validate_tts_parameters(doc, {"emotion_vector": good}) is None
        error = tts_client.validate_tts_parameters(doc, {"emotion_vector": [0.5]})
        assert "emotion_vector" in error and "8" in error, \
            "the count rules still apply next to item_labels"

        for bad in (["only", "two"], ["happy", "", "calm"], "happy", None):
            doc2 = make_capabilities_doc(engine="dots.tts")
            doc2["parameters"].append(self._array_spec(item_labels=bad))
            assert tts_client.validate_tts_parameters(
                doc2, {"emotion_vector": good}) is None, \
                f"malformed item_labels {bad!r} must be ignored"

    @staticmethod
    def _set_bounds(doc, name, minimum, maximum):
        """Overwrite the min/max of the named parameter in a doc."""
        for entry in doc["parameters"]:
            if entry["name"] == name:
                entry["min"], entry["max"] = minimum, maximum

    @staticmethod
    def _array_spec(name="emotion_vector", item_type="number",
                    min_items=8, max_items=8, item_labels=None) -> dict:
        """A synthetic v2 array parameter spec (no real snapshot has one yet)."""
        return {
            "name": name, "type": "array", "item_type": item_type,
            "required": False, "default": None, "description": None,
            "min": None, "max": None, "step": None, "enum": None,
            "min_items": min_items, "max_items": max_items,
            "item_labels": item_labels,
        }


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
        assert filename == "audio.ogg"  # extension from the MIME subtype

    def test_filename_never_depends_on_host_mimetypes_database(self, monkeypatch):
        # Integration-level regression guard for the macOS '.weba' bug
        # report (the unit-level guard lives in tests/test_mime.py): even
        # if a future change made the filename derivation consult the OS
        # mime database again, this fails on every platform — not just the
        # ones whose database disagrees — and pins the user-visible symptom
        # (the multipart part name) itself.
        _active_stt(monkeypatch)

        def poison(*args, **kwargs):
            raise AssertionError("must not consult the host OS's mime database")

        monkeypatch.setattr("mimetypes.guess_extension", poison)
        seen = {}

        def responder(method, url, **kw):
            seen["files"] = kw.get("files")
            return json_response(200, {"text": "ok"})

        _patch_http(monkeypatch, responder)
        _run(stt_client.transcribe_audio(b"x", mime_type="audio/webm"))

        filename, _, _ = seen["files"]["file"]
        assert filename == "audio.webm"

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
