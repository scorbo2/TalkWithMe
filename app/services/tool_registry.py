"""Startup cache of MCP tools.

Populated once at application start by querying every configured MCP
server. Provides the merged tool list for the LLM and the mapping from
a tool name back to the server that owns it (tool names are flat across
servers, so the LLM cannot tell which server it is calling).

The cache is in-memory only; a server that was down at startup simply
contributes no tools until the app is restarted (or `load_tools` is
called again, e.g. by a dev hot-reload).
"""

import logging
from typing import Dict, List, Optional

from app.config import MCPServerConfig, get_settings
from app.services import builtin, mcp_client

logger = logging.getLogger(__name__)

# server name -> tools in OpenAI function-calling format
_tool_cache: Dict[str, List[dict]] = {}
# tool name -> owning server config
_server_map: Dict[str, MCPServerConfig] = {}


async def load_tools() -> None:
    """Query all configured MCP servers and rebuild the tool cache.

    Failures are per-server: one dead endpoint never blocks the others.
    """
    _tool_cache.clear()
    _server_map.clear()

    settings = get_settings()
    if not settings.mcp.servers:
        logger.info("MCP: no servers configured, tool calling disabled")
        return

    for server in settings.mcp.servers:
        tools = await mcp_client.discover_tools(server)
        accepted = []
        for tool in tools:
            tool_name = tool["function"]["name"]
            if builtin.is_builtin_tool(tool_name):
                # Built-in names are reserved: the application owns that
                # tool (it works with no MCP servers at all), so a server
                # advertising the same name is skipped rather than shadowed.
                logger.warning(
                    "MCP tool '%s' from server '%s' ignored: name is reserved by a built-in tool",
                    tool_name, server.name,
                )
                continue
            if tool_name in _server_map:
                # First server wins: with a flat tool list the LLM could
                # not disambiguate a duplicated name anyway.
                logger.warning(
                    "MCP tool '%s' from server '%s' ignored: name already owned by '%s'",
                    tool_name, server.name, _server_map[tool_name].name,
                )
                continue
            _server_map[tool_name] = server
            accepted.append(tool)
        if accepted:
            _tool_cache[server.name] = accepted
            logger.info("MCP server '%s': %d tool(s) registered",
                        server.name, len(accepted))

    logger.info("MCP: %d tool(s) registered from %d server(s)",
                len(_server_map), len(_tool_cache))


def get_all_tools() -> List[dict]:
    """Flattened tool list from all servers, ready for the LLM payload."""
    return [tool for tools in _tool_cache.values() for tool in tools]


def get_server_for_tool(tool_name: str) -> Optional[MCPServerConfig]:
    """Look up which configured server owns the given tool name."""
    return _server_map.get(tool_name)


def reset() -> None:
    """Clear the cache (for tests and manual reloads)."""
    _tool_cache.clear()
    _server_map.clear()
