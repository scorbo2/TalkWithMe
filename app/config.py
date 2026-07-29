"""Configuration loading and validation.

Loads settings.yaml and personas.yaml from the project root.
Caches parsed config so we're not hitting disk on every request.
"""

from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field


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
    base_url: str = "http://localhost:5500"
    num_steps: int = 10
    guidance_scale: float = 3.0
    seed: Optional[int] = None
    timeout: float = 60.0


class AppSettings(BaseModel):
    llm: LLMSettings = LLMSettings()
    tts: TTSConfig = TTSConfig()


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
    language: str = "en"

    @property
    def tts_capable(self) -> bool:
        """TTS requires both reference audio AND its transcript."""
        return bool(self.reference_audio and self.reference_audio_transcript)


class PersonasConfig(BaseModel):
    personas: List[Persona] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_settings_cache: Optional[AppSettings] = None
_personas_cache: Optional[PersonasConfig] = None


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
    )
    return _settings_cache


def load_personas(path: Optional[Path] = None) -> PersonasConfig:
    """Parse personas.yaml. Returns empty list if file is missing."""
    global _personas_cache
    target = path or _PROJECT_ROOT / "personas.yaml"
    if not target.exists():
        return PersonasConfig()
    with open(target) as f:
        raw = yaml.safe_load(f) or {}
    _personas_cache = PersonasConfig(
        personas=[Persona(**p) for p in raw.get("personas", [])]
    )
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


def reload_all():
    """Force-reload both config files. Useful for dev hot-reload."""
    global _settings_cache, _personas_cache
    _settings_cache = None
    _personas_cache = None
    load_settings()
    load_personas()
