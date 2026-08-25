"""Async client for MCP (Model Context Protocol) servers.

Speaks JSON-RPC 2.0 over HTTP using the MCP "Streamable HTTP" transport
(spec revision 2025-03-26): a single endpoint that accepts POSTed
JSON-RPC messages and answers either with a plain JSON body or an
SSE stream of JSON-RPC messages.

Design note: every public function opens a fresh session (the
initialize handshake runs each time) instead of keeping long-lived
sessions around. That trades one extra round-trip per call for zero
session-expiry/reconnect bookkeeping, which is a fine bargain for a
local single-user app whose servers are at most a hop away.

Error convention: `call_tool` never raises. Failures are returned as
strings prefixed with "Error: " so the result (whether success or
failure) can be fed straight back to the LLM as a tool message. The
agentic loop turns the prefix into an explicit boolean "failed" flag on
tool_call events, which the frontend uses to style chips.
"""

import json
import logging
from typing import List, Optional

import httpx

from app.config import MCPServerConfig

logger = logging.getLogger(__name__)

_MCP_PROTOCOL_VERSION = "2025-03-26"
_MCP_CLIENT_INFO = {"name": "talkwithme", "version": "0.1.0"}
_REQUEST_HEADERS = {
    "Content-Type": "application/json",
    # Servers may answer with either a JSON body or an SSE stream
    "Accept": "application/json, text/event-stream",
}
# Failure marker for tool results, exported because the agentic loop uses
# it to set the explicit "failed" flag on tool_call events (the frontend
# must not have to sniff this prefix out of prose).
ERROR_PREFIX = "Error: "

# Hard cap on what gets fed back to the LLM (and shipped in the SSE
# tool_call event). An unbounded result — a fetched web page, a wide DB
# dump — would blow through a local model's context window, and the SSE
# event would carry every byte of it to the browser.
_MAX_RESULT_CHARS = 20_000


class MCPError(Exception):
    """Raised for JSON-RPC protocol errors and malformed server responses."""


def _rpc_request(method: str, params: Optional[dict] = None, req_id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request (has an "id", expects a response)."""
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


def _rpc_notification(method: str, params: Optional[dict] = None) -> dict:
    """Build a JSON-RPC 2.0 notification (no "id", no response expected)."""
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def _parse_response_body(resp: httpx.Response) -> Optional[dict]:
    """Extract the JSON-RPC response object from either response shape.

    Returns None when there is nothing to read (e.g. a 202 for a
    notification). Raises MCPError on JSON-RPC error objects.
    """
    if resp.status_code == 202 or not resp.content:
        return None

    content_type = resp.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        # SSE stream: the message we care about is the "data:" payload
        # carrying our request id. Other lines (server requests,
        # notifications) are irrelevant to a stateless client.
        for line in resp.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and _is_response(payload):
                return _unwrap(payload)
        raise MCPError(f"no JSON-RPC response found in SSE stream from {resp.url}")

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise MCPError(f"non-JSON response from MCP server {resp.url}: {exc}") from exc
    if isinstance(payload, dict) and _is_response(payload):
        return _unwrap(payload)
    raise MCPError(f"unrecognized response shape from MCP server {resp.url}")


def _is_response(payload: dict) -> bool:
    """A JSON-RPC response has an "id" and either "result" or "error"."""
    return "id" in payload and ("result" in payload or "error" in payload)


def _unwrap(payload: dict) -> Optional[dict]:
    """Return the "result" of a JSON-RPC response, raising on error objects."""
    if "error" in payload:
        error = payload["error"] or {}
        raise MCPError(
            f"JSON-RPC error {error.get('code')}: {error.get('message', 'unknown')}"
        )
    return payload.get("result")


async def _initialize(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Run the MCP initialize handshake; return the session id (if any)."""
    resp = await client.post(
        url,
        json=_rpc_request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _MCP_CLIENT_INFO,
            },
            req_id=1,
        ),
        headers=_REQUEST_HEADERS,
    )
    resp.raise_for_status()
    session_id = resp.headers.get("Mcp-Session-Id")
    result = _parse_response_body(resp)
    if result is None or "serverInfo" not in result:
        # Some servers omit serverInfo; we only need the handshake to complete.
        logger.debug("MCP initialize response without serverInfo from %s", url)
    # Completion notification — required by the spec, usually a 202.
    notify_headers = dict(_REQUEST_HEADERS)
    if session_id:
        notify_headers["Mcp-Session-Id"] = session_id
    await client.post(url, json=_rpc_notification("notifications/initialized"),
                      headers=notify_headers)
    return session_id


def _to_openai_tool(tool: dict) -> Optional[dict]:
    """Convert an MCP tool definition to the OpenAI function-calling shape."""
    name = tool.get("name")
    if not name:
        logger.warning("MCP tool without a name ignored: %s", tool)
        return None
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": tool.get("description", ""),
            # MCP uses "inputSchema"; OpenAI expects "parameters"
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


def _extract_result_text(result: dict) -> str:
    """Flatten a tools/call result into the string fed back to the LLM.

    Only text blocks are usable by the LLM. Non-text blocks (base64
    images, audio, ...) are described rather than dumped: serializing
    them into the conversation is how models get shot.
    """
    parts: List[str] = []
    non_text_blocks = 0
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
        else:
            non_text_blocks += 1
    if parts:
        return "\n".join(parts)
    if non_text_blocks:
        return f"(tool returned {non_text_blocks} non-text content block(s); no text output)"
    return "(no output)"


def _cap_result(text: str) -> str:
    """Truncate an overlong tool result, flagging the cut for the LLM."""
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + f"\n…[result truncated at {_MAX_RESULT_CHARS} chars]"


async def discover_tools(server: MCPServerConfig) -> List[dict]:
    """Query one MCP server for its tools, in OpenAI function-calling format.

    Returns an empty list on any failure (the server is simply skipped).
    """
    try:
        async with httpx.AsyncClient(timeout=server.timeout) as client:
            session_id = await _initialize(client, server.url)
            headers = dict(_REQUEST_HEADERS)
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            resp = await client.post(
                server.url,
                json=_rpc_request("tools/list", req_id=2),
                headers=headers,
            )
            resp.raise_for_status()
            result = _parse_response_body(resp)
            tools = (result or {}).get("tools", [])
            converted = [t for t in (_to_openai_tool(x) for x in tools) if t]
            logger.info("MCP server '%s' (%s): %d tool(s) discovered",
                        server.name, server.url, len(converted))
            return converted
    except Exception as exc:
        logger.warning("MCP tool discovery failed for server '%s' (%s): %s",
                       server.name, server.url, exc)
        return []


async def call_tool(server: MCPServerConfig, tool_name: str,
                    arguments: dict) -> str:
    """Invoke one tool on one MCP server. Returns the result as a string.

    Never raises: failures return "Error: ..." strings so the LLM can
    see and react to them.
    """
    try:
        async with httpx.AsyncClient(timeout=server.timeout) as client:
            session_id = await _initialize(client, server.url)
            headers = dict(_REQUEST_HEADERS)
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            resp = await client.post(
                server.url,
                json=_rpc_request(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments or {}},
                    req_id=2,
                ),
                headers=headers,
            )
            resp.raise_for_status()
            result = _parse_response_body(resp) or {}
            text = _cap_result(_extract_result_text(result))
            if result.get("isError"):
                return f"{ERROR_PREFIX}{text}"
            return text
    except Exception as exc:
        logger.warning("MCP tool call '%s' failed on server '%s' (%s): %s",
                       tool_name, server.name, server.url, exc)
        return f"{ERROR_PREFIX}{exc}"
