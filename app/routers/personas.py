"""Personas router — list, create, update, delete, clone personas and serve
avatar / reference audio files.

Personas live in per-persona subdirectories of the configured Personas
directory (see app/services/persona_store.py); the directory on disk is
the source of truth. Create/update are multipart/form-data so the editor
can submit text fields and file uploads in a single request. Every
mutation refreshes the in-memory persona cache via set_personas_cache()
— skipping that step is how the UI ends up stale until the next restart.
"""

import logging
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app.config import (
    ChatRoom,
    ChatRoomsConfig,
    Persona,
    PersonasConfig,
    get_chatrooms,
    get_personas,
    get_personas_directory,
    save_chatrooms,
    set_personas_cache,
)
from app.models import PersonaDetailResponse, PersonaResponse
from app.services import persona_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/personas", tags=["personas"])


# ---------------------------------------------------------------------------
# Chat-room cascades (kept in sync with the data model — see AGENTS.md)
# ---------------------------------------------------------------------------

def _cascade_persona_rename(old_name: str, new_name: str) -> None:
    """Update persona name references in all chat rooms.

    When a persona is renamed, every chat room that references it
    must be updated to use the new name so assignments stay valid.
    """
    config = get_chatrooms()
    if not config.chat_rooms:
        return
    updated = []
    for room in config.chat_rooms:
        new_names = [new_name if p == old_name else p for p in room.persona_names]
        updated.append(ChatRoom(name=room.name, persona_names=new_names))
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated))
    logger.info("Cascaded persona rename '%s' -> '%s' to chat rooms", old_name, new_name)


def _cascade_persona_delete(persona_name: str) -> None:
    """Remove a persona from all chat rooms.

    When a persona is deleted, it must be removed from every chat room
    that had it assigned to avoid dangling references.
    """
    config = get_chatrooms()
    if not config.chat_rooms:
        return
    updated = []
    for room in config.chat_rooms:
        new_names = [p for p in room.persona_names if p != persona_name]
        updated.append(ChatRoom(name=room.name, persona_names=new_names))
    save_chatrooms(ChatRoomsConfig(chat_rooms=updated))
    logger.info("Cascaded persona delete '%s' from chat rooms", persona_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _remove_persona_dir(persona_dir: Path) -> None:
    """Best-effort removal of a persona directory (create-failure cleanup)."""
    try:
        if persona_dir.is_dir():
            shutil.rmtree(persona_dir)
    except OSError as exc:
        logger.warning("Could not remove persona directory %s: %s", persona_dir, exc)


def _validate_image_upload(upload: UploadFile) -> Tuple[str, bytes]:
    """Validate an avatar image upload; return (extension, content) or raise 422."""
    filename = upload.filename or ""
    extension = Path(filename).suffix.lower()
    if extension not in persona_store.IMAGE_EXTENSIONS:
        allowed = ", ".join(ext.lstrip(".") for ext in persona_store.IMAGE_EXTENSIONS)
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported avatar image file '{filename}'. Allowed: {allowed}.",
        )
    content = upload.file.read()
    if len(content) > persona_store.MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Avatar image exceeds the {persona_store.MAX_IMAGE_BYTES // (1024 * 1024)}MB limit.",
        )
    return extension, content


def _validate_audio_upload(upload: UploadFile) -> bytes:
    """Validate a reference audio upload; return content or raise 422."""
    filename = upload.filename or ""
    if Path(filename).suffix.lower() != persona_store.AUDIO_EXTENSION:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported reference audio file '{filename}'. Only wav audio is supported.",
        )
    content = upload.file.read()
    if len(content) > persona_store.MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Reference audio exceeds the {persona_store.MAX_AUDIO_BYTES // (1024 * 1024)}MB limit.",
        )
    return content


