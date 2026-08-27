"""Tests for app/main.py — the index route and the startup lifespan."""

from fastapi.testclient import TestClient

import app.config as app_config
import app.main as main_module
from app.session import session
from tests.factories import make_chatrooms, make_personas, make_settings


class TestIndex:
    def test_serves_chat_ui(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "TalkWithMe v5.0" in resp.text

    def test_static_files_mounted(self, client):
        # state.js is the shared-globals module every other frontend file depends on.
        resp = client.get("/static/state.js")
        assert resp.status_code == 200
        assert len(resp.content) > 0


class TestLifespan:
    def test_startup_loads_config_and_seeds_session(self, monkeypatch):
        """Running the lifespan (via TestClient's context manager) must load
        all three config files, seed the session with every persona, and
        discover MCP tools."""
        calls = []

        async def fake_load_tools():
            calls.append("load_tools")

        monkeypatch.setattr(main_module.app_config, "load_personas",
                            lambda: (calls.append("load_personas"), make_personas())[1])
        monkeypatch.setattr(main_module.app_config, "load_settings",
                            lambda: (calls.append("load_settings"), make_settings())[1])
        monkeypatch.setattr(main_module.app_config, "load_chatrooms",
                            lambda: (calls.append("load_chatrooms"), make_chatrooms())[1])
        monkeypatch.setattr(main_module, "load_tools", fake_load_tools)

        with TestClient(main_module.app):
            pass

        assert calls == ["load_personas", "load_settings", "load_chatrooms", "load_tools"]
        # Session seeded with every configured persona.
        assert set(session.active_personas) == {"Alex", "Luna"}
