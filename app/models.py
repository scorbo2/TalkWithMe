"""Pydantic request / response models for the TalkWithMe API."""

from typing import List, Optional, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """User sends a message; optionally picks who answers."""
    message: str = Field(..., min_length=1, description="The user's message text")
    who_answers: str = Field(
        default="router",
        description='One of "router", "random", or a persona name',
    )


class SessionPersonasRequest(BaseModel):
    """Update which personas are active in the current session."""
    active_personas: List[str] = Field(
        ..., min_length=1, description="List of persona names to activate"
    )


class PersonaCreateRequest(BaseModel):
    """Create or update a persona definition."""
    name: str = Field(..., min_length=1, max_length=25)
    description: str = Field(default="", max_length=30)
    system_prompt: str = Field(..., min_length=1, max_length=8192)
    router_hints: str = Field(..., min_length=1, max_length=256)
    avatar_color: str = Field(default="#FF0000")
    avatar_image: Optional[str] = None
    reference_audio: Optional[str] = None
    reference_audio_transcript: Optional[str] = None
    language: str = Field(default="en", min_length=2, max_length=2)


class PersonaUpdateRequest(PersonaCreateRequest):
    """Update an existing persona (same fields as create)."""
    pass


class TTSRequest(BaseModel):
    """Proxy request to the TTS server."""
    text: str = Field(..., min_length=1, description="Text to synthesize")
    persona_name: str = Field(..., description="Which persona to synthesize for")


class STTRequest(BaseModel):
    """Proxy request to the STT server."""
    audio_base64: str = Field(..., min_length=1, description="Base64-encoded audio to transcribe")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PersonaResponse(BaseModel):
    """A single persona definition returned to the frontend."""
    name: str
    description: str
    avatar_color: str
    avatar_image: Optional[str] = None
    tts_capable: bool = False


class PersonaDetailResponse(BaseModel):
    """Full persona detail including all editable fields."""
    name: str
    description: str
    system_prompt: str
    router_hints: str
    avatar_color: str
    avatar_image: Optional[str] = None
    reference_audio: Optional[str] = None
    reference_audio_transcript: Optional[str] = None
    language: str
    tts_capable: bool = False


class SessionState(BaseModel):
    """Current session snapshot for the frontend."""
    history: List[dict] = Field(default_factory=list)
    active_personas: List[str] = Field(default_factory=list)


class TTSResponse(BaseModel):
    """Base64-encoded audio from the TTS server."""
    audio_base64: str
    sample_rate: int = 24000


class STTResponse(BaseModel):
    """Transcribed text from the STT server."""
    text: str
    language: Optional[str] = None


class TTSHealthResponse(BaseModel):
    """TTS availability status."""
    enabled: bool
    available: bool = Field(
        default=False,
        description="True if the TTS server responded to /health",
    )
    streaming: bool = Field(
        default=False,
        description="True if streaming (sentence-by-sentence) TTS mode is configured",
    )


class STTHealthResponse(BaseModel):
    """STT availability status."""
    enabled: bool
    available: bool = Field(
        default=False,
        description="True if the STT server responded to /health",
    )


# ---------------------------------------------------------------------------
# Internal models (not exposed over the API)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """A single turn in the conversation history."""
    role: Literal["user", "assistant"]
    content: str
    # Which persona produced this message (only set for assistant messages)
    persona: Optional[str] = None
