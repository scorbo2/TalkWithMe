"""API tests for app/routers/tts.py and app/routers/stt.py — the proxy endpoints.

The service clients (synthesize / transcribe_audio / health checks) are
monkeypatched at the router's import site; the reference-file handling is
exercised for real against tmp_path files.
"""

import base64

import httpx
import pytest

import app.config as app_config
import app.routers.stt as stt_router
import app.routers.tts as tts_router
import app.services.tts_client as tts_client
from app.services.tts_client import SynthesisOutcome, SynthesisResult
from app.config import Persona, PersonasConfig
from tests.factories import (
    FakeAsyncClient,
    json_response,
    make_capabilities_doc,
    make_engine_verdict,
    make_minimal_capabilities_doc,
    make_personas,
    make_settings,
    make_synthesis_audio,
)
from app.config import STTConfig, TTSConfig


def _active_tts_settings(monkeypatch, **tts_kwargs):
    tts = TTSConfig(enabled=True, base_url="http://tts.local:5500", **tts_kwargs)
    monkeypatch.setattr(app_config, "_settings_cache", make_settings(tts=tts))


def _active_stt_settings(monkeypatch, **stt_kwargs):
    stt = STTConfig(enabled=True, base_url="http://stt.local:6600", **stt_kwargs)
    monkeypatch.setattr(app_config, "_settings_cache", make_settings(stt=stt))


def _persona_cache(monkeypatch, personas: PersonasConfig):
    monkeypatch.setattr(app_config, "_personas_cache", personas)


# ---------------------------------------------------------------------------
# TTS health
# ---------------------------------------------------------------------------

class TestTTSHealth:
    # The health payload is the sidebar's only window onto the engine axis
    # (issue #164 phase 2), so it is asserted as a whole dict: a missing field
    # fails here instead of silently leaving a mute badge un-drawn.

    def _health_stubs(self, monkeypatch, health=(True, "dots.tts"), capabilities=None):
        """Stub both discovery calls at the router's import site (no network)."""
        monkeypatch.setattr(tts_router, "check_tts_health", lambda: _coro(health))
        monkeypatch.setattr(tts_router, "get_capabilities", lambda: _coro(capabilities))

    def test_inactive_reports_disabled_without_network(self, client):
        # TTS inactive: both discovery calls self-guard, so nothing touches
        # the wire — no httpx stub installed, and a real attempt would hang
        # the hermetic suite instead.
        resp = client.get("/api/tts/health")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "available": False,
                               "streaming": False, "server_type": None,
                               "reference_text_required": False}

    def test_active_reports_health_result(self, client, monkeypatch):
        _active_tts_settings(monkeypatch, streaming=True)
        self._health_stubs(monkeypatch, capabilities=make_capabilities_doc(engine="dots.tts"))

        resp = client.get("/api/tts/health")

        assert resp.json() == {"enabled": True, "available": True,
                               "streaming": True, "server_type": "dots.tts",
                               "reference_text_required": False}

    def test_active_but_unreachable(self, client, monkeypatch):
        _active_tts_settings(monkeypatch)
        self._health_stubs(monkeypatch, health=(False, None), capabilities=None)

        resp = client.get("/api/tts/health")

        assert resp.json()["enabled"] is True
        assert resp.json()["available"] is False
        assert resp.json()["reference_text_required"] is False

    def test_health_reports_reference_text_required_when_the_engine_demands_it(
        self, client, monkeypatch
    ):
        doc = make_capabilities_doc(engine="dots.tts")
        for spec in doc["parameters"]:
            if spec["name"] == "reference_text":
                spec["required"] = True
        _active_tts_settings(monkeypatch)
        self._health_stubs(monkeypatch, capabilities=doc)

        resp = client.get("/api/tts/health")

        assert resp.json()["reference_text_required"] is True

    def test_health_reports_reference_text_not_required_for_an_engine_without_the_field(
        self, client, monkeypatch
    ):
        # chatterbox never advertises reference_text: a persona with no
        # transcript is perfectly speakable there and must show no badge.
        _active_tts_settings(monkeypatch)
        self._health_stubs(monkeypatch, capabilities=make_capabilities_doc(engine="chatterbox"))

        resp = client.get("/api/tts/health")

        assert resp.json()["reference_text_required"] is False

    def test_health_fetches_capabilities_when_the_slot_is_cold_after_a_settings_save(
        self, client, monkeypatch
    ):
        # A settings save clears the capabilities slot on purpose. If health
        # read only the cache, the badge would flicker OFF exactly when the
        # user switches engines and looks at the sidebar — so health is the one
        # route allowed to fetch, and it warms the slot for the synthesis path.
        doc = make_capabilities_doc(engine="dots.tts")
        for spec in doc["parameters"]:
            if spec["name"] == "reference_text":
                spec["required"] = True
        _active_tts_settings(monkeypatch)
        monkeypatch.setattr(tts_router, "check_tts_health", lambda: _coro((True, "breeze-blue")))
        fetched = {}

        async def fake_get_capabilities():
            fetched["called"] = True
            tts_client._capabilities_cache = doc
            tts_client._capabilities_base_url = "http://tts.local:5500"
            return doc

        monkeypatch.setattr(tts_router, "get_capabilities", fake_get_capabilities)

        resp = client.get("/api/tts/health")

        assert fetched.get("called") is True
        assert resp.json()["reference_text_required"] is True
        # Warmed for the synthesis path, keyed on the url it was fetched for.
        assert tts_client.cached_capabilities() == ("http://tts.local:5500", doc)


