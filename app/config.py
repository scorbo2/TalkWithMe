"""Configuration loading and validation.

Loads settings.yaml and personas.yaml from the project root.
Caches parsed config so we're not hitting disk on every request.
"""

import logging
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class LLMSettings(BaseModel):
    base_url: str = "http://localhost:8080"
    model: str = "default"
    max_tokens: int = 1024
    temperature: float = 0.8


class TTSConfig(BaseModel):
    enabled: bool = True
    base_url: Optional[str] = None
    num_steps: int = 10
    guidance_scale: float = 3.0
    seed: Optional[int] = None
    timeout: float = 60.0
    streaming: bool = False

    @model_validator(mode="after")
    def _normalize_base_url(self) -> "TTSConfig":
        """Treat blank strings as None so a missing URL implicitly disables TTS."""
        if self.base_url is not None and not self.base_url.strip():
            self.base_url = None
        return self

    @property
    def is_active(self) -> bool:
        """Feature is active only when explicitly enabled AND a base_url is configured."""
        return self.enabled and bool(self.base_url)


class STTConfig(BaseModel):
    """Speech-to-text configuration, independent of TTS."""
    enabled: bool = True
    base_url: Optional[str] = None
    timeout: float = 30.0

    @model_validator(mode="after")
    def _normalize_base_url(self) -> "STTConfig":
        """Treat blank strings as None so a missing URL implicitly disables STT."""
        if self.base_url is not None and not self.base_url.strip():
            self.base_url = None
        return self

    @property
    def is_active(self) -> bool:
        """Feature is active only when explicitly enabled AND a base_url is configured."""
        return self.enabled and bool(self.base_url)


class GeneralConfig(BaseModel):
    """Application-wide feature flags and preferences."""
    persona_name_mentions: bool = True
    max_persona_replies: int = Field(default=1, ge=1, le=4)


class AppSettings(BaseModel):
    llm: LLMSettings = LLMSettings()
    tts: TTSConfig = TTSConfig()
    stt: STTConfig = STTConfig()
    general: GeneralConfig = GeneralConfig()


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------

class Persona(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    router_hints: str = ""
    avatar_color: str = "#888888"
    avatar_image: Optional[str] = None
    reference_audio: Optional[str] = None
    reference_audio_transcript: Optional[str] = None
    reference_audio_language: str = "en"

    @property
    def tts_capable(self) -> bool:
        """TTS requires both reference audio AND its transcript."""
        return bool(self.reference_audio and self.reference_audio_transcript)


class PersonasConfig(BaseModel):
    personas: List[Persona] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Chat Rooms
# ---------------------------------------------------------------------------

class ChatRoom(BaseModel):
    """A named grouping of personas."""
    name: str
    persona_names: List[str] = Field(default_factory=list)
    echo_chamber: bool = False


class ChatRoomsConfig(BaseModel):
    chat_rooms: List[ChatRoom] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_settings_cache: Optional[AppSettings] = None
_personas_cache: Optional[PersonasConfig] = None
_chatrooms_cache: Optional[ChatRoomsConfig] = None


def load_settings(path: Optional[Path] = None) -> AppSettings:
    """Parse settings.yaml. Falls back to defaults if file is missing."""
    global _settings_cache
    target = path or _PROJECT_ROOT / "settings.yaml"
    if not target.exists():
        return AppSettings()
    with open(target) as f:
        raw = yaml.safe_load(f) or {}
    _settings_cache = AppSettings(
        llm=LLMSettings(**raw.get("llm", {})),
        tts=TTSConfig(**raw.get("tts", {})),
        stt=STTConfig(**raw.get("stt", {})),
        general=GeneralConfig(**raw.get("general", {})),
    )
    return _settings_cache


def load_personas(path: Optional[Path] = None) -> PersonasConfig:
    """Parse personas.yaml. Returns empty list if file is missing.

    Migrates the legacy 'language' key to 'reference_audio_language' on the
    fly, so existing personas.yaml files from before the rename still load
    without requiring manual edits.
    """
    global _personas_cache
    target = path or _PROJECT_ROOT / "personas.yaml"
    if not target.exists():
        return PersonasConfig()
    with open(target) as f:
        raw = yaml.safe_load(f) or {}
    migrated = []
    for p in raw.get("personas", []):
        if "language" in p and "reference_audio_language" not in p:
            name = p.get("name", "<unknown>")
            logger.info(
                "Persona '%s': migrating legacy 'language' key to 'reference_audio_language'",
                name,
            )
            p["reference_audio_language"] = p.pop("language")
        migrated.append(p)
    _personas_cache = PersonasConfig(personas=[Persona(**p) for p in migrated])
    return _personas_cache


def get_settings() -> AppSettings:
    """Return cached settings, loading if necessary."""
    if _settings_cache is None:
        return load_settings()
    return _settings_cache


def get_personas() -> PersonasConfig:
    """Return cached personas, loading if necessary."""
    if _personas_cache is None:
        return load_personas()
    return _personas_cache


def save_personas(config: PersonasConfig, path: Optional[Path] = None) -> None:
    """Serialize PersonasConfig back to personas.yaml and update the in-memory cache."""
    global _personas_cache
    target = path or _PROJECT_ROOT / "personas.yaml"
    raw = {
        "personas": [
            p.model_dump(exclude_none=False)
            for p in config.personas
        ]
    }
    with open(target, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    _personas_cache = config


def save_settings(config: AppSettings, path: Optional[Path] = None) -> None:
    """Serialize AppSettings back to settings.yaml and update the in-memory cache."""
    global _settings_cache
    target = path or _PROJECT_ROOT / "settings.yaml"
    raw = {
        "llm": config.llm.model_dump(exclude_none=False),
        "tts": config.tts.model_dump(exclude_none=False),
        "stt": config.stt.model_dump(exclude_none=False),
        "general": config.general.model_dump(exclude_none=False),
    }
    with open(target, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    _settings_cache = config


def get_chatrooms() -> ChatRoomsConfig:
    """Return cached chat rooms, loading if necessary."""
    if _chatrooms_cache is None:
        return load_chatrooms()
    return _chatrooms_cache


def load_chatrooms(path: Optional[Path] = None) -> ChatRoomsConfig:
    """Parse chatrooms.yaml. Returns empty config if file is missing."""
    global _chatrooms_cache
    target = path or _PROJECT_ROOT / "chatrooms.yaml"
    if not target.exists():
        return ChatRoomsConfig()
    with open(target) as f:
        raw = yaml.safe_load(f) or {}
    _chatrooms_cache = ChatRoomsConfig(
        chat_rooms=[ChatRoom(**cr) for cr in raw.get("chat_rooms", [])]
    )
    return _chatrooms_cache


def save_chatrooms(config: ChatRoomsConfig, path: Optional[Path] = None) -> None:
    """Serialize ChatRoomsConfig back to chatrooms.yaml and update the in-memory cache."""
    global _chatrooms_cache
    target = path or _PROJECT_ROOT / "chatrooms.yaml"
    raw = {
        "chat_rooms": [
            cr.model_dump(exclude_none=False)
            for cr in config.chat_rooms
        ]
    }
    with open(target, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    _chatrooms_cache = config


def reload_all():
    """Force-reload all config files. Useful for dev hot-reload."""
    global _settings_cache, _personas_cache, _chatrooms_cache
    _settings_cache = None
    _personas_cache = None
    _chatrooms_cache = None
    load_settings()
    load_personas()
    load_chatrooms()
