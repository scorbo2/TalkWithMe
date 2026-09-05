"""Shared test factories and fake HTTP clients.

Everything a test needs to build config objects, parse SSE streams, and
stand in for the LLM/TTS/STT/MCP servers lives here so individual test
modules stay focused on behaviour, not plumbing.
"""

import copy
import json
import json as _json  # alias: FakeMCPClient.post has a `json` *parameter* that
                      # shadows the module, so its SSE handlers use _json.dumps
from pathlib import Path
from typing import Any, Callable, List, Optional

import httpx

from app.config import (
    AppSettings,
    ChatRoom,
    ChatRoomsConfig,
    GeneralConfig,
    LLMSettings,
    MCPConfig,
    MCPServerConfig,
    Persona,
    PersonasConfig,
    STTConfig,
    TTSConfig,
)


# ---------------------------------------------------------------------------
# Config factories
# ---------------------------------------------------------------------------

def make_settings(
    *,
    llm: Optional[LLMSettings] = None,
    tts: Optional[TTSConfig] = None,
    stt: Optional[STTConfig] = None,
    general: Optional[GeneralConfig] = None,
    mcp: Optional[MCPConfig] = None,
) -> AppSettings:
    """Build an AppSettings with sane test defaults (TTS/STT inactive)."""
    return AppSettings(
        llm=llm or LLMSettings(base_url="http://llm.local:8080", model="test-model"),
        tts=tts or TTSConfig(enabled=False, base_url=None),
        stt=stt or STTConfig(enabled=False, base_url=None),
        general=general or GeneralConfig(),
        mcp=mcp or MCPConfig(),
    )


def make_personas() -> PersonasConfig:
    """Two stock personas: Alex (TTS-incapable) and Luna (TTS-capable).

    Purely in-memory (persona_dir=None) — use for tests that never touch
    the disk. Use make_personas_in_dir() when the test exercises the
    persona directory on disk (persona CRUD router, migration, ...).
    """
    return PersonasConfig(
        personas=[
            Persona(
                name="Alex",
                description="A friendly assistant",
                system_prompt="You are Alex, a friendly assistant.",
                router_hints="general questions",
            ),
            Persona(
                name="Luna",
                description="A philosophical poet",
                system_prompt="You are Luna, a philosophical poet.",
                router_hints="philosophy, feelings",
                reference_audio="reference/luna.wav",
                reference_audio_transcript="reference/luna.txt",
                reference_audio_language="en",
            ),
        ]
    )


def make_personas_in_dir(root) -> PersonasConfig:
    """Materialize the stock Alex/Luna persona set as real directories.

    SETUP factory: writes the stock files through app.services.persona_store
    (the same code the router uses) and returns the scanned cache, so
    persona_dir / avatar_image / reference_audio are all real on-disk paths.
    Luna gets a ref.wav + ref.txt (TTS-capable); Alex gets neither.

    NOTE: it RE-WRITES the stock files on every call, so it must not be
    used to refresh a cache on an already-populated directory — any
    non-stock file written since setup (a custom prompt.md, an avatar, ...)
    would be clobbered. For cache refreshes use rescan_personas().
    """
    from app.services import persona_store

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    alex = root / "Alex"
    alex.mkdir(exist_ok=True)
    persona_store.write_prompt_md(
        alex,
        name="Alex",
        description="A friendly assistant",
        router_hints="general questions",
        avatar_color="#888888",
        allow_tool_calls=False,
        system_prompt="You are Alex, a friendly assistant.",
    )

    luna = root / "Luna"
    luna.mkdir(exist_ok=True)
    persona_store.write_prompt_md(
        luna,
        name="Luna",
        description="A philosophical poet",
        router_hints="philosophy, feelings",
        avatar_color="#888888",
        allow_tool_calls=False,
        system_prompt="You are Luna, a philosophical poet.",
    )
    persona_store.write_language_file(luna, "en")
    (luna / persona_store.REFERENCE_AUDIO_FILENAME).write_bytes(b"RIFF-fake-wav")
    (luna / persona_store.TRANSCRIPT_FILENAME).write_text(
        "The stars are just pinpricks in the dark.", encoding="utf-8",
    )

    return rescan_personas(root)


