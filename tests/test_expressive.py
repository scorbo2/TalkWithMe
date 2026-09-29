"""Tests for expressive speech (general.expressive_speech).

Covers the pure markup helpers in app/services/expressive.py, the payload
override in tts_client (direction_parameter / build_synthesis_payload), the
/api/tts proxy (tags -> instruction, events kept or stripped per engine), and
the markup rules injected into the chat system prompt.
"""

import re
from pathlib import Path

import app.config as app_config
import app.routers.chat as chat_router
import app.routers.tts as tts_router
import app.services.tts_client as tts_client
from app.config import GeneralConfig, Persona, PersonasConfig, TTSConfig
from app.services import expressive
from tests.factories import make_capabilities_doc, make_settings, parse_sse_events

BASE_URL = "http://tts.local:5500"


def _directing_doc(name="instruction"):
    """A cloning engine that also takes a voice instruction (Breeze-like)."""
    doc = make_capabilities_doc(engine="omnivoice")
    doc["parameters"].append({"name": name, "type": "string", "default": None})
    return doc


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestSplitDirection:
    def test_leading_tag_becomes_the_direction(self):
        assert expressive.split_direction("{coldly, slowly} Well done.") == ("coldly, slowly", "Well done.")

    def test_no_tag(self):
        assert expressive.split_direction("Well done.") == (None, "Well done.")

    def test_first_non_blank_tag_wins_and_all_are_removed(self):
        direction, text = expressive.split_direction("{ } Well {angry} done {sad}.")
        assert direction == "angry"
        assert text == "Well done."

    def test_event_in_braces_is_an_event_not_a_direction(self):
        assert expressive.split_direction("{sigh} Fine. {coldly} Go.") == ("coldly", "(sigh) Fine. Go.")

    def test_unclosed_brace_is_left_alone(self):
        assert expressive.split_direction("Well {done.") == (None, "Well {done.")


class TestPrepareTtsText:
    def test_known_event_kept_when_allowed(self):
        text = "Well, that went (laugh) as expected."
        assert expressive.prepare_tts_text(text, events_allowed=True) == text

    def test_known_event_removed_when_not_allowed(self):
        text = "Well, that went (laugh) as expected."
        assert expressive.prepare_tts_text(text, events_allowed=False) == "Well, that went as expected."

    def test_event_matching_is_case_and_space_insensitive(self):
        assert expressive.prepare_tts_text("( Clears Throat ) Right.", True) == "(clears throat) Right."

    def test_unknown_parenthesis_is_unwrapped(self):
        text = "The test (number four) failed."
        assert expressive.prepare_tts_text(text, True) == "The test number four failed."
        assert expressive.prepare_tts_text(text, False) == "The test number four failed."

    def test_asterisk_action_becomes_event_or_plain_text(self):
        assert expressive.prepare_tts_text("*laughs* Fine.", True) == "(laughs) Fine."
        assert expressive.prepare_tts_text("*laughs* Fine.", False) == "Fine."
        assert expressive.prepare_tts_text("That is *very* bad.", True) == "That is very bad."

    def test_direction_tags_always_removed(self):
        assert expressive.prepare_tts_text("{coldly} Fine.", True) == "Fine."

    def test_event_before_punctuation_keeps_tidy_spacing(self):
        assert expressive.prepare_tts_text("Fine (sigh).", False) == "Fine."

    def test_event_in_braces_kept_or_removed(self):
        assert expressive.prepare_tts_text("Well {Clears Throat} fine.", True) == "Well (clears throat) fine."
        assert expressive.prepare_tts_text("Well {sigh} fine.", False) == "Well fine."

    def test_several_sounds_in_one_bracket_are_never_read_aloud(self):
        text = "Right (cough, nervous laugh) of course."
        assert expressive.prepare_tts_text(text, True) == "Right (cough) (laugh) of course."
        assert expressive.prepare_tts_text(text, False) == "Right of course."

    def test_inflected_and_joined_sounds(self):
        assert expressive.prepare_tts_text("(sighing and groaning) Fine.", True) == "(sigh) (groan) Fine."
        assert expressive.prepare_tts_text("(chuckles softly) Fine.", True) == "(chuckle) Fine."
        assert expressive.prepare_tts_text("(giggling) Fine.", True) == "(giggle) Fine."

    def test_sounds_in_braces_become_events_only_when_all_parts_are_sounds(self):
        assert expressive.split_direction("{cough, sigh} Fine.") == (None, "(cough) (sigh) Fine.")
        assert expressive.split_direction("{pause, then coldly} Fine.") == ("pause, then coldly", "Fine.")
        assert expressive.split_direction("{laughing nervously} Fine.") == ("laughing nervously", "Fine.")

    def test_event_only_sentence(self):
        assert expressive.prepare_tts_text("(laugh)", True) == "(laugh)"
        assert expressive.prepare_tts_text("(laugh)", False) == ""