def _apply_persona_fields(
    persona_dir: Path,
    *,
    name: str,
    description: str,
    system_prompt: str,
    router_hints: str,
    avatar_color: str,
    reference_audio_language: str,
    allow_tool_calls: bool,
    reference_audio_transcript: str,
    image: Optional[Tuple[str, bytes]],
    audio: Optional[bytes],
    remove_image: bool,
    remove_audio: bool,
) -> None:
    """Write one create/update payload into a persona directory.

    Uploads are validated (422) BEFORE this is called, so this function
    only raises OSError. A failure mid-way can leave the persona
    half-updated; with local disk and small files that is acceptable
    (the previous values are recoverable from a backup).
    """
    persona_store.write_prompt_md(
        persona_dir,
        name=name,
        description=description,
        router_hints=router_hints,
        avatar_color=avatar_color,
        allow_tool_calls=allow_tool_calls,
        system_prompt=system_prompt,
    )
    persona_store.write_language_file(persona_dir, reference_audio_language)
    persona_store.write_transcript_file(persona_dir, reference_audio_transcript)
    if image is not None:
        persona_store.write_avatar_file(persona_dir, image[1], image[0])
    elif remove_image:
        persona_store.remove_avatar_file(persona_dir)
    if audio is not None:
        persona_store.write_reference_audio_file(persona_dir, audio)
    elif remove_audio:
        persona_store.remove_reference_audio_file(persona_dir)


def _read_uploads(
    avatar_image: Optional[UploadFile],
    reference_audio: Optional[UploadFile],
) -> Tuple[Optional[Tuple[str, bytes]], Optional[bytes]]:
    """Validate both optional file uploads (or treat them as absent)."""
    image = _validate_image_upload(avatar_image) if avatar_image and avatar_image.filename else None
    audio = _validate_audio_upload(reference_audio) if reference_audio and reference_audio.filename else None
    return image, audio


