"""TalkWithMe — FastAPI application entry point.

Wires up routers, static files, and the Jinja2 template engine.
Loads configuration at startup and seeds the session with all configured personas.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config as app_config
from app.routers import chat, chatrooms, personas, persistence, session as session_router, settings, stt, tts
from app.session import session

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

    # Seed session with all configured personas as active
    all_names = [p.name for p in personas_cfg.personas]
    session.set_active_personas(all_names)
    logger.info("TalkWithMe started with %d personas: %s", len(all_names), all_names)
    logger.info("LLM endpoint: %s", settings.llm.base_url)
    logger.info("TTS active: %s (endpoint: %s)", settings.tts.is_active, settings.tts.base_url)
    logger.info("STT active: %s (endpoint: %s)", settings.stt.is_active, settings.stt.base_url)

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
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={} # optional additional context
    )