# ---------------------------------------------------------------------------
# TTS proxy
# ---------------------------------------------------------------------------

class TestTTSProxy:
    def _tts_capable_persona(self, tmp_path, transcript=True):
        """A persona whose reference files really exist under tmp_path.

        `transcript=False` builds the issue-#164 persona: a real clip and no
        ref.txt at all, which is exactly what a LuxTTS user uploads.
        """
        wav = tmp_path / "luna.wav"
        wav.write_bytes(b"RIFF-ref")
        txt = None
        if transcript:
            txt = tmp_path / "luna.txt"
            txt.write_text("a reference transcript", encoding="utf-8")
        return Persona(
            name="Luna",
            system_prompt="You are Luna.",
            router_hints="philosophy",
            reference_audio=str(wav),
            reference_audio_transcript=str(txt) if txt else None,
            reference_audio_language="en",
        )

    def _cache_doc(self, monkeypatch, doc):
        """Warm the capabilities slot for the router's own base_url."""
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://tts.local:5500")
        monkeypatch.setattr(tts_client, "_capabilities_cache", doc)

    @staticmethod
    def _engine_requiring_transcript():
        """A synthetic capabilities doc for a transcript-mandatory engine.

        Breeze Blue, Ming3-TTS-MoE and Qwen3-TTS-MLX mark reference_text
        required, but none of them has a snapshot in tests/fixtures/ — and
        test_tts_fixtures.py pins those snapshots to real tts-serve output, so
        one may not be invented here. The minimal document is the sanctioned
        synthetic baseline; the refusal path reads the parameter's `required`
        flag and the `engine` slug, and nothing else.
        """
        doc = make_minimal_capabilities_doc(engine="breeze-blue")
        for spec in doc["parameters"]:
            if spec["name"] == "reference_text":
                spec["required"] = True
        return doc

    def test_unknown_persona_404(self, client):
        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "NoSuchOne"})
        assert resp.status_code == 404

    def test_persona_without_reference_audio_400(self, client):
        # The persona axis: no clip, nothing to clone from. A missing
        # TRANSCRIPT is not this error (issue #164) — see the 422 tests below.
        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Alex"})
        assert resp.status_code == 400
        assert "reference audio" in resp.json()["detail"]

    def test_unreadable_reference_audio_503(self, client, monkeypatch):
        personas = make_personas()
        personas.personas[1].reference_audio = "/nonexistent/luna.wav"
        _persona_cache(monkeypatch, personas)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})
        assert resp.status_code == 503

    # -- the feature is off: 503, and the router knows it by FLAG, not prose --

    def test_inactive_tts_503s_without_network_for_a_tts_capable_persona(
        self, client, monkeypatch, tmp_path
    ):
        # settings.yaml with TTS disabled (or with no tts base_url) and a
        # persona that could speak: the router has no is_active guard of its
        # own, so synthesize() refuses and flags the result. 503 is the only
        # honest status — 502 would tell the browser "engine hiccup, ask
        # again", and the streaming client would re-ask per sentence.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path)]))

        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(fail))

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 503
        # The client's sentence forwarded verbatim (wording is not what this
        # test is for — the constant is the wording, and it is the flag that
        # decides the status).
        assert resp.json()["detail"] == tts_client.INACTIVE_DETAIL

    def test_inactive_outcome_503s_whatever_sentence_it_carries(
        self, client, monkeypatch, tmp_path
    ):
        # Issue #171: this branch used to be `result.detail == "TTS is not
        # active (disabled or no base_url)"` — control flow by prose, with
        # the literal hand-copied out of tts_client. Re-word that sentence
        # there and the endpoint silently degraded 503 to 502, every test in
        # the suite still green. The router now reads SynthesisOutcome.INACTIVE,
        # so the wording is presentation and the status is the outcome's.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path)]))
        _active_tts_settings(monkeypatch)

        async def fake_synthesize(**kwargs):
            return SynthesisResult(
                outcome=SynthesisOutcome.INACTIVE, detail="TTS feature is not active"
            )

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 503
        # The user still reads the client's own sentence, verbatim.
        assert resp.json()["detail"] == "TTS feature is not active"

    def test_inactive_sentence_without_the_inactive_outcome_is_not_a_503(
        self, client, monkeypatch, tmp_path
    ):
        # The mirror of the test above, and the reason both must exist: a
        # LOCAL_FAILURE that happens to carry the old literal is a 502 like any
        # other failure this app cannot classify. If someone keeps the string
        # comparison next to the outcome table, this is the one that fails.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path)]))
        _active_tts_settings(monkeypatch)

        async def fake_synthesize(**kwargs):
            return SynthesisResult(
                outcome=SynthesisOutcome.LOCAL_FAILURE,
                detail="TTS is not active (disabled or no base_url)",
            )

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 502

    # -- the classification itself: every outcome has exactly one answer ----

    def test_every_synthesis_outcome_is_classified_by_the_router(self):
        # The seam issue #171 was really about: synthesize() has several
        # distinct exits, and a router that answers them with `else: 502`
        # teaches nothing to whoever adds the next one. The router therefore
        # keeps ONE table (_FAILURE_HTTP_STATUS) plus the engine-status rule,
        # and it subscripts the table instead of .get(..., 502) — an outcome
        # with no row raises KeyError instead of quietly becoming retryable.
        # This test walks the enum so that crash never happens in a browser.
        # GIVEN the table the router answers from, and the two outcomes it
        # decides somewhere else (AUDIO returns the body; ENGINE_VERDICT is
        # classified by the engine's own number):
        classified = set(tts_router._FAILURE_HTTP_STATUS)
        decided_elsewhere = {SynthesisOutcome.AUDIO, SynthesisOutcome.ENGINE_VERDICT}

        # THEN every outcome the client can report has exactly one home. A new
        # SynthesisOutcome with no row fails here, naming the table to fix.
        assert set(SynthesisOutcome) == classified | decided_elsewhere, (
            "every SynthesisOutcome needs a row in "
            "app.routers.tts._FAILURE_HTTP_STATUS or an explicit pass to the "
            "engine-status rule"
        )
        # ...and no row is wasted on an outcome the table never gets to answer
        # (a dead row reads like a decision).
        assert classified.isdisjoint(decided_elsewhere)

    def test_unclassified_outcome_would_raise_instead_of_defaulting_to_502(
        self, client, monkeypatch, tmp_path
    ):
        # The loud half of the previous test: the table is consulted by
        # subscript, so an outcome with no row is an error, not a status code.
        # Enum members cannot be added from a test, so the seam is probed by
        # deleting a row for one call (a stand-in for a future member that
        # forgot its row) and asserting the endpoint cannot answer.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path)]))
        _active_tts_settings(monkeypatch)

        async def inactive_synthesize(**kwargs):
            return SynthesisResult(outcome=SynthesisOutcome.INACTIVE, detail="off")

        monkeypatch.setattr(tts_router, "synthesize", inactive_synthesize)
        monkeypatch.delitem(tts_router._FAILURE_HTTP_STATUS, SynthesisOutcome.INACTIVE)

        with pytest.raises(KeyError):
            client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

    # -- the transcript requirement is the ENGINE's, not the persona's (#164) --

    def test_persona_without_transcript_synthesizes_when_reference_text_is_optional(
        self, client, monkeypatch, tmp_path
    ):
        # dots.tts advertises reference_text with required=False. A persona
        # with a clip and no transcript used to be refused outright — the bug
        # this test exists to keep dead.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)
        self._cache_doc(monkeypatch, make_capabilities_doc(engine="dots.tts"))
        seen = {}

        async def fake_synthesize(text, reference_text, audio_base64, language, persona_name):
            seen.update(reference_text=reference_text, persona_name=persona_name)
            return make_synthesis_audio()

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert seen["reference_text"] is None  # nothing invented, nothing sent

    def test_persona_without_transcript_synthesizes_when_engine_offers_no_reference_text_field(
        self, client, monkeypatch, tmp_path
    ):
        # chatterbox (and luxTTS / indexTTS / pocketTTS) has no reference_text
        # field at all: sending one would 422, and needing one is impossible.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)
        self._cache_doc(monkeypatch, make_capabilities_doc(engine="chatterbox"))
        seen = {}

        async def fake_synthesize(text, reference_text, audio_base64, language, persona_name):
            seen["reference_text"] = reference_text
            return make_synthesis_audio()

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert seen["reference_text"] is None

    def test_engine_requiring_reference_text_422s_before_synthesis_is_attempted(
        self, client, monkeypatch, tmp_path
    ):
        doc = self._engine_requiring_transcript()
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)
        self._cache_doc(monkeypatch, doc)

        async def failing_synthesize(**kwargs):
            raise AssertionError(
                "synthesize must not run: a missing required field is a FastAPI "
                "validation 422, which the stale-document self-heal would retry "
                "once per sentence of the reply"
            )

        monkeypatch.setattr(tts_router, "synthesize", failing_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 422  # the client's permanent-verdict signal
        detail = resp.json()["detail"]
        # The sentence is user-visible under the reply: it must name the
        # persona, the engine, and the fix.
        assert "Luna" in detail
        assert "breeze-blue" in detail
        assert "transcript" in detail

    def test_required_transcript_verdict_is_logged_once_per_persona_and_engine(
        self, client, monkeypatch, tmp_path, caplog
    ):
        # Streaming asks for one sentence per request; the log line is about
        # the persona's configuration, so it fires once per (engine, persona).
        import logging

        doc = self._engine_requiring_transcript()
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)
        self._cache_doc(monkeypatch, doc)

        with caplog.at_level(logging.WARNING, logger="app.services.tts_client"):
            for _ in range(3):
                assert client.post(
                    "/api/tts", json={"text": "hi", "persona_name": "Luna"}
                ).status_code == 422

        assert sum("No TTS for persona 'Luna'" in r.message for r in caplog.records) == 1

    def test_cold_capabilities_cache_never_refuses_for_a_missing_transcript(
        self, client, monkeypatch, tmp_path
    ):
        # No document = no EVIDENCE of a requirement, and "no evidence" must
        # never out-rule the engine (the same polarity rule as the non-cloning
        # check): the request proceeds and the server decides.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)
        called = {}

        async def fake_synthesize(text, reference_text, audio_base64, language, persona_name):
            called["yes"] = True
            return make_synthesis_audio()

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert called.get("yes")

    def test_required_transcript_doc_for_a_different_base_url_does_not_refuse(
        self, client, monkeypatch, tmp_path
    ):
        # A cached doc describes only the engine it was fetched for. After an
        # engine switch the slot may still hold the OLD engine's rules — and
        # the old engine happened to demand a transcript the new one doesn't.
        doc = self._engine_requiring_transcript()
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)  # base_url http://tts.local:5500
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://old.engine:9999")
        monkeypatch.setattr(tts_client, "_capabilities_cache", doc)
        called = {}

        async def fake_synthesize(text, reference_text, audio_base64, language, persona_name):
            called["yes"] = True
            return make_synthesis_audio()

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert called.get("yes")

    def test_synthesis_failure_502(self, client, monkeypatch, tmp_path):
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))

        async def failing_synthesize(**kwargs):
            return make_engine_verdict(500, "CUDA out of memory")

        monkeypatch.setattr(tts_router, "synthesize", failing_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})
        assert resp.status_code == 502
        # The engine's own words survive the proxy: the app's client (and the
        # log) get "CUDA out of memory", not a bare "returned no audio".
        assert "CUDA out of memory" in resp.json()["detail"]

    def test_engine_content_rejection_422_propagates_the_detail(self, client, monkeypatch, tmp_path):
        # LuxTTS (and VoxCPM / PocketTTS / Breeze) answer 422 for text the
        # engine cannot voice, with a detail naming the engine's script
        # coverage. It is the caller's to fix, so it keeps 422 and its words.
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))

        engine_detail = (
            "text contains nothing this engine can speak: LuxTTS keeps "
            "only ASCII letters, Chinese characters and <pinyin>/[tag] "
            "parts, and drops punctuation, digits, emoji and every "
            "other script (Cyrillic, Greek, Japanese, Korean, Arabic, "
            "...).  Send at least one English or Chinese word."
        )

        async def refusing_synthesize(**kwargs):
            return make_engine_verdict(422, engine_detail)

        monkeypatch.setattr(tts_router, "synthesize", refusing_synthesize)

        resp = client.post("/api/tts", json={"text": "...", "persona_name": "Luna"})

        assert resp.status_code == 422
        # The body is the engine's sentence VERBATIM: the status is the HTTP
        # status asserted above, so the line the browser prints under the
        # reply reads as prose, not as a log record.
        assert resp.json()["detail"] == engine_detail

    def test_engine_reference_clip_rejection_propagates_the_prompt_duration_advice(
        self, client, monkeypatch, tmp_path
    ):
        # The 400 is about the PERSONA's reference clip (no speech inside the
        # transcribable window). It is a permanent verdict about app-side
        # configuration — every sentence of every reply for this persona fails
        # identically — so it reaches the client as an app 422: the status the
        # frontend reads as "show the reason, stop asking for this reply".
        # Mapping it to 502 hid the engine's advice in a console and let the
        # client re-pay a near-full synthesis slot per sentence forever.
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))

        async def refusing_synthesize(**kwargs):
            return make_engine_verdict(
                400,
                (
                    "reference audio contains no recognizable speech in its "
                    "first 5 s: the engine transcribes only that window with "
                    "Whisper and conditions on the transcript, so silence, "
                    "music, or speech other than English / Chinese cannot be "
                    "cloned.  Start the clip with speech, or raise "
                    "prompt_duration."
                ),
            )

        monkeypatch.setattr(tts_router, "synthesize", refusing_synthesize)

        resp = client.post("/api/tts", json={"text": "hello there", "persona_name": "Luna"})

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "no recognizable speech" in detail
        assert "prompt_duration" in detail

    def test_success_forwards_reference_data_and_returns_audio(self, client, monkeypatch, tmp_path):
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))
        seen = {}

        async def fake_synthesize(text, reference_text, audio_base64, language, persona_name):
            seen.update(text=text, reference_text=reference_text,
                        audio_base64=audio_base64, language=language,
                        persona_name=persona_name)
            return make_synthesis_audio()

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hello", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert resp.json() == {"audio_base64": "QUJD", "sample_rate": 24000}
        assert seen["text"] == "hello"
        assert seen["reference_text"] == "a reference transcript"
        assert seen["audio_base64"] == base64.b64encode(b"RIFF-ref").decode()
        assert seen["language"] == "en"
        # The persona's name reaches synthesize() so the transcript pre-flight
        # can name the persona in its refusal. It is never sent to the engine
        # (build_synthesis_payload has no idea it exists).
        assert seen["persona_name"] == "Luna"

    def test_non_cloning_engine_503_without_calling_synthesize(self, client, monkeypatch, tmp_path):
        # The cached doc (for the current base_url) says reference_audio:
        # null — a non-cloning engine. Persona TTS is fundamentally
        # reference-audio-based, so the router refuses before spending a
        # request on a server that cannot do the job.
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))
        _active_tts_settings(monkeypatch)
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://tts.local:5500")
        monkeypatch.setattr(tts_client, "_capabilities_cache",
                            make_capabilities_doc(engine="omnivoice", reference_audio=None))

        async def failing_synthesize(**kwargs):
            raise AssertionError("synthesize must not be called for a non-cloning engine")

        monkeypatch.setattr(tts_router, "synthesize", failing_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 503
        assert "cloning" in resp.json()["detail"]

    def test_cached_doc_for_other_base_url_does_not_block_synthesis(self, client, monkeypatch, tmp_path):
        # A cached doc belonging to a DIFFERENT base_url says nothing about
        # the current server: the non-cloning check must not fire, and
        # synthesis proceeds (the server decides).
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))
        _active_tts_settings(monkeypatch)  # base_url http://tts.local:5500
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://other.server:9999")
        monkeypatch.setattr(tts_client, "_capabilities_cache",
                            make_capabilities_doc(engine="omnivoice", reference_audio=None))
        seen = {}

        async def fake_synthesize(text, reference_text, audio_base64, language,
                                  persona_name=""):
            seen["called"] = True
            return make_synthesis_audio()

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert seen.get("called")

    def test_cold_cache_transcript_refusal_from_synthesize_reaches_the_caller_as_422(
        self, client, monkeypatch, tmp_path
    ):
        # The router's own transcript pre-check can only read a WARM doc, so in
        # the cold-slot window the refusal happens inside synthesize() and lands
        # here as a SynthesisOutcome.LOCAL_REFUSAL — the one row of the outcome
        # table the warm path above never reaches. It must answer the same app
        # 422 (the frontend's "show the reason, stop asking for this reply"),
        # never a 502, or the reply's remaining sentences each re-pay the fetch.
        # The REAL synthesize runs; only httpx is faked.
        _persona_cache(monkeypatch, PersonasConfig(
            personas=[self._tts_capable_persona(tmp_path, transcript=False)]))
        _active_tts_settings(monkeypatch)
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            if url.endswith("/capabilities"):
                return json_response(200, self._engine_requiring_transcript())
            raise AssertionError(
                "no /synthesize request may be spent on a synthesis the "
                "document already condemned"
            )

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        # The /capabilities fetch is the proof this went through synthesize():
        # the ROUTER's refusal reads the cache only and never fetches, so a
        # passing test with no recorded call would mean the warm path answered
        # and the row under test was never exercised.
        assert calls == ["http://tts.local:5500/capabilities"]
        assert resp.status_code == 422
        assert "Luna" in resp.json()["detail"]

    def test_cold_cache_non_cloning_engine_502_without_synthesis_call(self, client, monkeypatch, tmp_path):
        # The backstop for the cache-cold window: the router's 503 check
        # can only consult a WARM cache, so the first /api/tts call after a
        # cache invalidation (TTS down at startup, settings save, ...)
        # fetches the doc inside synthesize() and must refuse THERE —
        # before the non-cloning engine answers in its default voice.
        # The REAL synthesize runs (only httpx is faked); the doc fetch
        # warms the cache, so the NEXT call gets the router's 503.
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))
        _active_tts_settings(monkeypatch)
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            if url.endswith("/capabilities"):
                return json_response(
                    200, make_capabilities_doc(engine="omnivoice", reference_audio=None))
            return json_response(200, {"audio_base64": "QUJD", "sample_rate": 24000})

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 502
        # The doc was fetched, but no /synthesize request was spent on an
        # engine that cannot do the job:
        assert calls == ["http://tts.local:5500/capabilities"]

        # Second call: the cache is now warm, so the ROUTER refuses —
        # no further network activity of any kind.
        calls.clear()
        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 503
        assert "cloning" in resp.json()["detail"]
        assert calls == []