def _validate_name(name: str, reserved_check: bool = True) -> str:
    """Shared name rules for create/update; returns the stripped name."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name may not be blank")
    if reserved_check and name.lower() == "user":
        raise HTTPException(
            status_code=422,
            detail="'user' is a reserved persona name and cannot be used.",
        )
    return name


def _to_response(p: Persona) -> PersonaResponse:
    return PersonaResponse(
        name=p.name,
        description=p.description,
        avatar_color=p.avatar_color,
        avatar_image=bool(p.avatar_image),
        tts_capable=p.tts_capable,
    )


def _to_detail(p: Persona) -> PersonaDetailResponse:
    transcript: Optional[str] = None
    if p.reference_audio_transcript:
        path = Path(p.reference_audio_transcript)
        if path.is_file():
            transcript = path.read_text(encoding="utf-8", errors="replace")
    return PersonaDetailResponse(
        name=p.name,
        description=p.description,
        system_prompt=p.system_prompt,
        router_hints=p.router_hints,
        avatar_color=p.avatar_color,
        avatar_image=bool(p.avatar_image),
        reference_audio=bool(p.reference_audio),
        reference_audio_transcript=transcript,
        reference_audio_language=p.reference_audio_language,
        allow_tool_calls=p.allow_tool_calls,
        tts_capable=p.tts_capable,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=List[PersonaResponse])
def list_personas():
    """Return all configured personas with TTS capability flags."""
    return [_to_response(p) for p in get_personas().personas]


@router.post("", response_model=PersonaDetailResponse, status_code=201)
def create_persona(
    name: str = Form(..., max_length=25),
    description: str = Form("", max_length=30),
    system_prompt: str = Form(..., min_length=1, max_length=8192),
    router_hints: str = Form(..., min_length=1, max_length=256),
    avatar_color: str = Form("#FF0000"),
    reference_audio_language: str = Form("en", min_length=2, max_length=2),
    allow_tool_calls: bool = Form(False),
    reference_audio_transcript: str = Form("", max_length=16384),
    avatar_image: Optional[UploadFile] = File(None),
    remove_avatar_image: bool = Form(False),
    reference_audio: Optional[UploadFile] = File(None),
    remove_reference_audio: bool = Form(False),
):
    """Create a new persona in the Personas directory (multipart/form-data).

    The remove_* flags are accepted for API symmetry with update but are
    ignored here: a brand-new directory has nothing to remove.
    """
    name = _validate_name(name)
    if any(p.name.lower() == name.lower() for p in get_personas().personas):
        raise HTTPException(status_code=409, detail=f"A persona named '{name}' already exists")

    dir_base = persona_store.sanitize_persona_dirname(name)
    if not dir_base:
        raise HTTPException(
            status_code=422,
            detail="Name must contain at least one letter, number, space, hyphen or underscore",
        )

    image, audio = _read_uploads(avatar_image, reference_audio)

    root = get_personas_directory()
    persona_dir = root / persona_store.unique_persona_dirname(root, dir_base)

    try:
        persona_dir.mkdir(parents=True)
        _apply_persona_fields(
            persona_dir,
            name=name,
            description=description or "",
            system_prompt=system_prompt,
            router_hints=router_hints,
            avatar_color=avatar_color,
            reference_audio_language=reference_audio_language,
            allow_tool_calls=allow_tool_calls,
            reference_audio_transcript=reference_audio_transcript,
            image=image,
            audio=audio,
            remove_image=False,
            remove_audio=False,
        )
        persona = persona_store.load_persona_from_dir(persona_dir)
    except OSError as exc:
        _remove_persona_dir(persona_dir)
        logger.error("Failed to create persona '%s' in %s: %s", name, persona_dir, exc)
        raise HTTPException(status_code=500, detail=f"Failed to create persona: {exc}") from exc

    set_personas_cache(PersonasConfig(personas=get_personas().personas + [persona]))
    return _to_detail(persona)


@router.get("/{name}/detail", response_model=PersonaDetailResponse)
def get_persona_detail(name: str):
    """Return full detail for a single persona (all editable fields)."""
    config = get_personas()
    persona = next((p for p in config.personas if p.name == name), None)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona '{name}' not found")
    return _to_detail(persona)


@router.put("/{name}", response_model=PersonaDetailResponse)
def update_persona(
    name: str,
    new_name: str = Form(..., max_length=25, alias="name"),
    description: str = Form("", max_length=30),
    system_prompt: str = Form(..., min_length=1, max_length=8192),
    router_hints: str = Form(..., min_length=1, max_length=256),
    avatar_color: str = Form("#FF0000"),
    reference_audio_language: str = Form("en", min_length=2, max_length=2),
    allow_tool_calls: bool = Form(False),
    reference_audio_transcript: str = Form("", max_length=16384),
    avatar_image: Optional[UploadFile] = File(None),
    remove_avatar_image: bool = Form(False),
    reference_audio: Optional[UploadFile] = File(None),
    remove_reference_audio: bool = Form(False),
):
    """Update an existing persona in its directory (multipart/form-data).

    Renaming rewrites the prompt.md frontmatter and cascades to chat
    rooms; the persona DIRECTORY is never renamed — the directory name
    is only a filesystem concern and renaming it would break any external
    reference to the old path.
    """
    config = get_personas()
    existing = next((p for p in config.personas if p.name == name), None)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Persona '{name}' not found")
    if existing.persona_dir is None or not existing.persona_dir.is_dir():
        raise HTTPException(
            status_code=500,
            detail=f"Persona '{name}' has no directory on disk; cannot update it",
        )

    new_name = _validate_name(new_name)
    if new_name.lower() != name.lower() and any(
        p.name.lower() == new_name.lower() for p in config.personas if p.name != name
    ):
        raise HTTPException(status_code=409, detail=f"A persona named '{new_name}' already exists")

    image, audio = _read_uploads(avatar_image, reference_audio)
    persona_dir = existing.persona_dir

    try:
        _apply_persona_fields(
            persona_dir,
            name=new_name,
            description=description or "",
            system_prompt=system_prompt,
            router_hints=router_hints,
            avatar_color=avatar_color,
            reference_audio_language=reference_audio_language,
            allow_tool_calls=allow_tool_calls,
            reference_audio_transcript=reference_audio_transcript,
            image=image,
            audio=audio,
            remove_image=remove_avatar_image,
            remove_audio=remove_reference_audio,
        )
        updated = persona_store.load_persona_from_dir(persona_dir)
    except OSError as exc:
        logger.error("Failed to update persona '%s' in %s: %s", name, persona_dir, exc)
        raise HTTPException(status_code=500, detail=f"Failed to update persona: {exc}") from exc

    new_list = [updated if p.name == name else p for p in config.personas]
    set_personas_cache(PersonasConfig(personas=new_list))

    # If the persona was renamed, update all chat rooms referencing it
    if new_name != name:
        _cascade_persona_rename(name, new_name)

    return _to_detail(updated)


@router.delete("/{name}", status_code=204)
def delete_persona(name: str):
    """Remove a persona's directory and drop it from all chat rooms."""
    config = get_personas()
    persona = next((p for p in config.personas if p.name == name), None)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona '{name}' not found")
    if persona.persona_dir is not None and persona.persona_dir.is_dir():
        try:
            shutil.rmtree(persona.persona_dir)
        except OSError as exc:
            logger.error("Failed to remove persona directory %s: %s", persona.persona_dir, exc)
            raise HTTPException(status_code=500, detail=f"Failed to delete persona: {exc}") from exc
    else:
        logger.warning("Deleting persona '%s' with no directory on disk", name)
    set_personas_cache(PersonasConfig(personas=[p for p in config.personas if p.name != name]))
    _cascade_persona_delete(name)


