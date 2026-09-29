"""Per-persona voice effects: speaker coloration, occasional glitches, distance.

A persona opts in with a `voice_fx.yaml` next to its `prompt.md` (a separate
file on purpose: the persona editor rewrites prompt.md from its own fields,
and a rename or clone moves/copies the whole directory, so this file follows
the persona without any editor support):

    effect: metallic_speaker   # a PRESETS name; empty/absent = no coloration
    glitch_chance: 0.1         # share of sentences that crackle at random (0-1)
    distance: near             # default distance, a DISTANCES name

Every synthesized sentence then runs through one ffmpeg filter chain, built
by `build_chain()`:

    coloration (effect) -> glitch burst (sometimes) -> level -> distance

- **glitch** — random per sentence (`glitch_chance`), or on demand when the
  LLM writes `(glitch)` (expressive speech teaches it to personas with an
  effect). The tag is stripped from the TTS text: the engine never sees it.
- **distance** — the persona's default, or taken from the sentence's voice
  direction when it says where the voice comes from ("{calling, from far
  away}", "{muffled, from inside a pocket}"), see `distance_from_direction()`.
  Distance comes after the level normalization, so far really is quieter.

Needs an `ffmpeg` binary on PATH. Without one the feature is off: audio
passes through unchanged, with one warning, and the LLM is not told about
distances or glitches. A failing ffmpeg run also passes the clip through —
effects must never cost a sentence.
"""

import asyncio
import base64
import logging
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

VOICE_FX_FILENAME = "voice_fx.yaml"

# Speaker coloration, tuned by ear on a cloned voice (A/B listening test).
PRESETS = {
    # cheap little speaker, driven into soft clipping
    "small_speaker": "highpass=f=450,lowpass=f=4200,volume=14dB,asoftclip=type=atan,highpass=f=350",
    # + very short echoes: the ringing of a small metal housing
    "metallic_speaker": (
        "highpass=f=450,lowpass=f=4200,aecho=0.8:0.6:5|11:0.35|0.25,"
        "volume=12dB,asoftclip=type=atan"
    ),
    # + flanger: synthetic shimmer
    "metallic_flanger": (
        "highpass=f=400,lowpass=f=5000,flanger=delay=1.5:depth=1.5:regen=40:speed=0.3,"
        "aecho=0.8:0.6:5|11:0.3|0.2,volume=10dB,asoftclip=type=atan"
    ),
    # subtle everyday version of metallic_speaker
    "metallic_light": (
        "highpass=f=250,lowpass=f=6000,aecho=0.8:0.5:5|11:0.2|0.12,"
        "volume=6dB,asoftclip=type=atan"
    ),
}

# The occasional malfunction: bit-crushed, band-limited, hard-clipped.
GLITCH_CHAIN = (
    "highpass=f=500,lowpass=f=3800,acrusher=bits=5:mode=log:aa=1:samples=2,"
    "volume=18dB,asoftclip=type=hard,highpass=f=400"
)

# Loudness before distance, so every distance starts from the same level
# (and the -1.5 dBTP ceiling tames the clipping presets).
_LEVEL = "loudnorm=I=-16:TP=-1.5"

# Where the voice is, relative to the listener: level, air absorption (high
# cut) and room (multi-tap echo as a light reverb). "near" is the neutral
# default and adds nothing. The gains are set so the result lands about
# room -3.5, far -8, muffled -8 LU below near (measured with ebur128; the
# high cuts cost part of the loudness; aecho's out_gain stays 0.9, it scales
# the whole signal, not just the echoes).
DISTANCES = {
    "close": "bass=g=3:f=150,volume=-2dB",  # warmer, not louder: headroom for the boost
    "near": "",
    "room": "lowpass=f=9000,aecho=0.8:0.9:23|41|67:0.18|0.12|0.08,volume=-1dB",
    "far": "lowpass=f=5000,aecho=0.8:0.9:37|71|113|167:0.3|0.24|0.18|0.12,volume=-6dB",
    "muffled": "lowpass=f=1100,aecho=0.8:0.9:19|43:0.2|0.12,volume=-4dB",
}

# Phrases in a voice direction that say where the voice comes from. Checked
# in this order (the first distance with a match wins), case-insensitive.
_DISTANCE_PHRASES = {
    "muffled": ("muffled", "next room", "other room", "behind a door", "behind the door",
                "through the wall", "through a wall", "pocket", "inside a", "from upstairs",
                "from downstairs", "under a", "in a box", "in a cupboard"),
    "far": ("far away", "far off", "from afar", "distant", "in the distance", "from a distance",
            "across the", "from the far", "echoing"),
    "room": ("from the corner", "a few steps away", "across from", "from the doorway"),
    "close": ("right next to", "in your ear", "up close", "very close", "close to the mic",
              "close-up", "intimate"),
}

# Sound-effect events: in the text for the LLM, removed before TTS.
FX_EVENTS = ("glitch",)
_FX_EVENT = re.compile(r"[({]\s*(glitch|glitches|glitching)\s*[)}]", re.IGNORECASE)

_warned_missing_ffmpeg = False


@dataclass(frozen=True)
class VoiceFx:
    effect: str = ""
    glitch_chance: float = 0.0
    distance: str = "near"


def _ffmpeg() -> Optional[str]:
    """Path of the ffmpeg binary (one place, so tests can pin it)."""
    return shutil.which("ffmpeg")


def available() -> bool:
    """Is there an ffmpeg to run the chains with?"""
    return _ffmpeg() is not None


