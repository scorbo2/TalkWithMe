"""Tests for app/services/tts_client.py and app/services/stt_client.py.

The httpx client is replaced with FakeAsyncClient; no network involved.
Both features are INACTIVE by default in the fixture config — tests that
need an active feature re-point the settings cache.
"""

import asyncio

import httpx
import pytest

import app.config as app_config
import app.services.stt_client as stt_client
import app.services.tts_client as tts_client
from tests.factories import FakeAsyncClient, json_response, make_settings
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
# TTS synthesis
# ---------------------------------------------------------------------------

class TestSynthesize:
    def test_inactive_tts_returns_none_without_network(self, monkeypatch):
        def fail(*a, **kw):
            raise AssertionError("network must not be touched when TTS is inactive")

        _patch_http(monkeypatch, fail)
        result = _run(tts_client.synthesize("hi", "prompt", "QUJD"))
        assert result is None

    def test_synthesize_posts_configured_payload_and_returns_body(self, monkeypatch):
        tts = _active_tts(monkeypatch, num_steps=7, guidance_scale=2.5, seed=42)
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
            "prompt_text": "prompt text",
            "audio_base64": "QUJD",
            "language": "de",
            "num_steps": 7,
            "guidance_scale": 2.5,
            "seed": 42,
        }

    def test_synthesize_http_error_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)
        _patch_http(monkeypatch, lambda method, url, **kw: json_response(500, {}))

        assert _run(tts_client.synthesize("hi", "p", "QUJD")) is None

    def test_synthesize_connection_error_returns_none(self, monkeypatch):
        _active_tts(monkeypatch)

        def refuse(*a, **kw):
            raise httpx.ConnectError("refused")

        _patch_http(monkeypatch, refuse)
        assert _run(tts_client.synthesize("hi", "p", "QUJD")) is None


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