@router.post("/{name}/clone", response_model=PersonaDetailResponse, status_code=201)
def clone_persona(name: str):
    """Clone an existing persona, appending a numeric suffix to ensure uniqueness.

    The clone gets its own directory (a copy of the source's files), so
    editing one never affects the other.
    """
    config = get_personas()
    source = next((p for p in config.personas if p.name == name), None)
    if not source:
        raise HTTPException(status_code=404, detail=f"Persona '{name}' not found")
    if source.persona_dir is None or not source.persona_dir.is_dir():
        raise HTTPException(
            status_code=500,
            detail=f"Persona '{name}' has no directory on disk; cannot clone it",
        )

    existing_names = {p.name.lower() for p in config.personas}
    suffix = 2
    while f"{name}_{suffix}".lower() in existing_names:
        suffix += 1
    new_name = f"{name}_{suffix}"

    root = get_personas_directory()
    new_dir = root / persona_store.unique_persona_dirname(
        root, persona_store.sanitize_persona_dirname(new_name)
    )
    try:
        shutil.copytree(source.persona_dir, new_dir)
        # The copy still carries the source's frontmatter; rewrite it with
        # the clone's name (build_prompt_md adds the `name` field when it
        # differs from the directory name).
        persona_store.write_prompt_md(
            new_dir,
            name=new_name,
            description=source.description,
            router_hints=source.router_hints,
            avatar_color=source.avatar_color,
            allow_tool_calls=source.allow_tool_calls,
            system_prompt=source.system_prompt,
        )
        clone = persona_store.load_persona_from_dir(new_dir)
    except OSError as exc:
        _remove_persona_dir(new_dir)
        logger.error("Failed to clone persona '%s': %s", name, exc)
        raise HTTPException(status_code=500, detail=f"Failed to clone persona: {exc}") from exc

    set_personas_cache(PersonasConfig(personas=config.personas + [clone]))
    return _to_detail(clone)


@router.get("/{name}/avatar")
async def get_avatar(name: str):
    """Serve a persona's avatar image file.

    Returns 404 if the persona has no avatar configured or the file
    doesn't exist on disk.
    """
    config = get_personas()
    persona = next((p for p in config.personas if p.name == name), None)
    if not persona or not persona.avatar_image:
        return Response(status_code=404, content="No avatar configured")

    path = Path(persona.avatar_image)
    if not path.exists():
        logger.warning("Avatar file not found for %s: %s", name, persona.avatar_image)
        return Response(status_code=404, content="Avatar file not found")

    return FileResponse(str(path))


@router.get("/{name}/reference-audio")
async def get_reference_audio(name: str):
    """Serve a persona's reference audio file (ref.wav).

    Returns 404 if the persona has no reference audio configured or the
    file doesn't exist on disk.
    """
    config = get_personas()
    persona = next((p for p in config.personas if p.name == name), None)
    if not persona or not persona.reference_audio:
        return Response(status_code=404, content="No reference audio configured")

    path = Path(persona.reference_audio)
    if not path.exists():
        logger.warning("Reference audio file not found for %s: %s", name, persona.reference_audio)
        return Response(status_code=404, content="Reference audio file not found")

    return FileResponse(str(path), media_type="audio/wav")