def rescan_personas(root) -> PersonasConfig:
    """Rebuild the personas cache from disk WITHOUT writing anything.

    Use this to refresh the app's persona cache after a test (or the
    router under test) has written persona files directly. Unlike
    make_personas_in_dir() it never touches the stock files, so files
    written since setup survive the refresh.
    """
    from app.services import persona_store
    return PersonasConfig(personas=persona_store.scan_personas_directory(Path(root)))


def make_chatrooms() -> ChatRoomsConfig:
    """One stock chat room containing both personas."""
    return ChatRoomsConfig(
        chat_rooms=[ChatRoom(name="TNG", persona_names=["Alex", "Luna"], echo_chamber=False)]
    )


def make_mcp_server(name: str = "tools-1", url: str = "http://mcp.local:9000") -> MCPServerConfig:
    return MCPServerConfig(name=name, url=url, timeout=5.0)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def parse_sse_events(body: str) -> List[dict]:
    """Parse an SSE response body into a list of event dicts."""
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def sse_events_by_type(events: List[dict], event_type: str) -> List[dict]:
    return [e for e in events if e.get("type") == event_type]



# ---------------------------------------------------------------------------
# TTS capabilities documents (TTS generification)
# ---------------------------------------------------------------------------
#
# The four snapshots below are FAITHFUL COPIES of the real v2 documents served
# by the tts-serve engine servers (source: tts-serve/impl/tests/snapshots/*.json).
# They span the full range of the document shape — enum vs free-form language,
# watermarked vs not, default-null numbers, booleans, advanced params — so
# tests built on them exercise the *real* protocol, not a paraphrase.
# There is a byte-identical JSON copy of each in tests/fixtures/ (used by the
# plain-Node tests/test_tts_settings.js, which cannot import Python). If a
# snapshot changes upstream, re-copy it into BOTH places verbatim (a
# throwaway script that reads the snapshot files is the reliable way to do
# it) — tests/test_tts_fixtures.py fails loudly if the two drift apart.