# ---------------------------------------------------------------------------
# TTS capabilities endpoint (plan T5)
# ---------------------------------------------------------------------------

class TestTTSCapabilities:
    def test_inactive_503_without_network(self, client, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(fail))

        resp = client.get("/api/tts/capabilities")

        assert resp.status_code == 503
        assert "not active" in resp.json()["detail"]

    def test_active_serves_warm_cache_without_refetching(self, client, monkeypatch):
        _active_tts_settings(monkeypatch)
        doc = make_capabilities_doc(engine="omnivoice")
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://tts.local:5500")
        monkeypatch.setattr(tts_client, "_capabilities_cache", doc)
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(200, {})

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        resp = client.get("/api/tts/capabilities")

        # 200 + the raw document (no wrapper), served from the warm
        # cache: zero network calls.
        assert resp.status_code == 200
        assert resp.json() == doc
        assert calls == []

    def test_active_fetches_when_cache_is_cold(self, client, monkeypatch):
        _active_tts_settings(monkeypatch)
        doc = make_capabilities_doc(engine="dots.tts")
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(200, doc)

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        resp = client.get("/api/tts/capabilities")

        assert resp.status_code == 200
        assert resp.json() == doc
        assert calls == ["http://tts.local:5500/capabilities"]

    def test_active_but_unreachable_503(self, client, monkeypatch):
        _active_tts_settings(monkeypatch)

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(refuse))

        resp = client.get("/api/tts/capabilities")

        assert resp.status_code == 503
        assert "capabilities" in resp.json()["detail"]

    # -- ?base_url= probe (the Servers-modal reconnect button) -------------

    def test_probe_schemeless_url_422_without_network(self, client, monkeypatch):
        # A scheme-less typo must fail fast with 422 so the user can fix the
        # field in place — not hang until the client's connect timeout.
        def fail(*a, **kw):
            raise AssertionError("a scheme-less probe url must not touch the network")

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(fail))

        resp = client.get("/api/tts/capabilities", params={"base_url": "localhost:9000"})

        assert resp.status_code == 422
        assert "http://" in resp.json()["detail"]

    def test_probe_blank_url_422(self, client, monkeypatch):
        resp = client.get("/api/tts/capabilities", params={"base_url": "   "})
        assert resp.status_code == 422

    def test_probe_returns_doc_bypassing_inactive_settings_and_cache(self, client, monkeypatch):
        # GIVEN TTS is INACTIVE (no settings patch: default config) and the
        # cache slot is warm for a DIFFERENT url:
        probe_doc = make_capabilities_doc(engine="dots.tts")
        monkeypatch.setattr(tts_client, "_capabilities_base_url", "http://saved.local:5500")
        monkeypatch.setattr(tts_client, "_capabilities_cache", {"engine": "saved"})
        calls = []

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(200, probe_doc)

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        # WHEN the user probes a new (unsaved) url — trailing slash included,
        # as browsers/users tend to type:
        resp = client.get(
            "/api/tts/capabilities", params={"base_url": "http://new.local:9000/"}
        )

        # THEN the probed server's document comes back raw: the is_active
        # gate is bypassed (the saved config may point elsewhere), the
        # trailing slash is stripped before the URL is built, and the cache
        # slot is left untouched — a one-shot probe must not re-key the
        # slot the synthesis path relies on:
        assert resp.status_code == 200
        assert resp.json() == probe_doc
        assert calls == ["http://new.local:9000/capabilities"]
        assert tts_client.cached_capabilities() == ("http://saved.local:5500", {"engine": "saved"})

    def test_probe_unreachable_503(self, client, monkeypatch):
        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(refuse))

        resp = client.get(
            "/api/tts/capabilities", params={"base_url": "http://dead.local:9000"}
        )

        assert resp.status_code == 503
        assert "capabilities" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# STT health