class TestVocabularyDrift:
    def test_frontend_list_matches_the_backend(self):
        # static/utils.js carries its own copy (the frontend must tell an
        # event in braces from a direction without a round trip).
        utils = (Path(__file__).parent.parent / "static" / "utils.js").read_text(encoding="utf-8")
        block = re.search(r"const VOCAL_EVENTS = \[(.*?)\];", utils, re.S).group(1)
        assert tuple(re.findall(r'"([^"]+)"', block)) == expressive.VOCAL_EVENTS


class TestDeliveryPrompt:
    def test_lists_every_event_and_the_direction_syntax(self):
        prompt = expressive.delivery_prompt()
        for event in expressive.VOCAL_EVENTS:
            assert f"({event})" in prompt
        assert "{coldly, slowly}" in prompt


# ---------------------------------------------------------------------------
# tts_client: which parameter carries the direction
# ---------------------------------------------------------------------------

class TestDirectionParameter:
    def test_breeze_style_instruction(self):
        assert tts_client.direction_parameter(_directing_doc("instruction")) == "instruction"

    def test_voice_design_proposal_instructions(self):
        assert tts_client.direction_parameter(_directing_doc("instructions")) == "instructions"

    def test_engine_without_instruction(self):
        assert tts_client.direction_parameter(make_capabilities_doc(engine="omnivoice")) is None

    def test_non_cloning_engine_cannot_direct(self):
        doc = _directing_doc()
        doc["reference_audio"] = None
        assert tts_client.direction_parameter(doc) is None

    def test_no_doc(self):
        assert tts_client.direction_parameter(None) is None


class TestPayloadInstruction:
    def _payload(self, doc, configured, instruction):
        return tts_client.build_synthesis_payload(
            doc, "Hi.", "ref text", "QUJD", "en", configured, instruction)

    def test_overrides_the_configured_instruction(self):
        payload = self._payload(_directing_doc(), {"instruction": "calm", "seed": 42}, "angry")
        assert payload["instruction"] == "angry"
        assert payload["seed"] == 42

    def test_configured_instruction_used_without_override(self):
        payload = self._payload(_directing_doc(), {"instruction": "calm"}, None)
        assert payload["instruction"] == "calm"

    def test_sent_under_the_advertised_name(self):
        payload = self._payload(_directing_doc("instructions"), {}, "angry")
        assert payload["instructions"] == "angry"
        assert "instruction" not in payload

    def test_dropped_for_an_engine_without_instruction(self):
        payload = self._payload(make_capabilities_doc(engine="omnivoice"), {}, "angry")
        assert "instruction" not in payload and "instructions" not in payload

    def test_dropped_without_a_doc(self):
        payload = self._payload(None, {}, "angry")
        assert "instruction" not in payload and "instructions" not in payload


# ---------------------------------------------------------------------------
# /api/tts proxy
# ---------------------------------------------------------------------------

