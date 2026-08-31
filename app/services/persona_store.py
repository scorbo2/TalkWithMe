"""File-based persona storage — the "Personas directory".

Each persona lives in its own subdirectory of the configured Personas
directory (``general.personas_directory`` in settings.yaml, default
``<project root>/Personas``)::

    Personas/
      Alex/
        prompt.md       YAML frontmatter (name, description, router_hints,
                        avatar_color, allow_tool_calls) + system prompt body
        language.txt    2-letter code for the reference audio (optional)
        ref.wav         Reference audio for TTS voice cloning (optional)
        ref.txt         Transcript of ref.wav (optional)
        image.png       Avatar image (optional; png/jpg/jpeg/gif/webp)

This module is framework-agnostic on purpose: it reads and writes files
and knows nothing about FastAPI, the config cache, or the frontend. The
router (app/routers/personas.py) translates HTTP into these operations
and refreshes the in-memory persona cache afterwards. The directory on
disk is the source of truth.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from pydantic import ValidationError

from app.config import Persona

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File layout constants
# ---------------------------------------------------------------------------

PROMPT_FILENAME = "prompt.md"
LANGUAGE_FILENAME = "language.txt"
REFERENCE_AUDIO_FILENAME = "ref.wav"
TRANSCRIPT_FILENAME = "ref.txt"
IMAGE_BASENAME = "image"
DEFAULT_LANGUAGE = "en"

# The browser can decode all of these natively; anything else is rejected
# at upload time (422) and skipped at migration time (warning).
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
# Reference audio is wav-only: the TTS servers we support resample from
# wav, and a single extension keeps the on-disk layout unambiguous.
AUDIO_EXTENSION = ".wav"

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class PersonaStorageError(RuntimeError):
    """Fatal problem with the Personas directory itself (not one persona)."""


class PersonaMigrationError(PersonaStorageError):
    """The one-time personas.yaml -> directory migration failed fatally."""


# ---------------------------------------------------------------------------
# prompt.md frontmatter
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> Tuple[dict, str]:
    """Split a prompt.md into (frontmatter dict, system prompt body).

    Frontmatter is optional: the file must start with a lone ``---`` line
    and close with another ``---`` line. Anything malformed degrades to
    ``({}, whole file)`` — a broken prompt.md should not take the app down
    at startup; it just loses its structured fields.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip("\n")
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() != "---":
            continue
        try:
            data = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError as exc:
            logger.warning("Malformed prompt.md frontmatter; ignoring it: %s", exc)
            return {}, text.strip("\n")
        if not isinstance(data, dict):
            logger.warning("prompt.md frontmatter is not a mapping; ignoring it")
            return {}, text.strip("\n")
        return data, "\n".join(lines[end + 1:]).strip("\n")
    # Opening delimiter but no closing one: treat the whole file as body.
    return {}, text.strip("\n")


def build_prompt_md(
    dir_name: str,
    *,
    name: str,
    description: str,
    router_hints: str,
    avatar_color: str,
    allow_tool_calls: bool,
    system_prompt: str,
) -> str:
    """Serialize a persona's frontmatter + system prompt for prompt.md.

    The ``name`` field is written only when it differs from the directory
    name — the directory name is the fallback identity, so duplicating it
    would be noise. (This is also how names with directory-hostile
    characters keep working: the dir is "OBrien", the frontmatter says
    "O'Brien".)
    """
    frontmatter: dict = {}
    if name != dir_name:
        frontmatter["name"] = name
    frontmatter["description"] = description
    frontmatter["router_hints"] = router_hints
    frontmatter["avatar_color"] = avatar_color
    frontmatter["allow_tool_calls"] = bool(allow_tool_calls)
    dumped = yaml.dump(
        frontmatter, default_flow_style=False, sort_keys=False, allow_unicode=True
    ).strip()
    return f"---\n{dumped}\n---\n\n{system_prompt}\n"


# ---------------------------------------------------------------------------
# Directory names
# ---------------------------------------------------------------------------

_DIRNAME_STRIP_PATTERN = re.compile(r"[^a-zA-Z0-9 _-]")


def sanitize_persona_dirname(name: str) -> str:
    """Reduce a persona name to a safe directory name.

    Same allowed character set as chat room names (letters, numbers,
    spaces, hyphens, underscores); everything else is stripped. May
    return "" — the caller decides what that means (422 for a new
    persona, a fallback for migrated legacy data).
    """
    return _DIRNAME_STRIP_PATTERN.sub("", name)


