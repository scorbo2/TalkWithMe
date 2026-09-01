"""Tests for app/models.py — API request/response model validation."""

import pytest
from pydantic import ValidationError

from app.models import (
    AssignPersonasRequest,
    AudioUploadRequest,
    ChatRequest,
    ChatRoomCreateRequest,
    EchoChamberRequest,
    GeneralSettingsRequest,
    PersonaDetailResponse,
    PersonaResponse,
    SessionPersonasRequest,
    SettingsUpdateRequest,
    STTRequest,
    TTSRequest,
)


class TestChatRequest:
    def test_chat_request_defaults(self):
        req = ChatRequest(message="hello")
        assert req.who_answers == "router"
        assert req.chat_room == "default"
        assert req.message_id is None

    def test_chat_request_empty_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="")

    def test_chat_request_accepts_all_fields(self):
        req = ChatRequest(
            message="hi", who_answers="random", chat_room="TNG", message_id="abc-123"
        )
        assert req.message_id == "abc-123"


class TestSessionPersonasRequest:
    def test_session_personas_request_requires_at_least_one(self):
        with pytest.raises(ValidationError):
            SessionPersonasRequest(active_personas=[])

    def test_session_personas_request_accepts_names(self):
        req = SessionPersonasRequest(active_personas=["Alex", "Luna"])
        assert req.active_personas == ["Alex", "Luna"]


class TestPersonaResponseModels:
    """Persona create/update have no request models (multipart Form/File
    params on the router); the response models are the API contract."""

    def test_persona_response_avatar_image_is_a_presence_flag(self):
        resp = PersonaResponse(
            name="A", description="d", avatar_color="#fff",
            avatar_image=True, tts_capable=False,
        )
        assert resp.avatar_image is True

    def test_persona_detail_response_defaults_for_absent_files(self):
        detail = PersonaDetailResponse(
            name="A", description="", system_prompt="p", router_hints="h",
            avatar_color="#fff", reference_audio_language="en",
        )
        assert detail.avatar_image is False
        assert detail.reference_audio is False
        assert detail.reference_audio_transcript is None
        assert detail.tts_capable is False


class TestTTSRequest:
    def test_tts_request_missing_required_fields_rejected(self):
        with pytest.raises(ValidationError):
            TTSRequest()  # both text and persona_name are required
        with pytest.raises(ValidationError):
            TTSRequest(text="hello")  # persona_name missing

    def test_tts_request_empty_text_rejected(self):
        with pytest.raises(ValidationError):
            TTSRequest(text="", persona_name="Luna")  # min_length=1

    def test_tts_request_valid(self):
        req = TTSRequest(text="hello", persona_name="Luna")
        assert req.persona_name == "Luna"


class TestSTTRequest:
    def test_stt_request_requires_audio(self):
        with pytest.raises(ValidationError):
            STTRequest(audio_base64="")

    def test_stt_request_default_mime_type_is_webm(self):
        req = STTRequest(audio_base64="QUJD")
        assert req.audio_mime_type == "audio/webm"


class TestChatRoomModels:
    def test_chat_room_create_request_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            ChatRoomCreateRequest(name="x" * 21)

    def test_assign_personas_request_requires_at_least_one(self):
        with pytest.raises(ValidationError):
            AssignPersonasRequest(persona_names=[])

    def test_echo_chamber_request_holds_flag(self):
        assert EchoChamberRequest(echo_chamber=True).echo_chamber is True


class TestAudioUploadRequest:
    def test_audio_upload_request_missing_required_fields_rejected(self):
        with pytest.raises(ValidationError):
            AudioUploadRequest()  # both message_id and audio_base64 are required
        with pytest.raises(ValidationError):
            AudioUploadRequest(message_id="m1")  # audio_base64 missing

    def test_audio_upload_request_mime_type_optional(self):
        req = AudioUploadRequest(message_id="m1", audio_base64="QUJD")
        assert req.mime_type is None


class TestGeneralSettingsRequestPartialUpdate:
    """The general section is a partial update: every field must be
    optional so omitted fields keep their current server-side values."""

    def test_general_settings_request_all_fields_omitted(self):
        req = GeneralSettingsRequest()
        assert req.model_dump() == {
            "persona_name_mentions": None,
            "max_persona_replies": None,
            "max_turns_for_context": None,
            "show_tool_calls": None,
            "enable_persona_memories": None,
        }

    def test_general_settings_request_exclude_none_drops_omitted_fields(self):
        req = GeneralSettingsRequest(show_tool_calls=False)
        dumped = req.model_dump(exclude_none=True)
        assert dumped == {"show_tool_calls": False}

    def test_general_settings_request_out_of_range_replies_rejected(self):
        with pytest.raises(ValidationError):
            GeneralSettingsRequest(max_persona_replies=5)

    def test_general_settings_request_out_of_range_context_rejected(self):
        with pytest.raises(ValidationError):
            GeneralSettingsRequest(max_turns_for_context=0)


class TestSettingsUpdateRequest:
    def test_settings_update_request_requires_llm_tts_stt(self):
        with pytest.raises(ValidationError):
            SettingsUpdateRequest(llm={"base_url": "http://x", "model": "m",
                                       "max_tokens": 1, "temperature": 0.5})

    def test_settings_update_request_general_defaults_to_empty_partial_update(self):
        req = SettingsUpdateRequest(
            llm={"base_url": "http://x", "model": "m", "max_tokens": 1, "temperature": 0.5},
            tts={"num_steps": 4, "guidance_scale": 1.0, "timeout": 5},
            stt={"timeout": 5},
        )
        # No general section sent: everything stays None -> nothing overrides.
        assert req.general.model_dump(exclude_none=True) == {}
