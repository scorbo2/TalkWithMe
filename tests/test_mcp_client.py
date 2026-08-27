"""Tests for app/services/mcp_client.py — MCP JSON-RPC over Streamable HTTP.

Pure functions are tested with real httpx.Response objects; the network
functions (discover_tools / call_tool) use FakeMCPClient.
"""

import httpx
import pytest

from app.services import mcp_client
from app.services.mcp_client import MCPError
from tests.factories import FakeMCPClient, json_response, make_mcp_server


def response(status_code: int, **kwargs) -> httpx.Response:
    """Build an httpx.Response that carries a request (httpx 0.24 quirk:
    response.url / raise_for_status() raise without one)."""
    kwargs.setdefault("request", httpx.Request("POST", "http://mcp.local/rpc"))
    return httpx.Response(status_code, **kwargs)


def patch_mcp_client(monkeypatch, client: FakeMCPClient):
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda *a, **kw: client)


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _parse_response_body
# ---------------------------------------------------------------------------

class TestParseResponseBody:
    def test_plain_json_result(self):
        resp = response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        assert mcp_client._parse_response_body(resp) == {"ok": True}

    def test_json_rpc_error_raises_mcp_error(self):
        resp = response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        })
        with pytest.raises(MCPError, match="Method not found"):
            mcp_client._parse_response_body(resp)

    def test_202_notification_ack_returns_none(self):
        assert mcp_client._parse_response_body(response(202)) is None

    def test_empty_body_returns_none(self):
        resp = response(204, content=b"")
        assert mcp_client._parse_response_body(resp) is None

    def test_sse_stream_extracts_matching_response(self):
        body = (
            "event: message\n"
            "data: {\"jsonrpc\": \"2.0\", \"method\": \"some/notify\", \"params\": {}}\n"
            "\n"
            "data: {\"jsonrpc\": \"2.0\", \"id\": 7, \"result\": {\"found\": true}}\n"
            "\n"
        )
        resp = httpx.Response(
            200, content=body.encode(),
            headers={"Content-Type": "text/event-stream"},
        )
        resp.request = httpx.Request("POST", "http://mcp.local/rpc")
        assert mcp_client._parse_response_body(resp) == {"found": True}

    def test_sse_stream_without_response_raises(self):
        body = 'data: {"jsonrpc": "2.0", "method": "some/notify", "params": {}}\n\n'
        resp = httpx.Response(
            200, content=body.encode(),
            headers={"Content-Type": "text/event-stream"},
        )
        resp.request = httpx.Request("POST", "http://mcp.local/rpc")
        with pytest.raises(MCPError, match="no JSON-RPC response"):
            mcp_client._parse_response_body(resp)

    def test_non_json_body_raises(self):
        resp = response(200, content=b"not json at all")
        resp.headers["Content-Type"] = "text/plain"
        with pytest.raises(MCPError, match="non-JSON"):
            mcp_client._parse_response_body(resp)

    def test_unrecognized_shape_raises(self):
        resp = response(200, json={"jsonrpc": "2.0", "method": "a/notify"})
        with pytest.raises(MCPError, match="unrecognized response shape"):
            mcp_client._parse_response_body(resp)


# ---------------------------------------------------------------------------
# _to_openai_tool / _extract_result_text / _cap_result
# ---------------------------------------------------------------------------

