"""Tests for app/main.py — the index route, the startup lifespan, and the
TALKWITHME_LOG_LEVEL environment override."""

import logging

import pytest
from fastapi.testclient import TestClient

import app.config as app_config
import app.main as main_module
from app.session import session
from tests.factories import make_chatrooms, make_personas, make_settings


class TestResolveRootLogLevel:
    """The TALKWITHME_LOG_LEVEL override (app/main.py::_resolve_root_log_level)."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("debug", logging.DEBUG),
            ("INFO", logging.INFO),
            ("warning", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ],
    )
    def test_resolve_root_log_level_validName_returnsLevel(self, monkeypatch, raw, expected):
        # GIVEN a valid level name in TALKWITHME_LOG_LEVEL (any casing):
        monkeypatch.setenv("TALKWITHME_LOG_LEVEL", raw)

        # WHEN the level is resolved,
        # THEN it maps to the matching numeric level:
        assert main_module._resolve_root_log_level() == expected

    def test_resolve_root_log_level_envUnset_returnsInfo(self, monkeypatch):
        # GIVEN no TALKWITHME_LOG_LEVEL in the environment:
        monkeypatch.delenv("TALKWITHME_LOG_LEVEL", raising=False)

        # WHEN the level is resolved,
        # THEN the default INFO applies:
        assert main_module._resolve_root_log_level() == logging.INFO

    def test_resolve_root_log_level_blankEnv_returnsInfo(self, monkeypatch):
        # GIVEN a whitespace-only TALKWITHME_LOG_LEVEL:
        monkeypatch.setenv("TALKWITHME_LOG_LEVEL", "   ")

        # WHEN the level is resolved,
        # THEN it is treated as unset and the default INFO applies:
        assert main_module._resolve_root_log_level() == logging.INFO

    def test_resolve_root_log_level_whitespacePaddedName_stillResolves(self, monkeypatch):
        # GIVEN a valid name padded with whitespace:
        monkeypatch.setenv("TALKWITHME_LOG_LEVEL", "  debug  ")

        # WHEN the level is resolved,
        # THEN the padding is stripped and DEBUG applies:
        assert main_module._resolve_root_log_level() == logging.DEBUG

    def test_resolve_root_log_level_invalidName_warnsAndReturnsInfo(self, monkeypatch, caplog):
        # GIVEN a nonsense level name:
        monkeypatch.setenv("TALKWITHME_LOG_LEVEL", "LOUD")

        # WHEN the level is resolved,
        # THEN a warning naming the offending value is logged
        # AND the safe default INFO is returned:
        with caplog.at_level(logging.WARNING):
            level = main_module._resolve_root_log_level()
        assert level == logging.INFO
        assert "TALKWITHME_LOG_LEVEL" in caplog.text
        assert "LOUD" in caplog.text


class TestIndex:
    def test_serves_chat_ui(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "TalkWithMe v6.0" in resp.text

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
