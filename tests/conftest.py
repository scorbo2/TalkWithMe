"""Shared fixtures: keep tests hermetic.

The app leans on module-level globals (config caches, a session singleton,
a fixed persistence root, an MCP tool cache). This autouse fixture points
all of them at throwaway state under pytest's tmp_path so no test ever
reads or writes the real settings.yaml / personas.yaml / chatrooms.yaml /
chatrooms/ data.
"""

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of pytest's invocation cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.config as app_config
import app.persistence as persistence
import app.routers.persistence as persistence_router
import app.services.llm as llm_module
import app.services.llm_auth as llm_auth
import app.services.tool_registry as tool_registry
import app.services.tts_client as tts_client
import app.services.voice_fx as voice_fx
from app.session import session as global_session

from tests.factories import make_chatrooms, make_personas, make_settings


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch):
    """Point every global at throwaway state; restore everything after."""
    # All YAML writes (save_settings/save_chatrooms) land here; persona
    # writes go to the Personas directory resolved from this root.
    monkeypatch.setattr(app_config, "_PROJECT_ROOT", tmp_path)
    # All chatroom history/audio files land here.
    monkeypatch.setattr(persistence, "_PERSISTENCE_ROOT", tmp_path / "chatrooms")
    # The persistence router imported _PERSISTENCE_ROOT by value at import
    # time, so it needs its own patch to stay in sync.
    monkeypatch.setattr(persistence_router, "_PERSISTENCE_ROOT", tmp_path / "chatrooms")

    # Fresh config caches (the real YAML files are never read).
    monkeypatch.setattr(app_config, "_settings_cache", make_settings())
    monkeypatch.setattr(app_config, "_personas_cache", make_personas())
    monkeypatch.setattr(app_config, "_chatrooms_cache", make_chatrooms())

    # Module-level registries that survive across tests.
    persistence._pending_audio.clear()
    tool_registry.reset()
    # The TTS capabilities cache (single slot, docs and failures alike):
    # a doc cached by one test must not leak into the next.
    tts_client.invalidate_capabilities()
    # The LLM API key: never read the real llm_api_key file or the
    # developer's TALKWITHME_LLM_API_KEY, and never let a key cached by an
    # earlier test leak into the next one.
    monkeypatch.setattr(llm_auth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(llm_auth.ENV_VAR_NAME, raising=False)
    llm_auth.invalidate_llm_api_key()
    # The once-per-URL cleartext warning dedupe in llm.py must not survive
    # a test boundary.
    llm_module._warned_plaintext_urls.clear()
    # Voice FX: never depend on the host having ffmpeg (tests opt in by
    # patching voice_fx._ffmpeg), and reset the once-only warning.
    monkeypatch.setattr(voice_fx, "_ffmpeg", lambda: None)
    monkeypatch.setattr(voice_fx, "_warned_missing_ffmpeg", False)

    # The global session singleton: start every test clean.
    global_session._history.clear()
    global_session._active_personas.clear()
    global_session.set_current_room("default")

    yield

    persistence._pending_audio.clear()
    tool_registry.reset()
    tts_client.invalidate_capabilities()
    llm_auth.invalidate_llm_api_key()
    llm_module._warned_plaintext_urls.clear()
    global_session._history.clear()
    global_session._active_personas.clear()
    global_session.set_current_room("default")


@pytest.fixture
def tmp_project_root(tmp_path) -> Path:
    """The tmp directory standing in for the project root (YAML files)."""
    return tmp_path


@pytest.fixture
def persistence_root(tmp_path) -> Path:
    """The tmp directory standing in for the chatrooms/ persistence root."""
    return tmp_path / "chatrooms"


@pytest.fixture
def client():
    """FastAPI TestClient WITHOUT the startup lifespan.

    Deliberate: the lifespan re-reads the real YAML files (clobbering the
    test config caches) and attempts MCP discovery. Tests that exercise the
    lifespan do so explicitly (see test_main.py).
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def raw_asgi_get():
    """Run a GET against the app with an ASGI scope built the way uvicorn does.

    httpx (the TestClient's transport) normalizes dot segments and
    re-encodes the URL client-side, so a traversal URL can never reach the
    handler through a normal request. uvicorn instead sets scope["path"]
    to the percent-decoded target and scope["raw_path"] to the raw bytes —
    that combination is what makes "..%2Fx" a traversal at all. This
    helper reproduces uvicorn's scope construction (h11_impl.py) exactly.
    """
    import asyncio

    from app.main import app

    def _raw_asgi_get(path: str, raw_path: bytes | None = None):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": (raw_path if raw_path is not None else path.encode("ascii")),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        result = {"status": None, "body": b""}

        async def send(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
            elif message["type"] == "http.response.body":
                result["body"] += message.get("body", b"")

        asyncio.run(app(scope, receive, send))
        return result["status"], result["body"]

    return _raw_asgi_get
