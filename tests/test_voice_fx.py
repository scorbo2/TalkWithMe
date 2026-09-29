"""Tests for per-persona voice effects (app/services/voice_fx.py).

ffmpeg itself is never run: conftest pins voice_fx._ffmpeg to "not
installed", tests that need it patch in a path plus a fake subprocess. The
filter chains are exercised for real on the deployment box (see
docs in the module), not here — pytest must run with nothing but Python.
"""

import asyncio
import base64
import logging

import pytest

import app.config as app_config
import app.routers.chat as chat_router
import app.routers.tts as tts_router
import app.services.tts_client as tts_client
import app.services.voice_fx as voice_fx
from app.config import GeneralConfig, Persona, PersonasConfig, TTSConfig
from app.services import expressive
from tests.factories import make_capabilities_doc, make_settings, parse_sse_events

BASE_URL = "http://tts.local:5500"


def _write_fx(persona_dir, text):
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / voice_fx.VOICE_FX_FILENAME).write_text(text, encoding="utf-8")


class _FixedRandom:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


# ---------------------------------------------------------------------------
# voice_fx.yaml
# ---------------------------------------------------------------------------

class TestLoadVoiceFx:
    def test_no_directory_or_no_file(self, tmp_path):
        assert voice_fx.load_voice_fx(None) is None
        assert voice_fx.load_voice_fx(tmp_path) is None

    def test_full_file(self, tmp_path):
        _write_fx(tmp_path, "effect: metallic_speaker\nglitch_chance: 0.1\ndistance: room\n")
        assert voice_fx.load_voice_fx(tmp_path) == voice_fx.VoiceFx("metallic_speaker", 0.1, "room")

    def test_unknown_values_degrade_to_neutral_with_warnings(self, tmp_path, caplog):
        _write_fx(tmp_path, "effect: tin_can\nglitch_chance: often\ndistance: mars\n")
        with caplog.at_level(logging.WARNING):
            assert voice_fx.load_voice_fx(tmp_path) is None  # nothing valid left
        assert "tin_can" in caplog.text and "often" in caplog.text and "mars" in caplog.text

    def test_chance_is_clamped(self, tmp_path):
        _write_fx(tmp_path, "effect: small_speaker\nglitch_chance: 7\n")
        assert voice_fx.load_voice_fx(tmp_path).glitch_chance == 1.0

    def test_malformed_yaml_or_non_mapping(self, tmp_path):
        _write_fx(tmp_path, "effect: [unclosed\n")
        assert voice_fx.load_voice_fx(tmp_path) is None
        _write_fx(tmp_path, "- just a list\n")
        assert voice_fx.load_voice_fx(tmp_path) is None


# ---------------------------------------------------------------------------
# Markup: (glitch) and distance phrases
# ---------------------------------------------------------------------------

class TestFxEvents:
    def test_glitch_tag_is_detected_and_removed(self):
        assert voice_fx.take_fx_events("Stop (glitch) right there.") == (True, "Stop right there.")
        assert voice_fx.take_fx_events("{Glitching} Stop.") == (True, "Stop.")

    def test_text_without_tag_is_returned_untouched(self):
        assert voice_fx.take_fx_events("Two  spaces (laugh).") == (False, "Two  spaces (laugh).")


class TestDistanceFromDirection:
    @pytest.mark.parametrize("direction, expected", [
        ("calling, from far away", "far"),
        ("shouting across the cellar", "far"),
        ("muffled, from inside a pocket", "muffled"),
        ("whispering right next to you", "close"),
        ("from the doorway, annoyed", "room"),
        ("coldly, slowly", None),
        (None, None),
    ])
    def test_phrases(self, direction, expected):
        assert voice_fx.distance_from_direction(direction) == expected


# ---------------------------------------------------------------------------
# Chain assembly
# ---------------------------------------------------------------------------

class TestBuildChain:
    FX = voice_fx.VoiceFx("metallic_speaker", 0.0, "near")

    def test_nothing_to_do(self):
        assert voice_fx.build_chain(None) is None
        assert voice_fx.build_chain(voice_fx.VoiceFx(distance="near")) is None

    def test_effect_then_level(self):
        chain = voice_fx.build_chain(self.FX)
        assert chain == voice_fx.PRESETS["metallic_speaker"] + "," + voice_fx._LEVEL

    def test_distance_comes_after_the_level(self):
        chain = voice_fx.build_chain(self.FX, direction="calling, from far away")
        assert chain.endswith(voice_fx._LEVEL + "," + voice_fx.DISTANCES["far"])

    def test_direction_distance_works_without_a_voice_fx_file(self):
        assert voice_fx.build_chain(None, direction="muffled") == (
            voice_fx._LEVEL + "," + voice_fx.DISTANCES["muffled"])

    def test_persona_default_distance(self):
        chain = voice_fx.build_chain(voice_fx.VoiceFx("small_speaker", 0.0, "room"))
        assert chain.endswith(voice_fx.DISTANCES["room"])

    def test_requested_glitch_sits_between_effect_and_level(self):
        chain = voice_fx.build_chain(self.FX, glitch_requested=True)
        assert chain == ",".join([voice_fx.PRESETS["metallic_speaker"], voice_fx.GLITCH_CHAIN, voice_fx._LEVEL])

    def test_random_glitch_follows_the_chance(self):
        fx = voice_fx.VoiceFx("metallic_speaker", 0.25, "near")
        assert voice_fx.GLITCH_CHAIN in voice_fx.build_chain(fx, rng=_FixedRandom(0.2))
        assert voice_fx.GLITCH_CHAIN not in voice_fx.build_chain(fx, rng=_FixedRandom(0.3))

    def test_no_glitch_without_a_speaker_effect(self):
        fx = voice_fx.VoiceFx("", 1.0, "far")
        assert voice_fx.GLITCH_CHAIN not in voice_fx.build_chain(fx, glitch_requested=True)

    def test_every_preset_and_distance_is_a_plain_filter_string(self):
        # No shell involved (exec, not a shell), but the chains must still be
        # single -af arguments: no quotes or whitespace-separated options.
        for chain in [*voice_fx.PRESETS.values(), *voice_fx.DISTANCES.values(), voice_fx.GLITCH_CHAIN]:
            assert " " not in chain and "'" not in chain and '"' not in chain


