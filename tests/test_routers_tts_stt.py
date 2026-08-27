"""API tests for app/routers/tts.py and app/routers/stt.py — the proxy endpoints.

The service clients (synthesize / transcribe_audio / health checks) are
monkeypatched at the router's import site; the reference-file handling is
exercised for real against tmp_path files.
"""

import base64

import app.config as app_config
import app.routers.stt as stt_router
import app.routers.tts as tts_router
from app.config import Persona, PersonasConfig
from tests.factories import make_personas, make_settings
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

        async def fake_synthesize(text, prompt_text, audio_base64, language):
            seen.update(text=text, prompt_text=prompt_text,
                        audio_base64=audio_base64, language=language)
            return {"audio_base64": "QUJD", "sample_rate": 24000}

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)

        resp = client.post("/api/tts", json={"text": "hello", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert resp.json() == {"audio_base64": "QUJD", "sample_rate": 24000}
        assert seen["text"] == "hello"
        assert seen["prompt_text"] == "a reference transcript"
        assert seen["audio_base64"] == base64.b64encode(b"RIFF-ref").decode()
        assert seen["language"] == "en"


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
