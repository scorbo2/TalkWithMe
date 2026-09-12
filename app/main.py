"""TalkWithMe — FastAPI application entry point.

Wires up routers, static files, and the Jinja2 template engine.
Loads configuration at startup and seeds the session with all configured personas.
Auto-starts the OmniVoice TTS server subprocess when TTS is enabled.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config as app_config
from app.routers import chat, chatrooms, personas, persistence, session as session_router, settings, stt, tts
from app.session import session
from app.services import llm, llm_auth
from app.services.tool_registry import get_all_tools, load_tools
from app.services.tts_client import ensure_capabilities

# Path to the OmniVoice REST wrapper and its venv
_TTS_PROJECT_ROOT = Path(os.environ.get("OMNIVOICE_TTS_ROOT", "C:/ai/tts"))
_TTS_WRAPPER = _TTS_PROJECT_ROOT / "omnivoice_rest.py"
_TTS_VENV_PYTHON = _TTS_PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

# Environment override: set OMNIVOICE_AUTO_START_TTS=0 to disable auto-start
_AUTO_START_TTS_DEFAULT = True

logger = logging.getLogger(__name__)

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


def _is_port_open(host: str, port: int) -> bool:
    """Check if a TCP port is accepting connections."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _start_tts_server() -> subprocess.Popen | None:
    """Launch the OmniVoice REST server as a subprocess if not already running.

    Returns the process handle, or None if:
    - Auto-start is disabled via env var
    - The wrapper script or venv python doesn't exist
    - A server is already listening on port 9000
    """
    if os.environ.get("OMNIVOICE_AUTO_START_TTS", "1") != "1":
        return None

    # Check if something is already serving on port 9000
    if _is_port_open("localhost", 9000):
        # Verify it's our OmniVoice server
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://localhost:9000/health", timeout=2)
            if resp.status == 200:
                logger.info("TTS server already running on port 9000")
                return None
        except Exception:
            pass  # Port is in use but not our server; fall through to start attempt

    python_exe = str(_TTS_VENV_PYTHON) if _TTS_VENV_PYTHON.exists() else sys.executable
    if not _TTS_WRAPPER.exists():
        logger.warning("OmniVoice wrapper not found at %s; skipping TTS auto-start", _TTS_WRAPPER)
        return None

    logger.info("Starting OmniVoice TTS server from %s", _TTS_PROJECT_ROOT)
    # Redirect stdout/stderr to a log file — using PIPE without reading causes
    # the subprocess to block once the OS pipe buffer fills up.
    tts_log = _TTS_PROJECT_ROOT / "tts_server.log"
    proc = subprocess.Popen(
        [python_exe, str(_TTS_WRAPPER)],
        cwd=str(_TTS_PROJECT_ROOT),
        stdout=open(tts_log, "ab"),
        stderr=subprocess.STDOUT,
        # Put the child in its own process group so we can kill the whole tree
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    return proc


def _stop_tts_server(proc: subprocess.Popen | None) -> None:
    """Terminate the TTS subprocess and its process group."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            # Kill the entire process group (child processes like uvicorn workers)
            proc.send_signal(signal.CTRL_BREAK_EVENT if hasattr(signal, "CTRL_BREAK_EVENT") else signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("TTS server did not exit gracefully, killing")
        proc.kill()
        proc.wait(timeout=2)
    except Exception as exc:
        logger.warning("Error stopping TTS server: %s", exc)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config, initialize session, and auto-start TTS server if enabled."""
    # Load configuration files
    personas_cfg = app_config.load_personas()
    settings = app_config.load_settings()
    app_config.load_chatrooms()

    # Resolve the LLM API key once (env var, then llm_api_key file). It is
    # deliberately not part of settings.yaml (that file is tracked in git)
    # and is not reloadable at runtime — a restart is required (docs/
    # feature_api_key.md). Also warn when the LLM URL is plain http.
    llm_auth.load_llm_api_key()
    llm.warn_if_plaintext_llm(settings.llm.base_url)

    # Warm the TTS capabilities cache (best-effort; ensure_capabilities
    # never raises, so a down TTS server cannot break startup). Imported
    # into this module's namespace so tests can monkeypatch it the same
    # way they patch load_tools.
    await ensure_capabilities()

    # Seed session with all configured personas as active
    all_names = [p.name for p in personas_cfg.personas]
    session.set_active_personas(all_names)
    logger.info("TalkWithMe started with %d personas: %s", len(all_names), all_names)
    # "configured"/"not configured" only — the key itself never reaches the log.
    logger.info(
        "LLM endpoint: %s (API key: %s)",
        settings.llm.base_url,
        "configured" if llm_auth.get_llm_api_key() else "not configured",
    )
    logger.info("TTS active: %s (endpoint: %s)", settings.tts.is_active, settings.tts.base_url)
    logger.info("STT active: %s (endpoint: %s)", settings.stt.is_active, settings.stt.base_url)

    # Auto-start the TTS server if TTS is enabled
    tts_proc = None
    if settings.tts.is_active:
        tts_proc = _start_tts_server()
        if tts_proc is not None:
            # Wait up to 60s for the server to start (model loading can take 10-30s on first run)
            for attempt in range(60):
                await asyncio.sleep(1)
                try:
                    async with httpx.AsyncClient(timeout=2) as client:
                        resp = await client.get("http://localhost:9000/health")
                        if resp.status_code == 200:
                            logger.info("TTS server started successfully on port 9000")
                            break
                except Exception:
                    pass  # Server not ready yet, keep waiting
            else:
                logger.warning("TTS server may not have started (health check failed after 60s)")
    else:
        logger.info("TTS disabled in config; skipping TTS server auto-start")

    # Discover MCP tools (per-server details are logged inside load_tools)
    await load_tools()
    logger.info("MCP tools available: %d", len(get_all_tools()))

    yield

    # Shutdown cleanup
    logger.info("TalkWithMe shutting down")
    _stop_tts_server(tts_proc)


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