# ---------------------------------------------------------------------------

class TestSTTHealth:
    def test_inactive_reports_disabled(self, client):
        resp = client.get("/api/stt/health")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "available": False}

    def test_active_and_reachable(self, client, monkeypatch):
        _active_stt_settings(monkeypatch)
        monkeypatch.setattr(stt_router, "check_stt_health", lambda: _coro(True))

        assert client.get("/api/stt/health").json() == {"enabled": True, "available": True}

    def test_active_but_unreachable(self, client, monkeypatch):
        _active_stt_settings(monkeypatch)
        monkeypatch.setattr(stt_router, "check_stt_health", lambda: _coro(False))

        assert client.get("/api/stt/health").json() == {"enabled": True, "available": False}


# ---------------------------------------------------------------------------
# STT proxy
# ---------------------------------------------------------------------------

class TestSTTProxy:
    def test_inactive_503(self, client):
        resp = client.post("/api/stt", json={"audio_base64": base64.b64encode(b"xx").decode()})
        assert resp.status_code == 503

    def test_invalid_base64_400(self, client, monkeypatch):
        _active_stt_settings(monkeypatch)
        resp = client.post("/api/stt", json={"audio_base64": "!!!not-base64!!!"})
        assert resp.status_code == 400

    def test_transcription_failure_502(self, client, monkeypatch):
        _active_stt_settings(monkeypatch)

        async def failing_transcribe(audio_bytes, mime_type="audio/webm"):
            return None

        monkeypatch.setattr(stt_router, "transcribe_audio", failing_transcribe)

        resp = client.post("/api/stt", json={"audio_base64": base64.b64encode(b"xx").decode()})
        assert resp.status_code == 502

    def test_success_returns_transcript_and_forwards_mime(self, client, monkeypatch):
        _active_stt_settings(monkeypatch)
        seen = {}

        async def fake_transcribe(audio_bytes, mime_type="audio/webm"):
            seen["audio_bytes"] = audio_bytes
            seen["mime_type"] = mime_type
            return {"text": "hello world", "language": "en",
                    "language_probability": 0.9}

        monkeypatch.setattr(stt_router, "transcribe_audio", fake_transcribe)

        resp = client.post("/api/stt", json={
            "audio_base64": base64.b64encode(b"raw-audio").decode(),
            "audio_mime_type": "audio/ogg",
        })

        assert resp.status_code == 200
        assert resp.json() == {"text": "hello world", "language": "en",
                               "language_probability": 0.9}
        assert seen["audio_bytes"] == b"raw-audio"
        assert seen["mime_type"] == "audio/ogg"


async def _coro(value):
    return value