_CAPABILITIES_SNAPSHOTS = {
    'chatterbox': json.loads(r'''
{
  "schema_version": 2,
  "engine": "chatterbox",
  "model": "chatterbox-multilingual-v3",
  "device": "cuda",
  "sample_rate": 24000,
  "watermarked": true,
  "endpoint": "/synthesize",
  "reference_audio": {
    "required": true,
    "formats": [
      "wav",
      "mp3",
      "ogg",
      "flac"
    ],
    "min_duration_s": 2.0,
    "max_duration_s": null,
    "note": "Only the first 10 s are used for speaker conditioning; longer clips are truncated."
  },
  "languages": [
    "ar",
    "da",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "he",
    "hi",
    "it",
    "ja",
    "ko",
    "ms",
    "nl",
    "no",
    "pl",
    "pt",
    "ru",
    "sv",
    "sw",
    "tr",
    "zh"
  ],
  "parameters": [
    {
      "name": "text",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Text to synthesize, e.g. 'Hello there'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "audio_base64",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Reference voice sample (roughly 10 s works well) as a base64 string.  Any container soundfile can decode (WAV, MP3, OGG, FLAC, ...).  The model uses only its first 10 s.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": 10000000,
      "group": "common",
      "advanced": false
    },
    {
      "name": "language",
      "type": "string",
      "required": false,
      "default": "en",
      "description": "Two-letter language code, e.g. 'en', 'fr', 'zh' (supported: ar, da, de, el, en, es, fi, fr, he, hi, it, ja, ko, ms, nl, no, pl, pt, ru, sv, sw, tr, zh).  Omitted or empty defaults to 'en'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": [
        "ar",
        "da",
        "de",
        "el",
        "en",
        "es",
        "fi",
        "fr",
        "he",
        "hi",
        "it",
        "ja",
        "ko",
        "ms",
        "nl",
        "no",
        "pl",
        "pt",
        "ru",
        "sv",
        "sw",
        "tr",
        "zh"
      ],
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "seed",
      "type": "integer",
      "required": false,
      "default": null,
      "description": "Random seed for reproducibility.  If omitted, a random seed in [1, 1000] is chosen and echoed in the response.",
      "min": 1.0,
      "max": 1000.0,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "exaggeration",
      "type": "number",
      "required": false,
      "default": 0.5,
      "description": "Expression/energy boost (README: ~0.5 general use, ~0.7+ for dramatic speech).  Higher values tend to speed up delivery.",
      "min": 0.0,
      "max": 2.0,
      "step": 0.05,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "cfg_weight",
      "type": "number",
      "required": false,
      "default": 0.5,
      "description": "Classifier-free guidance weight (README: ~0.5 general use, ~0.3 for fast-talking references or to reduce accent bleed from a foreign-language reference clip).",
      "min": 0.0,
      "max": 1.0,
      "step": 0.05,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "temperature",
      "type": "number",
      "required": false,
      "default": 0.8,
      "description": "Sampling temperature for the T3 language model.",
      "min": 0.0,
      "max": 2.0,
      "step": 0.05,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "repetition_penalty",
      "type": "number",
      "required": false,
      "default": 1.2,
      "description": "Penalty applied to repeated speech tokens.",
      "min": 1.0,
      "max": 2.0,
      "step": 0.05,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    },
    {
      "name": "min_p",
      "type": "number",
      "required": false,
      "default": 0.05,
      "description": "Min-p sampling threshold.",
      "min": 0.0,
      "max": 1.0,
      "step": 0.01,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    },
    {
      "name": "top_p",
      "type": "number",
      "required": false,
      "default": 1.0,
      "description": "Nucleus (top-p) sampling threshold.",
      "min": 0.0,
      "max": 1.0,
      "step": 0.01,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    }
  ]
}
'''),
    'dots.tts': json.loads(r'''
{
  "schema_version": 2,
  "engine": "dots.tts",
  "model": "rednote-hilab/dots.tts-soar",
  "device": "cpu",
  "sample_rate": 48000,
  "watermarked": false,
  "endpoint": "/synthesize",
  "reference_audio": {
    "required": true,
    "formats": [
      "wav",
      "mp3",
      "ogg",
      "flac"
    ],
    "min_duration_s": 2.0,
    "max_duration_s": null,
    "note": "Roughly 10 s of clean, low-noise audio clones best.  The transcript (reference_text) is optional but improves conditioning."
  },
  "languages": null,
  "parameters": [
    {
      "name": "text",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Text to synthesize, e.g. 'Hello there'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "audio_base64",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Reference voice sample (roughly 10 s works well) as a base64 string.  Any container soundfile can decode (WAV, MP3, OGG, FLAC, ...).",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": 10000000,
      "group": "common",
      "advanced": false
    },
    {
      "name": "reference_text",
      "type": "string",
      "required": false,
      "default": null,
      "description": "Exact transcript of the reference audio.  Optional \u2014 audio-only cloning works, but the transcript improves conditioning.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "language",
      "type": "string",
      "required": false,
      "default": "en",
      "description": "Two-letter language code, e.g. 'en' or 'zh', or 'auto' for auto-detection.  Omitted or empty defaults to 'en'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "seed",
      "type": "integer",
      "required": false,
      "default": null,
      "description": "Random seed for reproducibility.  If omitted, a random seed in [1, 1000] is chosen and echoed in the response.",
      "min": 1.0,
      "max": 1000.0,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "num_steps",
      "type": "integer",
      "required": false,
      "default": 10,
      "description": "Flow-matching sampling steps.",
      "min": 1.0,
      "max": 64.0,
      "step": 1.0,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "guidance_scale",
      "type": "number",
      "required": false,
      "default": 1.2,
      "description": "Classifier-free guidance scale.",
      "min": 0.0,
      "max": 5.0,
      "step": 0.1,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "speaker_scale",
      "type": "number",
      "required": false,
      "default": 1.5,
      "description": "Scale applied to the reference speaker embedding.",
      "min": 0.0,
      "max": 3.0,
      "step": 0.1,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "ode_method",
      "type": "string",
      "required": false,
      "default": "euler",
      "description": "ODE / flow-matching solver method.",
      "min": null,
      "max": null,
      "step": null,
      "enum": [
        "euler",
        "midpoint",
        "rk4"
      ],
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    }
  ]
}
'''),
    'omnivoice': json.loads(r'''
{
  "schema_version": 2,
  "engine": "omnivoice",
  "model": "k2-fsa/OmniVoice",
  "device": "cuda",
  "sample_rate": 24000,
  "watermarked": false,
  "endpoint": "/synthesize",
  "reference_audio": {
    "required": true,
    "formats": [
      "wav",
      "mp3",
      "ogg",
      "flac"
    ],
    "min_duration_s": 2.0,
    "max_duration_s": 20.0,
    "note": "3-10 s recommended.  Clips over 20 s are trimmed at the largest silence gap and cloning quality degrades.  If reference_text is omitted, the clip is auto-transcribed with Whisper."
  },
  "languages": null,
  "parameters": [
    {
      "name": "text",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Text to synthesize, e.g. 'Hello there'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "audio_base64",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Reference voice sample (3-10 s recommended) as a base64 string. Any container soundfile can decode (WAV, MP3, OGG, FLAC, ...). Clips over 20 s are trimmed at the largest silence gap.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": 10000000,
      "group": "common",
      "advanced": false
    },
    {
      "name": "reference_text",
      "type": "string",
      "required": false,
      "default": null,
      "description": "Exact transcript of the reference audio.  If omitted, the reference is auto-transcribed with Whisper ASR (the first such request pays a one-time ASR model load, which can be slow).",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "language",
      "type": "string",
      "required": false,
      "default": "en",
      "description": "Two-letter language code, e.g. 'en' or 'fr'.  Omitted or empty defaults to 'en'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "seed",
      "type": "integer",
      "required": false,
      "default": null,
      "description": "Random seed for reproducibility.  If omitted, a random seed in [1, 1000] is chosen and echoed in the response.",
      "min": 1.0,
      "max": 1000.0,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "num_steps",
      "type": "integer",
      "required": false,
      "default": 32,
      "description": "Flow-matching sampling steps.  32 is the engine default; 16 is a reasonable fast/quality trade-off.",
      "min": 4.0,
      "max": 128.0,
      "step": 4.0,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "guidance_scale",
      "type": "number",
      "required": false,
      "default": 2.0,
      "description": "Classifier-free guidance scale.",
      "min": 0.0,
      "max": 10.0,
      "step": 0.1,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "denoise",
      "type": "boolean",
      "required": false,
      "default": true,
      "description": "Prepend the denoise token (recommended when the reference clip has background noise).",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    }
  ]
}
'''),
    'qwen3-tts': json.loads(r'''
{
  "schema_version": 2,
  "engine": "qwen3-tts",
  "model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
  "device": "cuda",
  "sample_rate": 24000,
  "watermarked": false,
  "endpoint": "/synthesize",
  "reference_audio": {
    "required": true,
    "formats": [
      "wav",
      "mp3",
      "ogg",
      "flac"
    ],
    "min_duration_s": 2.0,
    "max_duration_s": null,
    "note": "~3 s is enough for high-quality cloning.  If reference_text is omitted, cloning falls back to speaker-embedding-only mode."
  },
  "languages": [
    "de",
    "en",
    "es",
    "fr",
    "it",
    "ja",
    "ko",
    "pt",
    "ru",
    "zh"
  ],
  "parameters": [
    {
      "name": "text",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Text to synthesize, e.g. 'Hello there'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "audio_base64",
      "type": "string",
      "required": true,
      "default": null,
      "description": "Reference voice sample as a base64 string.  Any container soundfile can decode (WAV, MP3, OGG, FLAC, ...).  ~3 s is enough for high-quality cloning.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": 1,
      "max_length": 10000000,
      "group": "common",
      "advanced": false
    },
    {
      "name": "reference_text",
      "type": "string",
      "required": false,
      "default": null,
      "description": "Exact transcript of the reference audio.  If omitted, x_vector_only_mode is enabled automatically (speaker-embedding-only cloning; quality may be reduced).",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "language",
      "type": "string",
      "required": false,
      "default": "en",
      "description": "Two-letter language code, e.g. 'en', 'zh', or 'auto' for auto-detection (supported: de, en, es, fr, it, ja, ko, pt, ru, zh).  Omitted or empty defaults to 'en'.",
      "min": null,
      "max": null,
      "step": null,
      "enum": [
        "auto",
        "zh",
        "en",
        "fr",
        "de",
        "it",
        "ja",
        "ko",
        "pt",
        "ru",
        "es"
      ],
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "seed",
      "type": "integer",
      "required": false,
      "default": null,
      "description": "Random seed for reproducibility.  If omitted, a random seed in [1, 1000] is chosen and echoed in the response.",
      "min": 1.0,
      "max": 1000.0,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "common",
      "advanced": false
    },
    {
      "name": "x_vector_only_mode",
      "type": "boolean",
      "required": false,
      "default": false,
      "description": "Use only the speaker embedding (no reference transcript / in-context codes).  Cloning quality may be reduced.",
      "min": null,
      "max": null,
      "step": null,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    },
    {
      "name": "temperature",
      "type": "number",
      "required": false,
      "default": null,
      "description": "Sampling temperature.  Omit for the engine default.",
      "min": 0.0,
      "max": 2.0,
      "step": 0.05,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": false
    },
    {
      "name": "top_p",
      "type": "number",
      "required": false,
      "default": null,
      "description": "Nucleus sampling threshold.  Omit for the engine default.",
      "min": 0.0,
      "max": 1.0,
      "step": 0.01,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    },
    {
      "name": "repetition_penalty",
      "type": "number",
      "required": false,
      "default": null,
      "description": "Penalty applied to repeated tokens.  Omit for the engine default.",
      "min": 1.0,
      "max": 2.0,
      "step": 0.05,
      "enum": null,
      "min_length": null,
      "max_length": null,
      "group": "engine",
      "advanced": true
    }
  ]
}
'''),
}