def load_voice_fx(persona_dir: Optional[Path]) -> Optional[VoiceFx]:
    """The persona's voice_fx.yaml, or None (no file, unreadable, or nothing set).

    Read on every request (like memories.txt): a hand edit applies to the
    next sentence without a restart. Invalid values are logged and replaced
    by the neutral default, never raised — a typo must not mute a persona.
    """
    if persona_dir is None:
        return None
    path = Path(persona_dir) / VOICE_FX_FILENAME
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Voice FX: unreadable %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Voice FX: %s is not a mapping; ignoring it", path)
        return None

    effect = str(data.get("effect") or "").strip()
    if effect and effect not in PRESETS:
        logger.warning("Voice FX: unknown effect %r in %s (known: %s)", effect, path, ", ".join(PRESETS))
        effect = ""
    try:
        chance = float(data.get("glitch_chance") or 0.0)
    except (TypeError, ValueError):
        logger.warning("Voice FX: glitch_chance %r in %s is not a number", data.get("glitch_chance"), path)
        chance = 0.0
    chance = min(max(chance, 0.0), 1.0)
    distance = str(data.get("distance") or "near").strip().lower()
    if distance not in DISTANCES:
        logger.warning("Voice FX: unknown distance %r in %s (known: %s)", distance, path, ", ".join(DISTANCES))
        distance = "near"

    fx = VoiceFx(effect=effect, glitch_chance=chance, distance=distance)
    return fx if fx != VoiceFx() else None


def write_voice_fx(persona_dir: Path, fx: VoiceFx) -> None:
    """Save the persona editor's values; the neutral setting removes the file.

    Values must already be valid (the router 422s unknown names). Raises
    OSError like the other persona file writers.
    """
    path = Path(persona_dir) / VOICE_FX_FILENAME
    if fx == VoiceFx():
        path.unlink(missing_ok=True)
        return
    body = yaml.dump(
        {"effect": fx.effect, "glitch_chance": round(fx.glitch_chance, 3), "distance": fx.distance},
        default_flow_style=False, sort_keys=False,
    )
    path.write_text(
        "# Voice effects (app/services/voice_fx.py), written by the persona editor.\n"
        f"# effect: {' | '.join(PRESETS)} | (empty = none)\n"
        f"# distance: {' | '.join(DISTANCES)}\n" + body,
        encoding="utf-8",
    )


def take_fx_events(text: str) -> tuple[bool, str]:
    """(did the text ask for a glitch, text without the fx tags)."""
    if _FX_EVENT.search(text) is None:
        return False, text
    return True, re.sub(r"[ \t]{2,}", " ", _FX_EVENT.sub(" ", text)).strip()


def distance_from_direction(direction: Optional[str]) -> Optional[str]:
    """The distance a voice direction describes, or None when it names none."""
    if not direction:
        return None
    lowered = direction.lower()
    for distance, phrases in _DISTANCE_PHRASES.items():
        if any(phrase in lowered for phrase in phrases):
            return distance
    return None


def build_chain(
    fx: Optional[VoiceFx],
    *,
    direction: Optional[str] = None,
    glitch_requested: bool = False,
    rng: random.Random | None = None,
) -> Optional[str]:
    """The ffmpeg -af chain for one sentence, or None when nothing applies.

    A direction's distance overrides the persona default (and works for
    personas without a voice_fx.yaml too); a glitch needs a persona effect
    (a speaker that can malfunction) and fires on request or by chance.
    """
    effect = fx.effect if fx else ""
    distance = distance_from_direction(direction) or (fx.distance if fx else "near")
    glitch = bool(effect) and (
        glitch_requested or (fx.glitch_chance > 0 and (rng or random).random() < fx.glitch_chance)
    )
    if not effect and not DISTANCES[distance]:
        return None
    stages = [PRESETS[effect]] if effect else []
    if glitch:
        stages.append(GLITCH_CHAIN)
    stages.append(_LEVEL)
    if DISTANCES[distance]:
        stages.append(DISTANCES[distance])
    return ",".join(stages)


def prompt_note(fx: Optional[VoiceFx]) -> str:
    """Per-persona addition to the markup rules (expressive speech)."""
    lines = [
        "- Distance: a direction may also say where your voice comes from, for example "
        "{calling, from far away}, {muffled, from inside a pocket} or {right next to you, "
        "quietly}. Only when it fits the scene."
    ]
    if fx and fx.effect:
        lines.append(
            "- Your voice comes out of a small, overdriven speaker. (glitch) makes it crackle "
            "and distort for a moment: use it rarely, when you are furious, shocked or "
            "malfunctioning."
        )
    return "\n".join(lines)


async def apply_chain(audio_base64: str, chain: str, sample_rate: int, timeout: float = 20.0) -> str:
    """Run a WAV (base64) through ffmpeg; the original on any failure."""
    global _warned_missing_ffmpeg
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        if not _warned_missing_ffmpeg:
            logger.warning("Voice FX: no ffmpeg on PATH; voice effects are disabled")
            _warned_missing_ffmpeg = True
        return audio_base64
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-af", chain, "-ar", str(sample_rate), "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        logger.warning("Voice FX: ffmpeg did not start (%s); sending the clip without effects", exc)
        return audio_base64
    try:
        out, err = await asyncio.wait_for(proc.communicate(base64.b64decode(audio_base64)), timeout)
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        if proc.returncode is None:
            proc.kill()
        logger.warning("Voice FX: ffmpeg failed (%s); sending the clip without effects", exc)
        return audio_base64
    if proc.returncode != 0 or not out:
        logger.warning("Voice FX: ffmpeg exited %s: %s", proc.returncode, err.decode(errors="replace")[:300])
        return audio_base64
    return base64.b64encode(out).decode("ascii")
