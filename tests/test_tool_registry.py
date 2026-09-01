"""Tests for app/services/tool_registry.py — the startup tool cache.

`mcp_client.discover_tools` is monkeypatched so no network is involved.
"""

import asyncio
import logging
from typing import Dict, List

import pytest

import app.config as app_config
from app.config import MCPConfig
from app.services import builtin, mcp_client, tool_registry
from tests.factories import make_mcp_server, make_settings


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _openai_tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": f"desc {name}",
                     "parameters": {"type": "object", "properties": {}}},
    }


@pytest.fixture
def patch_discover(monkeypatch):
    """Route mcp_client.discover_tools through a per-server lookup table.

    Returns (calls, table): calls is the list of queried server names,
    table maps server name -> tools that server "offers".
    """
    table: Dict[str, List[dict]] = {}
    calls: List[str] = []

    async def fake_discover(server):
        calls.append(server.name)
        return list(table.get(server.name, []))

    monkeypatch.setattr(mcp_client, "discover_tools", fake_discover)
    return calls, table


def _patch_settings(monkeypatch, servers):
    monkeypatch.setattr(
        app_config, "_settings_cache",
        make_settings(mcp=MCPConfig(servers=list(servers))),
    )


class TestLoadTools:
    def test_no_servers_configured_leaves_cache_empty(self, monkeypatch, patch_discover):
        _patch_settings(monkeypatch, [])
        calls, _ = patch_discover

        _run(tool_registry.load_tools())

        assert calls == []
        assert tool_registry.get_all_tools() == []

    def test_loads_tools_from_each_server(self, monkeypatch, patch_discover):
        s1 = make_mcp_server("a", "http://a.local")
        s2 = make_mcp_server("b", "http://b.local")
        calls, table = patch_discover
        table["a"] = [_openai_tool("get_time"), _openai_tool("get_weather")]
        table["b"] = [_openai_tool("get_stock")]
        _patch_settings(monkeypatch, [s1, s2])

        _run(tool_registry.load_tools())

        assert calls == ["a", "b"]
        names = [t["function"]["name"] for t in tool_registry.get_all_tools()]
        assert sorted(names) == ["get_stock", "get_time", "get_weather"]

    def test_first_server_wins_on_duplicate_tool_names(self, monkeypatch, patch_discover):
        s1 = make_mcp_server("first", "http://1.local")
        s2 = make_mcp_server("second", "http://2.local")
        calls, table = patch_discover
        table["first"] = [_openai_tool("shared"), _openai_tool("only_first")]
        table["second"] = [_openai_tool("shared"), _openai_tool("only_second")]
        _patch_settings(monkeypatch, [s1, s2])

        _run(tool_registry.load_tools())

        # The duplicate is owned by the first server and dropped from the second.
        assert tool_registry.get_server_for_tool("shared").name == "first"
        assert tool_registry.get_server_for_tool("only_first").name == "first"
        assert tool_registry.get_server_for_tool("only_second").name == "second"
        names = [t["function"]["name"] for t in tool_registry.get_all_tools()]
        assert names.count("shared") == 1
        assert sorted(names) == ["only_first", "only_second", "shared"]

    def test_builtin_tool_names_are_reserved(self, monkeypatch, patch_discover, caplog):
        # A server advertising "add_memory" cannot shadow the built-in
        # tool (which works with no MCP servers at all): the MCP copy is
        # skipped, and the server's other tools still register.
        s1 = make_mcp_server("shadow", "http://s.local")
        calls, table = patch_discover
        table["shadow"] = [
            _openai_tool(builtin.ADD_MEMORY_NAME),
            _openai_tool("harmless"),
        ]
        _patch_settings(monkeypatch, [s1])

        with caplog.at_level(logging.WARNING):
            _run(tool_registry.load_tools())

        assert calls == ["shadow"]
        names = [t["function"]["name"] for t in tool_registry.get_all_tools()]
        assert names == ["harmless"]
        assert tool_registry.get_server_for_tool(builtin.ADD_MEMORY_NAME) is None
        assert "name is reserved by a built-in tool" in caplog.text

    def test_dead_server_does_not_block_others(self, monkeypatch, patch_discover):
        s1 = make_mcp_server("dead", "http://d.local")
        s2 = make_mcp_server("alive", "http://l.local")
        calls, table = patch_discover
        table["alive"] = [_openai_tool("ok_tool")]  # "dead" returns [] (down at startup)
        _patch_settings(monkeypatch, [s1, s2])

        _run(tool_registry.load_tools())

        assert calls == ["dead", "alive"]
        assert [t["function"]["name"] for t in tool_registry.get_all_tools()] == ["ok_tool"]
        assert tool_registry.get_server_for_tool("ok_tool").name == "alive"

    def test_load_tools_rebuilds_cache_from_scratch(self, monkeypatch, patch_discover):
        s1 = make_mcp_server("a", "http://a.local")
        calls, table = patch_discover
        table["a"] = [_openai_tool("t1")]
        _patch_settings(monkeypatch, [s1])

        _run(tool_registry.load_tools())
        assert tool_registry.get_all_tools() != []

        # Second run: stale entries must not survive.
        table["a"] = [_openai_tool("t2")]
        _run(tool_registry.load_tools())

        assert [t["function"]["name"] for t in tool_registry.get_all_tools()] == ["t2"]
        assert calls == ["a", "a"]

    def test_reset_clears_everything(self, monkeypatch, patch_discover):
        s1 = make_mcp_server("a", "http://a.local")
        _, table = patch_discover
        table["a"] = [_openai_tool("t1")]
        _patch_settings(monkeypatch, [s1])

        _run(tool_registry.load_tools())
        tool_registry.reset()

        assert tool_registry.get_all_tools() == []
        assert tool_registry.get_server_for_tool("t1") is None
