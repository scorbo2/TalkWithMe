"""API tests for app/routers/tts.py and app/routers/stt.py — the proxy endpoints.

The service clients (synthesize / transcribe_audio / health checks) are
monkeypatched at the router's import site; the reference-file handling is
exercised for real against tmp_path files.
"""

import base64

import httpx

import app.config as app_config
import app.routers.stt as stt_router
import app.routers.tts as tts_router
import app.services.tts_client as tts_client
from app.config import Persona, PersonasConfig
from tests.factories import (
    FakeAsyncClient,
    json_response,
    make_capabilities_doc,
    make_personas,
    make_settings,
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
    def test_inactive_reports_disabled_without_network(self, client):
        resp = client.get("/api/tts/health")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "available": False,
                               "streaming": False, "server_type": None}

    def test_active_reports_health_result(self, client, monkeypatch):
        _active_tts_settings(monkeypatch, streaming=True)
        monkeypatch.setattr(tts_router, "check_tts_health",
                            lambda: _coro((True, "dots.tts")))

        resp = client.get("/api/tts/health")

        assert resp.json() == {"enabled": True, "available": True,
                               "streaming": True, "server_type": "dots.tts"}

    def test_active_but_unreachable(self, client, monkeypatch):
        _active_tts_settings(monkeypatch)
        monkeypatch.setattr(tts_router, "check_tts_health",
                            lambda: _coro((False, None)))

        resp = client.get("/api/tts/health")

        assert resp.json()["enabled"] is True
        assert resp.json()["available"] is False


# ---------------------------------------------------------------------------
# TTS proxy
# ---------------------------------------------------------------------------

class TestTTSProxy:
    def _tts_capable_persona(self, tmp_path):
        """A persona whose reference files really exist under tmp_path."""
        wav = tmp_path / "luna.wav"
        wav.write_bytes(b"RIFF-ref")
        txt = tmp_path / "luna.txt"
        txt.write_text("a reference transcript", encoding="utf-8")
        return Persona(
            name="Luna",
            system_prompt="You are Luna.",
            router_hints="philosophy",
            reference_audio=str(wav),
            reference_audio_transcript=str(txt),
            reference_audio_language="en",
        )

    def test_unknown_persona_404(self, client):
        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "NoSuchOne"})
        assert resp.status_code == 404

    def test_persona_without_tts_400(self, client):
        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Alex"})
        assert resp.status_code == 400
        assert "TTS configured" in resp.json()["detail"]

    def test_missing_reference_files_503(self, client, monkeypatch):
        personas = make_personas()
        personas.personas[1].reference_audio = "/nonexistent/luna.wav"
        _persona_cache(monkeypatch, personas)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})
        assert resp.status_code == 503

    def test_synthesis_failure_502(self, client, monkeypatch, tmp_path):
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))

        async def failing_synthesize(**kwargs):
            return None

        monkeypatch.setattr(tts_router, "synthesize", failing_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})
        assert resp.status_code == 502

    def test_success_forwards_reference_data_and_returns_audio(self, client, monkeypatch, tmp_path):
        _persona_cache(monkeypatch, PersonasConfig(personas=[self._tts_capable_persona(tmp_path)]))
        seen = {}

        async def fake_synthesize(text, reference_text, audio_base64, language):
            seen.update(text=text, reference_text=reference_text,
                        audio_base64=audio_base64, language=language)
            return {"audio_base64": "QUJD", "sample_rate": 24000}

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hello", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert resp.json() == {"audio_base64": "QUJD", "sample_rate": 24000}
        assert seen["text"] == "hello"
        assert seen["reference_text"] == "a reference transcript"
        assert seen["audio_base64"] == base64.b64encode(b"RIFF-ref").decode()
        assert seen["language"] == "en"

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

        async def fake_synthesize(text, reference_text, audio_base64, language):
            seen["called"] = True
            return {"audio_base64": "QUJD", "sample_rate": 24000}

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hi", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert seen.get("called")

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