# ---------------------------------------------------------------------------
# ffmpeg runner
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode=0, out=b"RIFF-processed", err=b"", hang=False):
        self.returncode = None if hang else returncode
        self._final = returncode
        self._out, self._err, self._hang = out, err, hang
        self.killed = False
        self.stdin_bytes = None

    async def communicate(self, data):
        self.stdin_bytes = data
        if self._hang:
            await asyncio.sleep(10)
        self.returncode = self._final
        return self._out, self._err

    def kill(self):
        self.killed = True


def _fake_exec(monkeypatch, proc, seen):
    async def fake_create(*args, **kwargs):
        seen.append(args)
        return proc

    monkeypatch.setattr(voice_fx, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_fx.asyncio, "create_subprocess_exec", fake_create)


class TestApplyChain:
    SRC = base64.b64encode(b"RIFF-original").decode()

    def test_missing_ffmpeg_passes_through_and_warns_once(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert asyncio.run(voice_fx.apply_chain(self.SRC, "volume=1", 24000)) == self.SRC
            assert asyncio.run(voice_fx.apply_chain(self.SRC, "volume=1", 24000)) == self.SRC
        assert caplog.text.count("no ffmpeg") == 1

    def test_success_returns_the_processed_wav(self, monkeypatch):
        proc, seen = _FakeProc(), []
        _fake_exec(monkeypatch, proc, seen)

        out = asyncio.run(voice_fx.apply_chain(self.SRC, "volume=1", 44100))

        assert base64.b64decode(out) == b"RIFF-processed"
        assert proc.stdin_bytes == b"RIFF-original"
        args = seen[0]
        assert args[args.index("-af") + 1] == "volume=1"
        assert args[args.index("-ar") + 1] == "44100"

    def test_ffmpeg_error_passes_through(self, monkeypatch):
        _fake_exec(monkeypatch, _FakeProc(returncode=1, out=b"", err=b"bad filter"), [])
        assert asyncio.run(voice_fx.apply_chain(self.SRC, "nonsense", 24000)) == self.SRC

    def test_timeout_kills_and_passes_through(self, monkeypatch):
        proc = _FakeProc(hang=True)
        _fake_exec(monkeypatch, proc, [])
        assert asyncio.run(voice_fx.apply_chain(self.SRC, "volume=1", 24000, timeout=0.05)) == self.SRC
        assert proc.killed


# ---------------------------------------------------------------------------
# /api/tts proxy
# ---------------------------------------------------------------------------

def _directing_doc():
    doc = make_capabilities_doc(engine="omnivoice")
    doc["parameters"].append({"name": "instruction", "type": "string", "default": None})
    return doc


class TestProxyVoiceFx:
    def _setup(self, monkeypatch, tmp_path, *, fx_yaml=None, expressive_on=True, doc=None):
        persona_dir = tmp_path / "Robot"
        persona_dir.mkdir()
        (persona_dir / "ref.wav").write_bytes(b"RIFF-ref")
        (persona_dir / "ref.txt").write_text("a reference transcript", encoding="utf-8")
        if fx_yaml:
            _write_fx(persona_dir, fx_yaml)
        monkeypatch.setattr(app_config, "_personas_cache", PersonasConfig(personas=[Persona(
            name="Robot", system_prompt="You are Robot.",
            reference_audio=str(persona_dir / "ref.wav"),
            reference_audio_transcript=str(persona_dir / "ref.txt"),
            persona_dir=persona_dir,
        )]))
        settings = make_settings(tts=TTSConfig(enabled=True, base_url=BASE_URL))
        settings.general = GeneralConfig(expressive_speech=expressive_on)
        monkeypatch.setattr(app_config, "_settings_cache", settings)
        monkeypatch.setattr(tts_client, "_capabilities_base_url", BASE_URL)
        monkeypatch.setattr(tts_client, "_capabilities_cache", doc if doc is not None else _directing_doc())
        seen = {"chains": []}

        async def fake_synthesize(text, reference_text, audio_base64, language, **kwargs):
            seen.update(text=text, kwargs=kwargs)
            return {"audio_base64": "UkFX", "sample_rate": 24000}

        async def fake_apply(audio_base64, chain, sample_rate, timeout=20.0):
            seen["chains"].append(chain)
            return "RlhE"

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)
        monkeypatch.setattr(tts_router.voice_fx, "apply_chain", fake_apply)
        # Pin the glitch dice: never, unless a test says otherwise.
        monkeypatch.setattr(voice_fx.random, "random", lambda: 0.99)
        return seen

    def _post(self, client, text, **extra):
        return client.post("/api/tts", json={"text": text, "persona_name": "Robot", **extra})

    def test_persona_effect_is_applied(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, fx_yaml="effect: metallic_speaker\n")

        resp = self._post(client, "Hello.")

        assert resp.json()["audio_base64"] == "RlhE"
        assert seen["chains"] == [voice_fx.build_chain(voice_fx.VoiceFx("metallic_speaker"))]

    def test_glitch_tag_never_reaches_the_engine(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, fx_yaml="effect: metallic_speaker\n")

        self._post(client, "Stop (glitch) right there.")

        assert seen["text"] == "Stop right there."
        assert voice_fx.GLITCH_CHAIN in seen["chains"][0]

    def test_random_glitch(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, fx_yaml="effect: metallic_speaker\nglitch_chance: 0.5\n")
        monkeypatch.setattr(voice_fx.random, "random", lambda: 0.1)

        self._post(client, "Hello.")

        assert voice_fx.GLITCH_CHAIN in seen["chains"][0]

    def test_distance_from_the_carried_direction(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, fx_yaml="effect: metallic_speaker\n")

        self._post(client, "Still here.", instruction="calling, from far away")

        assert seen["chains"][0].endswith(voice_fx.DISTANCES["far"])
        assert seen["kwargs"] == {"instruction": "calling, from far away"}

    def test_distance_applies_even_when_the_engine_cannot_direct(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=make_capabilities_doc(engine="omnivoice"))

        self._post(client, "{muffled, from inside a pocket} Help.")

        assert seen["kwargs"] == {}
        assert seen["chains"] == [voice_fx._LEVEL + "," + voice_fx.DISTANCES["muffled"]]

    def test_no_file_and_no_distance_means_no_ffmpeg_run(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path)

        resp = self._post(client, "{coldly} Hello.")

        assert resp.json()["audio_base64"] == "UkFX"
        assert seen["chains"] == []

    def test_glitch_alone_is_nothing_to_speak(self, client, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path, fx_yaml="effect: metallic_speaker\n")

        assert self._post(client, "(glitch)").status_code == 400


