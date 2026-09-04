"""Settings router — read and update application settings from settings.yaml."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app import config as app_config
from app.config import AppSettings, LLMSettings, STTConfig, TTSConfig
from app.models import (
    GeneralSettingsResponse,
    LLMSettingsResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    STTSettingsResponse,
    TTSSettingsResponse,
)
from app.services import tts_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


def _to_response(cfg: AppSettings) -> SettingsResponse:
    """Convert internal AppSettings to the API response model."""
    return SettingsResponse(
        llm=LLMSettingsResponse(
            base_url=cfg.llm.base_url,
            model=cfg.llm.model,
            max_tokens=cfg.llm.max_tokens,
            temperature=cfg.llm.temperature,
        ),
        tts=TTSSettingsResponse(
            enabled=cfg.tts.enabled,
            base_url=cfg.tts.base_url,
            timeout=cfg.tts.timeout,
            streaming=cfg.tts.streaming,
            parameters=cfg.tts.parameters,
        ),
        stt=STTSettingsResponse(
            enabled=cfg.stt.enabled,
            base_url=cfg.stt.base_url,
            timeout=cfg.stt.timeout,
        ),
        general=GeneralSettingsResponse(
            persona_name_mentions=cfg.general.persona_name_mentions,
            max_persona_replies=cfg.general.max_persona_replies,
            max_turns_for_context=cfg.general.max_turns_for_context,
            show_tool_calls=cfg.general.show_tool_calls,
            enable_persona_memories=cfg.general.enable_persona_memories,
        ),
    )


@router.get("", response_model=SettingsResponse)
def get_settings():
    """Return current application settings."""
    return _to_response(app_config.get_settings())


def _validate_tts_parameters_if_documented(base_url: Optional[str], parameters: dict) -> None:
    """T7: 422 on garbage tts.parameters — but only when we can actually
    judge them.

    Validation runs only when the cached capabilities document belongs to
    the exact base_url being saved. If the user is switching engines in the
    same save, the cached doc describes the OLD server and is useless for
    judging the new parameters — validation is skipped (T4 makes the switch
    safe by construction: unadvertised fields are never sent). A negative
    or empty cache (server down, no /capabilities) is skipped the same
    way: the save path stays synchronous and offline-safe, and the server's
    own 422 on the first synthesis is the backstop.
    """
    if not parameters or not base_url:
        return
    cached_url, doc = tts_client.cached_capabilities()
    if doc is None or cached_url != base_url:
        return
    error = tts_client.validate_tts_parameters(doc, parameters)
    if error:
        logger.warning("Settings save rejected: invalid tts.parameters: %s", error)
        raise HTTPException(status_code=422, detail=error)


@router.put("", response_model=SettingsResponse)
def update_settings(req: SettingsUpdateRequest):
    """Update application settings and persist to settings.yaml.

    The tts: section is a full replacement; its generic `parameters` map is
    validated against the cached capabilities doc when one is available for
    the saved base_url (see _validate_tts_parameters_if_documented).
    """
    # Normalize exactly the way the config models will (strip whitespace +
    # trailing slashes, blank -> None). The capabilities cache is keyed on
    # the NORMALIZED URL, so the T7 comparison below must see the same form:
    # a same-server save spelled with a trailing slash must still be judged,
    # not silently skipped.
    tts_base = app_config.clean_base_url(req.tts.base_url)
    stt_base = app_config.clean_base_url(req.stt.base_url)

    _validate_tts_parameters_if_documented(tts_base, req.tts.parameters)

    # The mcp section is yaml-only for now (deliberately not in the request
    # model). Carry it over from the current config, otherwise every UI save
    # would silently wipe it from settings.yaml.
    current = app_config.get_settings()

    # The general section is a partial update: fields the client omitted
    # (None) keep their current values. Dialogs that don't edit general
    # settings (Servers) rely on this instead of carrying possibly-stale
    # in-memory values around. Merging via model_dump(exclude_none=True)
    # means any field added to GeneralConfig in the future is preserved
    # automatically — no per-field wiring to forget, which is exactly what
    # bit us with show_tool_calls.
    updated_general = current.general.model_copy(
        update=req.general.model_dump(exclude_none=True)
    )

    updated = AppSettings(
        llm=LLMSettings(
            base_url=req.llm.base_url,
            model=req.llm.model,
            max_tokens=req.llm.max_tokens,
            temperature=req.llm.temperature,
        ),
        tts=TTSConfig(
            enabled=req.tts.enabled,
            base_url=tts_base,
            timeout=req.tts.timeout,
            streaming=req.tts.streaming,
            parameters=req.tts.parameters,
        ),
        stt=STTConfig(
            enabled=req.stt.enabled,
            base_url=stt_base,
            timeout=req.stt.timeout,
        ),
        general=updated_general,
        mcp=current.mcp,
    )

    app_config.save_settings(updated)
    # Drop the capabilities cache (slot only, no inline refetch): the save
    # may have changed the TTS base_url, and a negative cache from a server
    # that was down at startup must not outlive the save that fixed it.
    # The next get_capabilities() call refetches.
    tts_client.invalidate_capabilities()
    logger.info("Settings updated: llm=%s, tts_active=%s, stt_active=%s",
                updated.llm.base_url, updated.tts.is_active, updated.stt.is_active)

    return _to_response(updated)
