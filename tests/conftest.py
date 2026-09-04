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
import app.services.tool_registry as tool_registry
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

    # The global session singleton: start every test clean.
    global_session._history.clear()
    global_session._active_personas.clear()
    global_session.set_current_room("default")

    yield

    persistence._pending_audio.clear()
    tool_registry.reset()
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