# ---------------------------------------------------------------------------
# Chat: distance and glitch rules in the markup
# ---------------------------------------------------------------------------

class TestPromptNote:
    def _system_prompt(self, client, monkeypatch, tmp_path, *, fx_yaml=None, ffmpeg=True):
        persona_dir = tmp_path / "Robot"
        persona_dir.mkdir()
        if fx_yaml:
            _write_fx(persona_dir, fx_yaml)
        monkeypatch.setattr(app_config, "_personas_cache", PersonasConfig(personas=[Persona(
            name="Robot", system_prompt="You are Robot.", persona_dir=persona_dir)]))
        settings = make_settings(tts=TTSConfig(enabled=True, base_url=BASE_URL))
        settings.general = GeneralConfig(expressive_speech=True)
        monkeypatch.setattr(app_config, "_settings_cache", settings)
        if ffmpeg:
            monkeypatch.setattr(voice_fx, "_ffmpeg", lambda: "/usr/bin/ffmpeg")

        async def fake_capabilities():
            return _directing_doc()

        monkeypatch.setattr(chat_router, "get_capabilities", fake_capabilities)
        seen = []

        async def capturing_stream(messages):
            seen.append(messages[0]["content"])
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)
        resp = client.post("/api/chat", json={"message": "hi", "who_answers": "Robot", "chat_room": "default"})
        parse_sse_events(resp.text)
        return seen[0]

    def test_speaker_persona_learns_glitch_and_distance(self, client, monkeypatch, tmp_path):
        prompt = self._system_prompt(client, monkeypatch, tmp_path, fx_yaml="effect: metallic_speaker\n")
        assert "(glitch)" in prompt and "from far away" in prompt
        assert prompt.index(expressive.delivery_prompt()) < prompt.index("(glitch)")

    def test_plain_persona_learns_only_distance(self, client, monkeypatch, tmp_path):
        prompt = self._system_prompt(client, monkeypatch, tmp_path)
        assert "from far away" in prompt and "(glitch)" not in prompt

    def test_without_ffmpeg_neither(self, client, monkeypatch, tmp_path):
        prompt = self._system_prompt(client, monkeypatch, tmp_path,
                                     fx_yaml="effect: metallic_speaker\n", ffmpeg=False)
        assert "(glitch)" not in prompt and "from far away" not in prompt
        assert prompt.endswith(expressive.delivery_prompt())
