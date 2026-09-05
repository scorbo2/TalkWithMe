"""Configuration loading and validation.

Loads settings.yaml and the Personas directory from the project root.
Caches parsed config so we're not hitting disk on every request.

Personas are stored as per-persona subdirectories (see
app/services/persona_store.py). The legacy personas.yaml file is read
only for the one-time startup migration — never for anything else.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persona memory limits (docs/feature_persona_memory.md)
# ---------------------------------------------------------------------------

DEFAULT_MEMORY_SIZE = 8192
"""Default per-persona memories.txt size budget, in UTF-8 bytes."""
MAX_MEMORY_SIZE = 16384
"""Hard cap for a persona's memory_size; larger values are invalid."""
MAX_MEMORY_LINE_CHARS = 1024
"""Max length of a single memory, in characters. Longer memories are
rejected, never truncated — the LLM can reformulate a shorter one."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def clean_base_url(raw: Optional[str]) -> Optional[str]:
    """Normalize a user-supplied base URL (config models AND save-time
    comparisons must agree on the stored form — see the settings router).

    Strips surrounding whitespace and any trailing slashes, then maps an
    empty result to None. Trailing slashes matter because every client
    builds URLs as f"{base_url}/path": a stored trailing slash turns into
    a doubled "///path" that servers 404 on — and for TTS that 404 gets
    negative-cached, blinding /capabilities discovery for the process
    lifetime.
    """
    if raw is None:
        return None
    cleaned = raw.strip().rstrip("/")
    return cleaned or None


class LLMSettings(BaseModel):
    base_url: str = "http://localhost:8080"
    model: str = "default"
    max_tokens: int = 1024
    temperature: float = 0.8


# Pre-generification TTS parameter keys (docs/feature_TTS_generification.md,
# plan T2). A legacy settings.yaml stores them as top-level tts: fields; the
# before-validator below folds them into TTSConfig.parameters so an existing
# file keeps working with zero user action.
_TTS_LEGACY_PARAMETER_KEYS = ("num_steps", "guidance_scale", "seed")


class TTSConfig(BaseModel):
    enabled: bool = True
    base_url: Optional[str] = None
    timeout: float = 60.0
    streaming: bool = False
    # Engine parameters, generically (TTS generification, plan T1): a name ->
    # value map for whatever the connected engine's /capabilities document
    # advertises. Deliberately UNtyped: values are validated at settings-save
    # time (routers/settings.py, against the cached capabilities doc) and by
    # the server's own 422s — a hand-edited YAML with a wrong value type must
    # never crash startup.
    parameters: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_parameters(cls, data):
        """Fold legacy tts: keys (num_steps/guidance_scale/seed) into
        `parameters` (plan T2: migrate on load, not on save).

        Rules:
        - legacy keys only  -> folded into `parameters` (logged);
        - `parameters` AND legacy keys -> `parameters` wins, the legacy keys
          are warned about and dropped (never merged — a hand-edited mix is
          ambiguous by definition);
        - null values are dropped, because an absent key means "let the
          engine decide"; seed 0 is dropped the same way (the old UI's
          "0 = random" encoding is the predecessor of "absent = random").
        """
        if not isinstance(data, dict):
            return data
        # A non-dict `parameters` is a hand-editing accident (the field never
        # existed before this change, so no real file has one). Drop it with
        # a warning rather than letting pydantic crash startup.
        if "parameters" in data and not isinstance(data["parameters"], dict):
            logger.warning(
                "settings.yaml: tts.parameters must be a mapping of parameter "
                "name to value, got %r; ignoring it",
                type(data["parameters"]).__name__,
            )
            data = {**data, "parameters": {}}
        legacy_values = {k: data[k] for k in _TTS_LEGACY_PARAMETER_KEYS if k in data}
        if not legacy_values:
            return data
        cleaned = {k: v for k, v in data.items() if k not in _TTS_LEGACY_PARAMETER_KEYS}
        if "parameters" in cleaned:
            logger.warning(
                "settings.yaml: tts section has both 'parameters' and the "
                "legacy keys %s; keeping 'parameters' and ignoring the legacy "
                "keys",
                sorted(legacy_values),
            )
            return cleaned
        migrated = {
            key: value
            for key, value in legacy_values.items()
            if value is not None and not (key == "seed" and value == 0)
        }
        if migrated:
            logger.info(
                "settings.yaml: folded legacy TTS keys %s into tts.parameters "
                "(they leave the file on the next settings save)",
                sorted(migrated),
            )
        cleaned["parameters"] = migrated
        return cleaned

    @model_validator(mode="after")
    def _normalize_base_url(self) -> "TTSConfig":
        """Blank → None (implicitly disables TTS); trailing slashes stripped."""
        self.base_url = clean_base_url(self.base_url)
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
        """Blank → None (implicitly disables STT); trailing slashes stripped."""
        self.base_url = clean_base_url(self.base_url)
        return self

    @property
    def is_active(self) -> bool:
        """Feature is active only when explicitly enabled AND a base_url is configured."""
        return self.enabled and bool(self.base_url)


class GeneralConfig(BaseModel):
    """Application-wide feature flags and preferences."""
    persona_name_mentions: bool = True
    max_persona_replies: int = Field(default=1, ge=1, le=4)
    max_turns_for_context: int = Field(default=6, ge=1, le=50, description="Max history turns sent to the LLM")
    show_tool_calls: bool = True
    # Global kill-switch for the persona memory feature (docs/
    # feature_persona_memory.md). False disables the add_memory tool AND
    # stops injecting saved memories into system prompts — without touching
    # any persona's memory_size or deleting any memories.txt.
    enable_persona_memories: bool = True
    # Where persona subdirectories live. Absolute, or relative to the
    # project root; None/empty falls back to <project root>/Personas.
    # yaml-only for now (no UI) — like the mcp: section, changes need a
    # restart, because the directory is resolved at startup and by the
    # persona router from this cache.
    personas_directory: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _strict_enable_persona_memories(cls, data):
        """Reject (warn + default) a non-boolean enable_persona_memories.

        The spec is strict: anything that is not a real boolean in
        settings.yaml is invalid, logged, and replaced with the default
        (True). We intercept before pydantic's lax coercion, which would
        silently turn the string "false" into False — a far sneakier failure
        than a loud warning at startup.
        """
        if isinstance(data, dict) and "enable_persona_memories" in data:
            value = data["enable_persona_memories"]
            if not isinstance(value, bool):
                logger.warning(
                    "settings.yaml: invalid general.enable_persona_memories %r; "
                    "expected a boolean, assuming the default (true)",
                    value,
                )
                data = {**data, "enable_persona_memories": True}
        return data


class MCPServerConfig(BaseModel):
    """A single MCP server endpoint (SSE/HTTP transport only — no stdio)."""
    name: str
    url: str
    # gt=0: in httpx a 0.0 timeout does NOT mean "no timeout" — Timeout(0)
    # keeps a real zero and fails every request instantly, so the typo
    # would silently kill the server. le=300: a hung tool call should not
    # be allowed to stall the SSE stream for unreasonably long.
    timeout: float = Field(default=10.0, gt=0, le=300)

    @model_validator(mode="after")
    def _validate_url_scheme(self) -> "MCPServerConfig":
        # Fail at config load, not per-call: a scheme-less typo
        # ("localhost:9000") used to surface as a ConnectTimeout warning
        # buried in the log on every request instead of a clear startup error.
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(
                f"MCP server '{self.name}': url must start with http:// or https://, got {self.url!r}"
            )
        return self


class MCPConfig(BaseModel):
    """MCP server configurations and the agentic tool-call loop cap."""
    servers: List[MCPServerConfig] = Field(default_factory=list)
    max_tool_iterations: int = Field(default=8, ge=1, le=50, description="Max tool-call rounds per persona reply")


class AppSettings(BaseModel):
    llm: LLMSettings = LLMSettings()
    tts: TTSConfig = TTSConfig()
    stt: STTConfig = STTConfig()
    general: GeneralConfig = GeneralConfig()
    mcp: MCPConfig = Field(default_factory=MCPConfig)


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
    allow_tool_calls: bool = False
    # Size budget (UTF-8 bytes) for this persona's memories.txt. 0 disables
    # memory saving. The ge/le constraints are a safety net: persona_store
    # sanitizes frontmatter values before this model is constructed, and the
    # router validates form input, so out-of-range values should never reach
    # here (and a legacy prompt.md missing the key gets the default).
    memory_size: int = Field(default=DEFAULT_MEMORY_SIZE, ge=0, le=MAX_MEMORY_SIZE)
    # Where this persona's files live on disk (set by the directory scan;
    # None for personas assembled outside of it, e.g. in tests).
    persona_dir: Optional[Path] = None

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
        mcp=MCPConfig(**raw.get("mcp", {})),
    )
    return _settings_cache


def get_personas_directory() -> Path:
    """Resolve the configured Personas directory.

    ``general.personas_directory`` may be absolute or relative to the
    project root; missing/empty falls back to <project root>/Personas.
    """
    configured = (get_settings().general.personas_directory or "").strip()
    if not configured:
        return _PROJECT_ROOT / "Personas"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else _PROJECT_ROOT / path


def load_personas() -> PersonasConfig:
    """Load personas from the configured Personas directory and cache them.

    Startup decision matrix (docs/feature_persona_autodiscovery.md):

    * ``personas.yaml`` AND a populated Personas directory -> warn
      loudly; the directory wins and the YAML is left in place (it is
      IGNORED — not renamed, not deleted).
    * ``personas.yaml`` only -> one-time automatic migration into the
      directory; the YAML is renamed to personas.yaml.bak on success.
    * Neither -> log "No personas found!", create the (empty) directory,
      and start with zero personas.

    Raises on fatal errors (uncreatable directory, failed migration):
    the app must not run while unsure where its personas live.
    """
    global _personas_cache
    # Imported lazily: persona_store imports the Persona models from this
    # module, so a top-level import would be circular.
    from app.services import persona_store

    root = get_personas_directory()
    legacy_yaml = _PROJECT_ROOT / "personas.yaml"

    if legacy_yaml.is_file():
        if _personas_directory_populated(root):
            logger.warning(
                "Both personas.yaml and the Personas directory (%s) exist. "
                "The directory takes precedence and personas.yaml is IGNORED. "
                "Delete or rename personas.yaml to silence this warning.",
                root,
            )
        else:
            persona_store.migrate_from_legacy_yaml(legacy_yaml, root)
    elif not root.is_dir():
        logger.error("No personas found!")
        logger.error("Persona directory: %s", root)
        try:
            root.mkdir(parents=True)
        except OSError as exc:
            logger.error("Cannot create the Personas directory %s: %s — aborting startup.", root, exc)
            raise persona_store.PersonaStorageError(
                f"cannot create personas directory {root}: {exc}"
            ) from exc
        logger.info("Created empty Personas directory: %s", root)

    _personas_cache = PersonasConfig(personas=persona_store.scan_personas_directory(root))
    return _personas_cache


def _personas_directory_populated(root: Path) -> bool:
    """True when the directory exists and holds at least one persona subdirectory."""
    if not root.is_dir():
        return False
    return any(entry.is_dir() for entry in root.iterdir())


def set_personas_cache(config: PersonasConfig) -> None:
    """Replace the in-memory persona cache without touching the disk.

    The persona router calls this after every directory mutation; the
    directory on disk is the source of truth, so there is nothing to
    persist here. Skipping this step is how the UI ends up stale until
    the next restart — it has happened before.
    """
    global _personas_cache
    _personas_cache = config


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


def save_settings(config: AppSettings, path: Optional[Path] = None) -> None:
    """Serialize AppSettings back to settings.yaml and update the in-memory cache."""
    global _settings_cache
    target = path or _PROJECT_ROOT / "settings.yaml"
    raw = {
        "llm": config.llm.model_dump(exclude_none=False),
        "tts": config.tts.model_dump(exclude_none=False),
        "stt": config.stt.model_dump(exclude_none=False),
        "general": config.general.model_dump(exclude_none=False),
        "mcp": config.mcp.model_dump(exclude_none=False),
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