def unique_persona_dirname(root: Path, base: str) -> str:
    """Pick ``base``, or the first free ``base_2``, ``base_3``, ... in root.

    Suffixes start at 2 and use the same ``Name_2`` convention as the
    clone endpoint, so a collision already reads as a duplicate.
    """
    candidate = base
    suffix = 2
    while (root / candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


# ---------------------------------------------------------------------------
# Loading (read-only; never mutates the directory)
# ---------------------------------------------------------------------------

def read_language_file(persona_dir: Path, persona_name: str) -> str:
    """Read the 2-letter language code, defaulting (with a warning) to 'en'."""
    path = persona_dir / LANGUAGE_FILENAME
    if not path.is_file():
        logger.warning(
            "Persona %s has no %s; defaulting to '%s'",
            persona_name, LANGUAGE_FILENAME, DEFAULT_LANGUAGE,
        )
        return DEFAULT_LANGUAGE
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        logger.warning(
            "Persona %s: unreadable %s (%s); defaulting to '%s'",
            persona_name, LANGUAGE_FILENAME, exc, DEFAULT_LANGUAGE,
        )
        return DEFAULT_LANGUAGE
    if len(value) != 2:
        logger.warning(
            "Persona %s has invalid language code %r in %s; defaulting to '%s'",
            persona_name, value, LANGUAGE_FILENAME, DEFAULT_LANGUAGE,
        )
        return DEFAULT_LANGUAGE
    return value


def find_avatar_file(persona_dir: Path, persona_name: str) -> Optional[Path]:
    """Locate the persona's avatar image (image.png, image.webp, ...)."""
    candidates = sorted(
        path
        for path in persona_dir.glob(f"{IMAGE_BASENAME}.*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "Persona %s has multiple image files; using %s",
            persona_name, candidates[0].name,
        )
    return candidates[0]


def load_persona_from_dir(persona_dir: Path) -> Persona:
    """Build a Persona from one subdirectory of the Personas directory.

    Raises PersonaStorageError when the directory has no readable
    prompt.md — the scanner (scan_personas_directory) logs and skips such
    directories rather than failing the whole load.
    """
    prompt_path = persona_dir / PROMPT_FILENAME
    if not prompt_path.is_file():
        raise PersonaStorageError(f"missing {PROMPT_FILENAME}")
    try:
        text = prompt_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PersonaStorageError(f"unreadable {PROMPT_FILENAME}: {exc}") from exc

    frontmatter, system_prompt = parse_frontmatter(text)
    name = str(frontmatter.get("name") or "").strip() or persona_dir.name

    avatar_path = find_avatar_file(persona_dir, name)

    audio_path = persona_dir / REFERENCE_AUDIO_FILENAME
    reference_audio = str(audio_path) if audio_path.is_file() else None

    transcript_path = persona_dir / TRANSCRIPT_FILENAME
    reference_transcript: Optional[str] = None
    if transcript_path.is_file():
        try:
            transcript = transcript_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Persona %s: unreadable %s: %s", name, TRANSCRIPT_FILENAME, exc)
            transcript = ""
        if transcript.strip():
            reference_transcript = str(transcript_path)
        else:
            # An empty transcript makes TTS voice cloning meaningless, so
            # the persona reports as not-TTS-capable instead of silently
            # synthesizing with an empty prompt.
            logger.warning(
                "Persona %s has an empty %s; persona will be treated as not TTS-capable",
                name, TRANSCRIPT_FILENAME,
            )

    return Persona(
        name=name,
        description=str(frontmatter.get("description") or ""),
        system_prompt=system_prompt,
        router_hints=str(frontmatter.get("router_hints") or ""),
        avatar_color=str(frontmatter.get("avatar_color") or "#888888"),
        avatar_image=str(avatar_path) if avatar_path else None,
        reference_audio=reference_audio,
        reference_audio_transcript=reference_transcript,
        reference_audio_language=read_language_file(persona_dir, name),
        allow_tool_calls=bool(frontmatter.get("allow_tool_calls") or False),
        persona_dir=persona_dir,
    )


def scan_personas_directory(root: Path) -> List[Persona]:
    """Load every persona subdirectory under root, in directory-name order.

    Unrecognized top-level files are ignored (they may be OS junk like
    .DS_Store or editor swap files). Subdirectories that fail to load
    are skipped with a warning — one broken persona should not blind the
    rest.
    """
    if not root.is_dir():
        return []
    personas: List[Persona] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        try:
            personas.append(load_persona_from_dir(entry))
        except PersonaStorageError as exc:
            logger.warning("Skipping persona directory %s: %s", entry.name, exc)
    return personas


# ---------------------------------------------------------------------------
# Writing (used by the router for create/update and by the migration)
# ---------------------------------------------------------------------------

def write_prompt_md(
    persona_dir: Path,
    *,
    name: str,
    description: str,
    router_hints: str,
    avatar_color: str,
    allow_tool_calls: bool,
    system_prompt: str,
) -> None:
    (persona_dir / PROMPT_FILENAME).write_text(
        build_prompt_md(
            persona_dir.name,
            name=name,
            description=description,
            router_hints=router_hints,
            avatar_color=avatar_color,
            allow_tool_calls=allow_tool_calls,
            system_prompt=system_prompt,
        ),
        encoding="utf-8",
    )


def write_language_file(persona_dir: Path, language: str) -> None:
    (persona_dir / LANGUAGE_FILENAME).write_text(language, encoding="utf-8")


def write_transcript_file(persona_dir: Path, text: str) -> None:
    """Write the transcript, or delete ref.txt when the text is blank.

    The file is stored stripped: a transcript that is only whitespace is
    the same as no transcript, and keeping the file would hide that from
    the UI (the detail endpoint reports file contents).
    """
    path = persona_dir / TRANSCRIPT_FILENAME
    stripped = text.strip()
    if not stripped:
        if path.exists():
            path.unlink()
        return
    path.write_text(stripped, encoding="utf-8")


def write_avatar_file(persona_dir: Path, data: bytes, extension: str) -> Path:
    """Store a new avatar as image.<ext>, replacing any existing avatar."""
    remove_avatar_file(persona_dir)
    target = persona_dir / f"{IMAGE_BASENAME}{extension.lower()}"
    target.write_bytes(data)
    return target


def write_reference_audio_file(persona_dir: Path, data: bytes) -> Path:
    target = persona_dir / REFERENCE_AUDIO_FILENAME
    target.write_bytes(data)
    return target


def remove_avatar_file(persona_dir: Path) -> bool:
    """Delete the persona's avatar image if present. Returns True if removed."""
    removed = False
    for path in persona_dir.glob(f"{IMAGE_BASENAME}.*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            path.unlink()
            removed = True
    return removed


def remove_reference_audio_file(persona_dir: Path) -> bool:
    """Delete the persona's ref.wav if present. Returns True if removed."""
    path = persona_dir / REFERENCE_AUDIO_FILENAME
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Legacy personas.yaml: parsing + one-time migration
# ---------------------------------------------------------------------------

def load_personas_yaml(path: Path) -> List[Persona]:
    """Parse a legacy personas.yaml into Persona objects.

    Exists for the one-time startup migration (and its tests) — the live
    app reads personas from the directory and never from this file again.
    Raises on malformed content; the migration turns that into a fatal error.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise PersonaMigrationError(
            f"{path.name} is malformed: expected a top-level mapping with a 'personas' list"
        )
    entries = raw.get("personas", [])
    if not isinstance(entries, list):
        raise PersonaMigrationError(
            f"{path.name} is malformed: 'personas' must be a list"
        )
    personas: List[Persona] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PersonaMigrationError(
                f"{path.name} is malformed: each persona must be a mapping"
            )
        if "language" in entry and "reference_audio_language" not in entry:
            logger.info(
                "Persona '%s': migrating legacy 'language' key to 'reference_audio_language'",
                entry.get("name", "<unknown>"),
            )
            entry = {**entry, "reference_audio_language": entry.pop("language")}
        personas.append(Persona(**entry))
    return personas


def migrate_from_legacy_yaml(yaml_path: Path, root: Path) -> None:
    """One-time migration: personas.yaml -> per-persona subdirectories.

    On success ``yaml_path`` is renamed to ``personas.yaml.bak`` (never
    deleted) so the migration can never run twice.

    Error policy (docs/feature_persona_autodiscovery.md):
      * fatal (malformed YAML, unreadable file, unwritable directory,
        disk full) -> raise PersonaMigrationError with the YAML left
        untouched and any partially created directory removed
        best-effort, so the next startup retries cleanly;
      * minor (missing referenced files, unsupported image/audio
        formats) -> log a warning and continue without the file.
    """
    logger.info("Persona migration in progress: %s -> %s", yaml_path, root)
    try:
        personas = load_personas_yaml(yaml_path)
    except PersonaMigrationError as exc:
        raise _abort_migration(yaml_path, root, str(exc)) from exc
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise _abort_migration(yaml_path, root, f"cannot parse {yaml_path.name}: {exc}") from exc

    try:
        root.mkdir(parents=True, exist_ok=True)
        for persona in personas:
            _migrate_one_persona(persona, root)
    except PersonaMigrationError:
        raise
    except OSError as exc:
        raise _abort_migration(yaml_path, root, f"disk error while writing personas: {exc}") from exc

    backup_path = yaml_path.with_name(yaml_path.name + ".bak")
    try:
        yaml_path.rename(backup_path)
    except OSError as exc:
        # The directory is complete and the YAML is intact, so the next
        # startup will take the (noisy but safe) "both exist" path. Do not
        # rmtree the finished directory here.
        raise _abort_migration(
            yaml_path, root,
            f"cannot rename {yaml_path.name} to {backup_path.name}: {exc}",
            remove_partial_dir=False,
        ) from exc
    logger.info(
        "Persona migration complete: %s -> %s (%d persona%s)",
        yaml_path.name, root, len(personas), "" if len(personas) == 1 else "s",
    )


def _migrate_one_persona(persona: Persona, root: Path) -> None:
    """Migrate one legacy persona into its own subdirectory of root."""
    base_name = sanitize_persona_dirname(persona.name)
    if not base_name:
        # A name with no directory-usable characters still needs a home;
        # the frontmatter `name` field keeps the real one.
        logger.warning(
            "Migration: persona name '%s' has no usable directory characters; using 'persona'",
            persona.name,
        )
        base_name = "persona"
    dir_name = unique_persona_dirname(root, base_name)
    persona_dir = root / dir_name

    try:
        persona_dir.mkdir()
        write_prompt_md(
            persona_dir,
            name=persona.name,
            description=persona.description,
            router_hints=persona.router_hints,
            avatar_color=persona.avatar_color,
            allow_tool_calls=persona.allow_tool_calls,
            system_prompt=persona.system_prompt,
        )
        write_language_file(persona_dir, persona.reference_audio_language)
    except OSError as exc:
        raise PersonaMigrationError(
            f"persona '{persona.name}': cannot write {persona_dir}: {exc}"
        ) from exc

    if persona.avatar_image:
        source = Path(persona.avatar_image)
        extension = source.suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            logger.warning(
                "Migration: ignoring unsupported image file '%s' for persona %s. "
                "Only png, jpg, jpeg, gif, webp images are supported.",
                source.name, persona.name,
            )
        else:
            _migrate_file(persona, source, persona_dir / f"image{extension}", "avatar image")

    if persona.reference_audio:
        source = Path(persona.reference_audio)
        if source.suffix.lower() != AUDIO_EXTENSION:
            logger.warning(
                "Migration: ignoring unsupported audio file '%s' for persona %s. "
                "Only wav audio is supported.",
                source.name, persona.name,
            )
        else:
            _migrate_file(
                persona, source, persona_dir / REFERENCE_AUDIO_FILENAME, "reference audio"
            )

    if persona.reference_audio_transcript:
        source = Path(persona.reference_audio_transcript)
        try:
            transcript = source.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning(
                "Migration: transcript file '%s' for persona %s could not be read (%s); skipped.",
                source.name, persona.name, exc,
            )
        else:
            if transcript:
                try:
                    (persona_dir / TRANSCRIPT_FILENAME).write_text(transcript, encoding="utf-8")
                except OSError as exc:
                    raise PersonaMigrationError(
                        f"persona '{persona.name}': cannot write {TRANSCRIPT_FILENAME}: {exc}"
                    ) from exc


def _migrate_file(persona: Persona, source: Path, target: Path, kind: str) -> None:
    """Copy one referenced legacy file into the persona directory.

    A missing or unreadable source is a *minor* error (warn + skip); a
    write failure is *fatal* (raises PersonaMigrationError).
    """
    try:
        data = source.read_bytes()
    except OSError as exc:
        logger.warning(
            "Migration: %s file '%s' for persona %s could not be read (%s); skipped.",
            kind, source.name, persona.name, exc,
        )
        return
    try:
        target.write_bytes(data)
    except OSError as exc:
        raise PersonaMigrationError(
            f"persona '{persona.name}': cannot write {target.name}: {exc}"
        ) from exc


def _abort_migration(
    yaml_path: Path, root: Path, reason: str, *, remove_partial_dir: bool = True
) -> PersonaMigrationError:
    """Log a fatal migration failure and roll the directory state back.

    The YAML file is ALWAYS left untouched — it is the only guaranteed
    copy of the data until the rename succeeds.
    """
    logger.error("Persona migration failed: %s", reason)
    logger.error(
        "%s was left untouched. Fix the problem and restart to retry the migration.",
        yaml_path.name,
    )
    if remove_partial_dir and root.exists():
        try:
            shutil.rmtree(root)
            logger.info("Removed partially created personas directory: %s", root)
        except OSError as cleanup_exc:
            logger.warning(
                "Could not remove partially created personas directory %s: %s (left in place)",
                root, cleanup_exc,
            )
    return PersonaMigrationError(f"persona migration aborted: {reason}")
