"""Settings router — read and update application settings from settings.yaml."""

import logging

from fastapi import APIRouter

from app import config as app_config
from app.config import AppSettings, LLMSettings, STTConfig, TTSConfig
from app.models import (
    LLMSettingsResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    STTSettingsResponse,
    TTSSettingsResponse,
)

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
            num_steps=cfg.tts.num_steps,
            guidance_scale=cfg.tts.guidance_scale,
            seed=cfg.tts.seed,
            timeout=cfg.tts.timeout,
            streaming=cfg.tts.streaming,
        ),
        stt=STTSettingsResponse(
            enabled=cfg.stt.enabled,
            base_url=cfg.stt.base_url,
            timeout=cfg.stt.timeout,
        ),
    )


@router.get("", response_model=SettingsResponse)
def get_settings():
    """Return current application settings."""
    return _to_response(app_config.get_settings())


@router.put("", response_model=SettingsResponse)
def update_settings(req: SettingsUpdateRequest):
    """Update application settings and persist to settings.yaml.

    The frontend sends seed=0 to mean "no seed" (null). We normalize that
    here so the YAML and in-memory cache stay consistent.
    """
    # Normalize: blank base_url strings -> None, seed 0 -> None
    tts_base = req.tts.base_url if req.tts.base_url.strip() else None
    stt_base = req.stt.base_url if req.stt.base_url.strip() else None
    tts_seed = None if req.tts.seed == 0 else req.tts.seed

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
            num_steps=req.tts.num_steps,
            guidance_scale=req.tts.guidance_scale,
            seed=tts_seed,
            timeout=req.tts.timeout,
            streaming=req.tts.streaming,
        ),
        stt=STTConfig(
            enabled=req.stt.enabled,
            base_url=stt_base,
            timeout=req.stt.timeout,
        ),
    )

    app_config.save_settings(updated)
    logger.info("Settings updated: llm=%s, tts_active=%s, stt_active=%s",
                updated.llm.base_url, updated.tts.is_active, updated.stt.is_active)

    return _to_response(updated)