class TestTTSProxyExpressive:
    def _setup(self, monkeypatch, tmp_path, *, expressive_on=True, doc=None):
        wav = tmp_path / "luna.wav"
        wav.write_bytes(b"RIFF-ref")
        txt = tmp_path / "luna.txt"
        txt.write_text("a reference transcript", encoding="utf-8")
        monkeypatch.setattr(app_config, "_personas_cache", PersonasConfig(personas=[Persona(
            name="Luna", system_prompt="You are Luna.", router_hints="philosophy",
            reference_audio=str(wav), reference_audio_transcript=str(txt),
            reference_audio_language="en",
        )]))
        settings = make_settings(tts=TTSConfig(enabled=True, base_url=BASE_URL))
        settings.general = GeneralConfig(expressive_speech=expressive_on)
        monkeypatch.setattr(app_config, "_settings_cache", settings)
        monkeypatch.setattr(tts_client, "_capabilities_base_url", BASE_URL)
        monkeypatch.setattr(tts_client, "_capabilities_cache", doc)
        seen = {}

        async def fake_synthesize(text, reference_text, audio_base64, language, **kwargs):
            seen.update(text=text, kwargs=kwargs)
            return {"audio_base64": "QUJD", "sample_rate": 24000}

        monkeypatch.setattr(tts_router, "synthesize", fake_synthesize)
        return seen

    def test_tag_becomes_instruction_and_events_stay(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=_directing_doc())

        resp = client.post("/api/tts", json={
            "text": "{coldly} Well (laugh), that went well.", "persona_name": "Luna"})

        assert resp.status_code == 200
        assert seen["text"] == "Well (laugh), that went well."
        assert seen["kwargs"] == {"instruction": "coldly"}

    def test_carried_instruction_from_the_frontend_wins(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=_directing_doc())

        client.post("/api/tts", json={
            "text": "Still going.", "persona_name": "Luna", "instruction": "whispering"})

        assert seen["kwargs"] == {"instruction": "whispering"}

    def test_no_direction_leaves_the_call_unchanged(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=_directing_doc())

        client.post("/api/tts", json={"text": "Plain.", "persona_name": "Luna"})

        assert seen["kwargs"] == {}

    def test_engine_without_direction_strips_the_markup(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=make_capabilities_doc(engine="omnivoice"))

        client.post("/api/tts", json={
            "text": "{coldly} Well (laugh), that went well.", "persona_name": "Luna",
            "instruction": "whispering"})

        assert seen["text"] == "Well, that went well."
        assert seen["kwargs"] == {}

    def test_cold_cache_strips_the_markup(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=None)

        client.post("/api/tts", json={"text": "{coldly} Hi (sigh).", "persona_name": "Luna"})

        assert seen["text"] == "Hi."
        assert seen["kwargs"] == {}

    def test_feature_off_sends_text_untouched(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, expressive_on=False, doc=_directing_doc())

        client.post("/api/tts", json={
            "text": "{coldly} Hi (sigh).", "persona_name": "Luna", "instruction": "angry"})

        assert seen["text"] == "{coldly} Hi (sigh)."
        assert seen["kwargs"] == {}

    def test_nothing_left_to_speak_is_400(self, client, monkeypatch, tmp_path):
        seen = self._setup(monkeypatch, tmp_path, doc=make_capabilities_doc(engine="omnivoice"))

        resp = client.post("/api/tts", json={"text": "{coldly} (laugh)", "persona_name": "Luna"})

        assert resp.status_code == 400
        assert seen == {}


# ---------------------------------------------------------------------------
# Chat: markup rules in the system prompt
# ---------------------------------------------------------------------------

class TestDeliveryMarkupInChat:
    def _run(self, client, monkeypatch, *, expressive_on, doc, tts_active=True):
        settings = make_settings(tts=TTSConfig(enabled=tts_active, base_url=BASE_URL))
        settings.general = GeneralConfig(expressive_speech=expressive_on)
        monkeypatch.setattr(app_config, "_settings_cache", settings)

        async def fake_capabilities():
            return doc

        monkeypatch.setattr(chat_router, "get_capabilities", fake_capabilities)
        seen = []

        async def capturing_stream(messages):
            seen.append(messages[0]["content"])
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)
        resp = client.post("/api/chat", json={
            "message": "hello", "who_answers": "Alex", "chat_room": "default"})
        assert resp.status_code == 200
        parse_sse_events(resp.text)
        return seen[0]

    def test_rules_appended_last_when_the_engine_can_direct(self, client, monkeypatch):
        prompt = self._run(client, monkeypatch, expressive_on=True, doc=_directing_doc())

        assert prompt.endswith(expressive.delivery_prompt())

    def test_rules_follow_the_global_prompt(self, client, monkeypatch):
        settings = make_settings(tts=TTSConfig(enabled=True, base_url=BASE_URL))
        settings.general = GeneralConfig(expressive_speech=True, global_system_prompt="GLOBAL RULES")
        monkeypatch.setattr(app_config, "_settings_cache", settings)

        async def fake_capabilities():
            return _directing_doc()

        monkeypatch.setattr(chat_router, "get_capabilities", fake_capabilities)
        seen = []

        async def capturing_stream(messages):
            seen.append(messages[0]["content"])
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)
        client.post("/api/chat", json={"message": "hello", "who_answers": "Alex", "chat_room": "default"})

        assert seen[0].index("GLOBAL RULES") < seen[0].index("Delivery markup")

    def test_no_rules_when_the_engine_cannot_direct(self, client, monkeypatch):
        prompt = self._run(client, monkeypatch, expressive_on=True,
                           doc=make_capabilities_doc(engine="omnivoice"))

        assert "Delivery markup" not in prompt

    def test_no_rules_when_off(self, client, monkeypatch):
        prompt = self._run(client, monkeypatch, expressive_on=False, doc=_directing_doc())

        assert "Delivery markup" not in prompt

    def test_no_rules_when_tts_inactive(self, client, monkeypatch):
        prompt = self._run(client, monkeypatch, expressive_on=True, doc=_directing_doc(),
                           tts_active=False)

        assert "Delivery markup" not in prompt