def make_capabilities_doc(engine: str = "omnivoice", **overrides: Any) -> dict:
    """A fresh copy of the real capabilities snapshot for `engine`.

    `engine` is the stable slug from the document itself: one of
    "chatterbox", "dots.tts", "omnivoice", "qwen3-tts". `overrides`
    replaces top-level document fields (e.g. schema_version, watermarked).
    Returns a deep copy: mutating the result never touches the snapshot.
    """
    if engine not in _CAPABILITIES_SNAPSHOTS:
        raise ValueError(
            f"Unknown capabilities engine {engine!r}; expected one of "
            f"{sorted(_CAPABILITIES_SNAPSHOTS)}"
        )
    doc = copy.deepcopy(_CAPABILITIES_SNAPSHOTS[engine])
    doc.update(overrides)
    return doc


def make_minimal_capabilities_doc(engine: str = "generic", **overrides: Any) -> dict:
    """Smallest well-formed v2 document: the core vocabulary only.

    A cloning engine with a free-form language and no engine-specific
    parameters — a neutral baseline that carries none of the real engines'
    quirks. Parameter shapes mirror the common fields of the snapshots.
    """
    doc = {
        "schema_version": 2,
        "engine": engine,
        "model": "generic-test-model",
        "device": "cpu",
        "sample_rate": 24000,
        "watermarked": False,
        "endpoint": "/synthesize",
        "reference_audio": {
            "required": True,
            "formats": ["wav"],
            "min_duration_s": 2.0,
            "max_duration_s": None,
            "note": "Test fixture.",
        },
        "languages": None,
        "parameters": [
            {
                "name": "text", "type": "string", "required": True, "default": None,
                "description": "Text to synthesize.",
                "min": None, "max": None, "step": None, "enum": None,
                "min_length": 1, "max_length": None, "group": "common", "advanced": False,
            },
            {
                "name": "audio_base64", "type": "string", "required": True, "default": None,
                "description": "Reference voice sample as a base64 string.",
                "min": None, "max": None, "step": None, "enum": None,
                "min_length": 1, "max_length": 10000000, "group": "common", "advanced": False,
            },
            {
                "name": "reference_text", "type": "string", "required": False, "default": None,
                "description": "Exact transcript of the reference audio.",
                "min": None, "max": None, "step": None, "enum": None,
                "min_length": None, "max_length": None, "group": "common", "advanced": False,
            },
            {
                "name": "language", "type": "string", "required": False, "default": "en",
                "description": "Two-letter language code; omitted or empty defaults to 'en'.",
                "min": None, "max": None, "step": None, "enum": None,
                "min_length": None, "max_length": None, "group": "common", "advanced": False,
            },
            {
                "name": "seed", "type": "integer", "required": False, "default": None,
                "description": "Random seed; omitted means the engine picks one.",
                "min": 1.0, "max": 1000.0, "step": None, "enum": None,
                "min_length": None, "max_length": None, "group": "common", "advanced": False,
            },
        ],
    }
    doc.update(overrides)
    return doc