class TestToOpenAITool:
    def test_converts_mcp_tool_to_openai_shape(self):
        mcp_tool = {
            "name": "get_time",
            "description": "Get the current time.",
            "inputSchema": {"type": "object", "properties": {"zone": {"type": "string"}}},
        }
        assert mcp_client._to_openai_tool(mcp_tool) == {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Get the current time.",
                "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}},
            },
        }

    def test_missing_input_schema_gets_empty_object(self):
        converted = mcp_client._to_openai_tool({"name": "x", "description": "d"})
        assert converted["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_missing_name_returns_none(self):
        assert mcp_client._to_openai_tool({"description": "nameless"}) is None


class TestExtractResultText:
    def test_joins_text_blocks(self):
        result = {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}
        assert mcp_client._extract_result_text(result) == "one\ntwo"

    def test_accepts_bare_strings(self):
        assert mcp_client._extract_result_text({"content": ["plain"]}) == "plain"

    def test_non_text_blocks_are_described_not_dumped(self):
        result = {"content": [{"type": "image", "data": "BASE64BLOB"}]}
        assert mcp_client._extract_result_text(result) == (
            "(tool returned 1 non-text content block(s); no text output)"
        )

    def test_no_content_returns_placeholder(self):
        assert mcp_client._extract_result_text({"content": []}) == "(no output)"
        assert mcp_client._extract_result_text({}) == "(no output)"


class TestCapResult:
    def test_short_result_unchanged(self):
        assert mcp_client._cap_result("small") == "small"

    def test_long_result_truncated_with_marker(self):
        text = "x" * (mcp_client._MAX_RESULT_CHARS + 500)
        capped = mcp_client._cap_result(text)
        assert len(capped) < len(text)
        assert "truncated" in capped
        assert capped.startswith("x" * mcp_client._MAX_RESULT_CHARS)


# ---------------------------------------------------------------------------
# discover_tools
# ---------------------------------------------------------------------------

class TestDiscoverTools:
    def test_discover_tools_returns_openai_shape_tools(self, monkeypatch):
        mcp_tools = [
            {"name": "get_time", "description": "d1", "inputSchema": {"type": "object"}},
            {"description": "nameless tool"},  # must be filtered out
        ]
        client = FakeMCPClient(tools=mcp_tools)
        patch_mcp_client(monkeypatch, client)

        tools = _run(mcp_client.discover_tools(make_mcp_server()))

        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "get_time"
        # Handshake: initialize, initialized notification, tools/list.
        methods = [p[1]["method"] for p in client.posts]
        assert methods == ["initialize", "notifications/initialized", "tools/list"]

    def test_initialize_notification_carries_session_id(self, monkeypatch):
        client = FakeMCPClient(session_id="sess-abc")
        patch_mcp_client(monkeypatch, client)
        _run(mcp_client.discover_tools(make_mcp_server()))

        notify = client.posts[1]
        assert notify[2].get("Mcp-Session-Id") == "sess-abc"
        # A notification has no "id" field.
        assert "id" not in notify[1]

    def test_discover_tools_returns_empty_list_on_failure(self, monkeypatch):
        class Down(FakeMCPClient):
            async def post(self, url, json=None, headers=None):
                raise httpx.ConnectError("nope")

        patch_mcp_client(monkeypatch, Down())
        assert _run(mcp_client.discover_tools(make_mcp_server())) == []

    def test_discover_tools_returns_empty_list_on_rpc_error(self, monkeypatch):
        class RpcError(FakeMCPClient):
            async def post(self, url, json=None, headers=None):
                self.posts.append((url, json, dict(headers or {})))
                if json.get("method") == "initialize":
                    return json_response(200, {
                        "jsonrpc": "2.0", "id": json["id"],
                        "error": {"code": -32000, "message": "boom"},
                    })
                return json_response(200, {"jsonrpc": "2.0", "id": json["id"], "result": {}})

        patch_mcp_client(monkeypatch, RpcError())
        assert _run(mcp_client.discover_tools(make_mcp_server())) == []


# ---------------------------------------------------------------------------
# call_tool
# ---------------------------------------------------------------------------

class TestCallTool:
    def _client(self, monkeypatch, call_result, sse=False):
        client = FakeMCPClient(call_result=call_result, call_result_sse=sse)
        patch_mcp_client(monkeypatch, client)
        return client

    def test_call_tool_returns_text_result(self, monkeypatch):
        self._client(monkeypatch, {"content": [{"type": "text", "text": "noon"}]})

        result = _run(mcp_client.call_tool(make_mcp_server(), "get_time", {"zone": "utc"}))

        assert result == "noon"

    def test_call_tool_sends_name_and_arguments(self, monkeypatch):
        client = self._client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]})
        _run(mcp_client.call_tool(make_mcp_server(), "get_time", {"zone": "utc"}))

        tool_call = next(p for p in client.posts if p[1]["method"] == "tools/call")
        assert tool_call[1]["params"] == {"name": "get_time", "arguments": {"zone": "utc"}}

    def test_call_tool_sse_response_body_is_parsed(self, monkeypatch):
        self._client(
            monkeypatch,
            {"content": [{"type": "text", "text": "via-sse"}]},
            sse=True,
        )
        result = _run(mcp_client.call_tool(make_mcp_server(), "get_time", {}))
        assert result == "via-sse"

    def test_call_tool_is_error_sets_error_prefix(self, monkeypatch):
        self._client(monkeypatch, {
            "content": [{"type": "text", "text": "disk on fire"}], "isError": True,
        })
        result = _run(mcp_client.call_tool(make_mcp_server(), "burn", {}))
        assert result.startswith(mcp_client.ERROR_PREFIX)
        assert "disk on fire" in result

    def test_call_tool_truncates_oversized_results(self, monkeypatch):
        self._client(monkeypatch, {"content": [{"type": "text", "text": "y" * 50_000}]})
        result = _run(mcp_client.call_tool(make_mcp_server(), "big", {}))
        assert len(result) < 50_000
        assert "truncated" in result

    def test_call_tool_never_raises_on_connection_error(self, monkeypatch):
        class Down(FakeMCPClient):
            async def post(self, url, json=None, headers=None):
                raise httpx.ConnectError("server is down")

        patch_mcp_client(monkeypatch, Down())
        result = _run(mcp_client.call_tool(make_mcp_server(), "get_time", {}))
        assert result.startswith(mcp_client.ERROR_PREFIX)

    def test_call_tool_never_raises_on_http_error(self, monkeypatch):
        class Http500(FakeMCPClient):
            async def post(self, url, json=None, headers=None):
                self.posts.append((url, json, dict(headers or {})))
                if json.get("method") == "initialize":
                    return json_response(200, {
                        "jsonrpc": "2.0", "id": json["id"],
                        "result": {"serverInfo": {"name": "x", "version": "1"}},
                    })
                # Request attached: without it, httpx 0.24 raises
                # RuntimeError in raise_for_status and we'd test the wrong path.
                resp = httpx.Response(500, content=b"boom")
                resp.request = httpx.Request("POST", "http://mcp.local/rpc")
                return resp

        patch_mcp_client(monkeypatch, Http500())
        result = _run(mcp_client.call_tool(make_mcp_server(), "get_time", {}))
        assert result.startswith(mcp_client.ERROR_PREFIX)
