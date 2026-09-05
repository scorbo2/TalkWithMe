"""Tests for app/main.py — the index route, the startup lifespan, and the
TALKWITHME_LOG_LEVEL environment override."""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

import app.config as app_config
import app.main as main_module
import app.services.tts_client as tts_client
from app.config import TTSConfig
from app.session import session
from tests.factories import (
    FakeAsyncClient,
    json_response,
    make_capabilities_doc,
    make_chatrooms,
    make_personas,
    make_settings,
)


def _run(coro):
    """Run an awaitable to completion on a throwaway event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def fake_load_tools():
    pass


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
        assert "TalkWithMe v7.0" in resp.text

    def test_static_files_mounted(self, client):
        # state.js is the shared-globals module every other frontend file depends on.
        resp = client.get("/static/state.js")
        assert resp.status_code == 200
        assert len(resp.content) > 0


class TestLifespan:
    def test_startup_loads_config_and_seeds_session(self, monkeypatch):
        """Running the lifespan (via TestClient's context manager) must load
        all three config files, warm the TTS capabilities cache, seed the
        session with every persona, and discover MCP tools."""
        calls = []

        async def fake_ensure_capabilities():
            calls.append("ensure_capabilities")

        async def fake_load_tools():
            calls.append("load_tools")

        monkeypatch.setattr(main_module.app_config, "load_personas",
                            lambda: (calls.append("load_personas"), make_personas())[1])
        monkeypatch.setattr(main_module.app_config, "load_settings",
                            lambda: (calls.append("load_settings"), make_settings())[1])
        monkeypatch.setattr(main_module.app_config, "load_chatrooms",
                            lambda: (calls.append("load_chatrooms"), make_chatrooms())[1])
        monkeypatch.setattr(main_module, "ensure_capabilities", fake_ensure_capabilities)
        monkeypatch.setattr(main_module, "load_tools", fake_load_tools)

        with TestClient(main_module.app):
            pass

        assert calls == ["load_personas", "load_settings", "load_chatrooms",
                         "ensure_capabilities", "load_tools"]
        # Session seeded with every configured persona.
        assert set(session.active_personas) == {"Alex", "Luna"}

    def test_startup_warms_tts_capabilities_cache(self, monkeypatch):
        """With TTS active, the real ensure_capabilities() fetches
        /capabilities once during the lifespan and the cache is warm
        afterwards (no further network access)."""
        tts = TTSConfig(enabled=True, base_url="http://tts.local:5500")
        doc = make_capabilities_doc(engine="omnivoice")
        calls = []

        monkeypatch.setattr(main_module.app_config, "load_personas", lambda: make_personas())
        monkeypatch.setattr(main_module.app_config, "load_settings", lambda: make_settings(tts=tts))
        monkeypatch.setattr(main_module.app_config, "load_chatrooms", lambda: make_chatrooms())
        monkeypatch.setattr(main_module, "load_tools", fake_load_tools)
        # The settings cache must agree with what load_settings "loaded".
        monkeypatch.setattr(app_config, "_settings_cache", make_settings(tts=tts))

        def responder(method, url, **kw):
            calls.append((method, url))
            return json_response(200, doc)

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        with TestClient(main_module.app):
            pass

        assert calls == [("GET", "http://tts.local:5500/capabilities")]
        # The warm cache serves without another request:
        def dead(*a, **kw):
            raise AssertionError("the capabilities cache should have been warm")

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(dead))
        assert _run(tts_client.get_capabilities()) == doc

    def test_startup_survives_tts_capabilities_fetch_failure(self, monkeypatch):
        """TTS active but /capabilities 404s (e.g. an old pre-ported script,
        unsupported per plan T11): startup completes, the cache holds a
        negative result, and no retry happens."""
        tts = TTSConfig(enabled=True, base_url="http://tts.local:5500")
        calls = []

        monkeypatch.setattr(main_module.app_config, "load_personas", lambda: make_personas())
        monkeypatch.setattr(main_module.app_config, "load_settings", lambda: make_settings(tts=tts))
        monkeypatch.setattr(main_module.app_config, "load_chatrooms", lambda: make_chatrooms())
        monkeypatch.setattr(main_module, "load_tools", fake_load_tools)
        monkeypatch.setattr(app_config, "_settings_cache", make_settings(tts=tts))

        def responder(method, url, **kw):
            calls.append(url)
            return json_response(404, {"detail": "Not Found"})

        monkeypatch.setattr(tts_client.httpx, "AsyncClient",
                            lambda *a, **kw: FakeAsyncClient(responder))

        with TestClient(main_module.app):
            pass

        assert calls == ["http://tts.local:5500/capabilities"]
        assert _run(tts_client.get_capabilities()) is None
        assert calls == ["http://tts.local:5500/capabilities"]  # negative cache served, no retry