def make_unexpected_field_422(field: str, value: Any = None) -> dict:
    """The 422 body an extra="forbid" /synthesize returns for a field the
    engine never advertised (e.g. a parameter left over from the previous
    engine after a base_url switch). Mirrors the FastAPI/Pydantic-v2 shape
    that tts-serve's derived request models produce.
    """
    return {
        "detail": [
            {
                "type": "extra_forbidden",
                "loc": ["body", field],
                "msg": "Extra inputs are not permitted",
                "input": value,
            }
        ]
    }



# ---------------------------------------------------------------------------
# Fake httpx clients
# ---------------------------------------------------------------------------

class FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient (non-streaming).

    `responder` is called as responder(method, url, **request_kwargs) and
    must return an httpx.Response or raise. All calls are recorded in
    `.calls` for assertions.
    """

    def __init__(self, responder: Callable[..., httpx.Response], *args, **kwargs):
        self.responder = responder
        self.calls: List[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def _record(self, method: str, url: str, kwargs: dict) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        response = self.responder(method, url, **kwargs)
        # Ensure the response carries a request: httpx 0.24 raises if
        # response.url / raise_for_status() are touched without one.
        # (Note: the public .request getter raises when unset, so check
        # the private attribute.)
        if response._request is None:
            response.request = httpx.Request(method, url)
        return response

    async def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)


def json_response(
    status_code: int,
    payload: Any,
    headers: Optional[dict] = None,
    method: str = "POST",
    url: str = "http://fake.local/rpc",
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        headers=headers or {},
        request=httpx.Request(method, url),
    )


def sse_response(
    status_code: int,
    body: str,
    method: str = "POST",
    url: str = "http://fake.local/rpc",
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
        request=httpx.Request(method, url),
    )


class FakeStreamResponse:
    """Mimics the context manager returned by httpx.AsyncClient.stream()."""

    def __init__(self, lines: List[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://fake"),
                response=httpx.Response(self.status_code),
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeLLMClient:
    """httpx.AsyncClient stand-in for the LLM endpoints.

    `lines` is the list of SSE lines served for every streamed request;
    `payloads` records the JSON payload of each request. For non-streaming
    calls, pass `post_response` (used by chat_completion).
    """

    def __init__(
        self,
        lines: List[str],
        post_response: Optional[httpx.Response] = None,
        *args,
        **kwargs,
    ):
        self.lines = lines
        self.post_response = post_response
        self.payloads: List[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url, json=None):
        self.payloads.append(json)
        return FakeStreamResponse(self.lines)

    async def post(self, url, json=None):
        self.payloads.append(json)
        if self.post_response is None:
            raise RuntimeError("FakeLLMClient: post called without post_response")
        response = self.post_response
        if response._request is None:  # public getter raises when unset
            response.request = httpx.Request("POST", url)
        return response


class FakeMCPClient:
    """httpx.AsyncClient stand-in for an MCP server (Streamable HTTP).

    Routes on the JSON-RPC method in the request body:
      initialize               -> serverInfo result (+ Mcp-Session-Id header)
      notifications/initialized-> 202 empty
      tools/list               -> `tools` (MCP shape)
      tools/call               -> `call_result` (MCP result object)

    `posts` records (url, json_body, headers) for assertions.
    """

    def __init__(
        self,
        tools: Optional[List[dict]] = None,
        call_result: Optional[dict] = None,
        call_result_sse: bool = False,
        session_id: Optional[str] = "sess-123",
        *args,
        **kwargs,
    ):
        self.tools = tools if tools is not None else []
        self.call_result = call_result
        self.call_result_sse = call_result_sse
        self.session_id = session_id
        self.posts: List[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json, dict(headers or {})))
        method = json.get("method")
        req = httpx.Request("POST", url)

        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "serverInfo": {"name": "fake-server", "version": "1.0"},
                    },
                },
                headers={"Mcp-Session-Id": self.session_id} if self.session_id else {},
                request=req,
            )

        if method == "notifications/initialized":
            return httpx.Response(202, request=req)

        if method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": json["id"],
                "result": {"tools": self.tools},
            }
            if self.call_result_sse:
                return sse_response(200, f"data: {_json.dumps(payload)}\n\n")
            return json_response(200, payload)

        if method == "tools/call":
            payload = {
                "jsonrpc": "2.0",
                "id": json["id"],
                "result": self.call_result if self.call_result is not None else {},
            }
            if self.call_result_sse:
                return sse_response(200, f"data: {_json.dumps(payload)}\n\n")
            return json_response(200, payload)

        raise ValueError(f"FakeMCPClient: unexpected method {method!r}")
