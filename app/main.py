"""TalkWithMe — FastAPI application entry point.

Wires up routers, static files, and the Jinja2 template engine.
Loads configuration at startup and seeds the session with all configured personas.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config as app_config
from app.routers import chat, chatrooms, personas, persistence, session as session_router, settings, stt, tts
from app.session import session
from app.services.tool_registry import get_all_tools, load_tools
from app.services.tts_client import ensure_capabilities

# uvicorn configures its own loggers but leaves the root logger at the
# Python default level (WARNING), which silently swallows every
# logger.info() call in this app — including the per-server MCP
# discovery lines at startup. basicConfig() is a no-op if a host process
# already attached handlers to the root logger, so this stays out of the
# way under gunicorn or test harnesses that configure logging themselves.
#
# The level defaults to INFO; TALKWITHME_LOG_LEVEL (e.g. "debug")
# overrides it for the run — see the "Logging" section of the README.
_VALID_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _resolve_root_log_level() -> int:
    """Numeric level for the app's root logger (INFO unless overridden).

    TALKWITHME_LOG_LEVEL takes a standard level name, case-insensitively.
    An invalid value warns and falls back to INFO instead of refusing to
    start over an environment typo. (The warning is emitted before
    basicConfig() runs, so it reaches the console via the last-resort
    handler — still visible.)
    """
    raw = os.environ.get("TALKWITHME_LOG_LEVEL", "").strip()
    if not raw:
        return logging.INFO
    level = _VALID_LOG_LEVELS.get(raw.upper())
    if level is None:
        logging.getLogger(__name__).warning(
            "TALKWITHME_LOG_LEVEL=%r is not a valid level name (expected one of %s); "
            "falling back to INFO",
            raw, ", ".join(_VALID_LOG_LEVELS),
        )
        return logging.INFO
    return level


logging.basicConfig(
    level=_resolve_root_log_level(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
# httpx logs every request it makes at INFO. Fine when debugging a client,
# spammy in normal operation — TTS/STT/LLM/MCP traffic would otherwise
# flood the console one line per HTTP round-trip.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config and initialize the session on startup."""
    # Load configuration files
    personas_cfg = app_config.load_personas()
    settings = app_config.load_settings()
    app_config.load_chatrooms()

    # Warm the TTS capabilities cache (best-effort; ensure_capabilities
    # never raises, so a down TTS server cannot break startup). Imported
    # into this module's namespace so tests can monkeypatch it the same
    # way they patch load_tools.
    await ensure_capabilities()

    # Seed session with all configured personas as active
    all_names = [p.name for p in personas_cfg.personas]
    session.set_active_personas(all_names)
    logger.info("TalkWithMe started with %d personas: %s", len(all_names), all_names)
    logger.info("LLM endpoint: %s", settings.llm.base_url)
    logger.info("TTS active: %s (endpoint: %s)", settings.tts.is_active, settings.tts.base_url)
    logger.info("STT active: %s (endpoint: %s)", settings.stt.is_active, settings.stt.base_url)

    # Discover MCP tools (per-server details are logged inside load_tools)
    await load_tools()
    logger.info("MCP tools available: %d", len(get_all_tools()))

    yield

    logger.info("TalkWithMe shutting down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TalkWithMe",
    description="A local multi-persona group chat application",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "static")), name="static")

# Register routers
app.include_router(personas.router)
app.include_router(chatrooms.router)
app.include_router(session_router.router)
app.include_router(chat.router)
app.include_router(tts.router)
app.include_router(stt.router)
app.include_router(settings.router)
app.include_router(persistence.router)

# Jinja2 templates
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main chat UI."""
    # New-style signature (request first): the legacy TemplateResponse(name,
    # {"request": ...}) form was removed from Starlette, and the new form
    # injects `request` into the template context for us.
    return templates.TemplateResponse(request, "index.html")
