"""Tests for app/services/llm.py — SSE parsing and the agentic tool loop.

The httpx client is replaced with FakeLLMClient (see tests/factories.py);
no network access is involved.
"""

import asyncio
import json

import pytest

import app.config as app_config
import app.services.llm as llm
from tests.factories import (
    FakeLLMClient,
    FakeStreamResponse,
    json_response,
    make_settings,
)


def sse_line(chunk: dict) -> str:
    return f"data: {json.dumps(chunk)}"


def token_line(text: str) -> str:
    return sse_line({"choices": [{"delta": {"content": text}}]})


def tool_call_delta_line(index: int, **fields) -> str:
    delta = {"tool_calls": [{"index": index, **fields}]}
    return sse_line({"choices": [{"delta": delta}]})


def finish_line(reason: str) -> str:
    return sse_line({"choices": [{"delta": {}, "finish_reason": reason}]})


def patch_llm_client(monkeypatch, client: FakeLLMClient):
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **kw: client)


def _run_until_complete(aw):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(aw)
    finally:
        loop.close()


def _collect(agen):
    """Run an async generator to completion and return its items."""

    async def runner():
        return [item async for item in agen]

    return _run_until_complete(runner())


def _run(coro):
    return _run_until_complete(coro)


# ---------------------------------------------------------------------------
# stream_chat — SSE parsing
# ---------------------------------------------------------------------------

class TestStreamChat:
    def test_stream_chat_yields_tokens_in_order_and_skips_malformed_lines(self, monkeypatch):
        lines = [
            token_line("Hel"),
            token_line("lo"),
            "data: not-json",            # malformed JSON: skipped, not fatal
            ": keep-alive comment",      # not a data line: skipped
            "",                          # blank line: skipped
            sse_line({"choices": [{"delta": {"role": "assistant"}}]}),  # no content
            sse_line({"choices": []}),   # empty choices: skipped
            token_line(" world"),
            "data: [DONE]",
            token_line("AFTER-DONE"),    # never yielded
        ]
        patch_llm_client(monkeypatch, FakeLLMClient(lines))

        tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        assert tokens == ["Hel", "lo", " world"]

    def test_stream_chat_sends_configured_payload(self, monkeypatch):
        client = FakeLLMClient([token_line("x"), "data: [DONE]"])
        patch_llm_client(monkeypatch, client)

        _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        payload = client.payloads[0]
        assert payload["model"] == "test-model"
        assert payload["stream"] is True
        assert payload["max_tokens"] == 1024
        assert payload["temperature"] == 0.8
        assert payload["messages"] == [{"role": "user", "content": "hi"}]

    def test_stream_chat_connection_error_propagates(self, monkeypatch):
        class RefusingStream:
            async def __aenter__(self):
                raise RuntimeError("connection refused")

            async def __aexit__(self, *a):
                return False

        class Boom(FakeLLMClient):
            def stream(self, method, url, json=None):
                return RefusingStream()

        patch_llm_client(monkeypatch, Boom([]))
        with pytest.raises(RuntimeError, match="connection refused"):
            _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

    def test_stream_chat_http_status_error_propagates(self, monkeypatch):
        class StatusBoom(FakeLLMClient):
            def stream(self, method, url, json=None):
                return FakeStreamResponse([], status_code=500)

        patch_llm_client(monkeypatch, StatusBoom([]))
        with pytest.raises(Exception, match="HTTP 500"):
            _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))


# ---------------------------------------------------------------------------
# chat_completion — non-streaming router call
# ---------------------------------------------------------------------------

class TestChatCompletion:
    def test_chat_completion_returns_content_and_sends_routing_payload(self, monkeypatch):
        resp = json_response(200, {"choices": [{"message": {"content": "Luna"}}]})
        client = FakeLLMClient([], post_response=resp)
        patch_llm_client(monkeypatch, client)

        result = _run(llm.chat_completion([{"role": "user", "content": "pick"}], max_tokens=16))

        assert result == "Luna"
        payload = client.payloads[0]
        assert payload["stream"] is False
        assert payload["max_tokens"] == 16
        assert payload["temperature"] == 0.1  # deterministic routing

    def test_chat_completion_returns_empty_string_on_failure(self, monkeypatch):
        class Down(FakeLLMClient):
            async def post(self, url, json=None):
                self.payloads.append(json)
                raise RuntimeError("server down")

        patch_llm_client(monkeypatch, Down([]))
        result = _run(llm.chat_completion([{"role": "user", "content": "pick"}]))
        assert result == ""


# ---------------------------------------------------------------------------
# Tool-call delta merging
# ---------------------------------------------------------------------------

class TestMergeToolCallDelta:
    def test_merge_first_delta_collects_id_type_name_args(self):
        pending = {}
        llm._merge_tool_call_delta(
            pending,
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_time", "arguments": '{"zone":'}},
        )
        entry = pending[0]
        assert entry["id"] == "call_1"
        assert entry["function"]["name"] == "get_time"
        assert entry["function"]["arguments"] == '{"zone":'

    def test_merge_appends_argument_fragments(self):
        pending = {}
        llm._merge_tool_call_delta(
            pending,
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_time", "arguments": '{"zone": '}},
        )
        llm._merge_tool_call_delta(pending, {"index": 0, "function": {"arguments": '"utc"}'}})
        assert pending[0]["function"]["arguments"] == '{"zone": "utc"}'

    def test_merge_name_fragment_concatenation(self):
        pending = {}
        llm._merge_tool_call_delta(pending, {"index": 0, "function": {"name": "get_"}})
        llm._merge_tool_call_delta(pending, {"index": 0, "function": {"name": "time"}})
        assert pending[0]["function"]["name"] == "get_time"

    def test_merge_resends_full_name_instead_of_concatenating(self):
        """Backends that re-send the full name in a later delta must not
        produce 'get_timeget_time'."""
        pending = {}
        llm._merge_tool_call_delta(pending, {"index": 0, "function": {"name": "get_time"}})
        llm._merge_tool_call_delta(pending, {"index": 0, "function": {"name": "get_time"}})
        assert pending[0]["function"]["name"] == "get_time"

    def test_merge_keeps_separate_calls_by_index(self):
        pending = {}
        llm._merge_tool_call_delta(pending, {"index": 0, "id": "a",
                                             "function": {"name": "one", "arguments": "1"}})
        llm._merge_tool_call_delta(pending, {"index": 1, "id": "b",
                                             "function": {"name": "two", "arguments": "2"}})
        assert pending[0]["function"]["name"] == "one"
        assert pending[1]["function"]["name"] == "two"


class TestNormalizeToolCall:
    def test_normalize_synthesizes_missing_id(self):
        normalized = llm._normalize_tool_call(
            {"function": {"name": "get_time", "arguments": "{}"}}
        )
        assert normalized["id"].startswith("call_")
        assert normalized["type"] == "function"

    def test_normalize_preserves_existing_id(self):
        normalized = llm._normalize_tool_call(
            {"id": "call_keep", "function": {"name": "x", "arguments": "{}"}}
        )
        assert normalized["id"] == "call_keep"

    def test_normalize_fills_missing_function_fields(self):
        normalized = llm._normalize_tool_call({"id": "c1", "type": "function"})
        assert normalized["function"]["name"] == ""
        assert normalized["function"]["arguments"] == ""


class TestTryParseArguments:
    def test_try_parse_arguments_empty_string_means_no_args(self):
        assert llm._try_parse_arguments("") == {}

    def test_try_parse_arguments_valid_json(self):
        assert llm._try_parse_arguments('{"a": 1}') == {"a": 1}

    def test_try_parse_arguments_invalid_json_returns_none(self):
        assert llm._try_parse_arguments('{"a": 1') is None

    def test_try_parse_arguments_non_dict_wrapped_in_value(self):
        assert llm._try_parse_arguments("42") == {"value": 42}


# ---------------------------------------------------------------------------
# stream_chat_with_tools — the agentic loop
# ---------------------------------------------------------------------------

class TestStreamChatWithTools:
    def test_tool_call_executed_result_fed_back_and_text_streamed(self, monkeypatch):
        """Full round: LLM asks for a tool (name/args arriving in fragments),
        the result is fed back, and the final text is streamed."""
        from app.services import mcp_client, tool_registry
        from tests.factories import make_mcp_server

        server = make_mcp_server()
        tool_registry._server_map["get_time"] = server

        executed = []

        async def fake_call_tool(server_cfg, tool_name, arguments):
            executed.append((tool_name, arguments))
            return "It is noon."

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

        class RoundClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                if len(self.payloads) == 1:
                    lines = [
                        tool_call_delta_line(0, id="call_9", type="function",
                                             function={"name": "get_ti", "arguments": ""}),
                        tool_call_delta_line(0, function={"name": "me", "arguments": '{"zone": '}),
                        tool_call_delta_line(0, function={"arguments": '"utc"}'}),
                        finish_line("tool_calls"),
                    ]
                else:
                    lines = [token_line("It is "), token_line("noon."), finish_line("stop")]
                return FakeStreamResponse(lines)

        client = RoundClient([])
        patch_llm_client(monkeypatch, client)

        events = _collect(
            llm.stream_chat_with_tools(
                [{"role": "user", "content": "what time is it?"}],
                [{"type": "function", "function": {"name": "get_time"}}],
            )
        )

        assert [e["type"] for e in events] == ["tool_call", "token", "token"]

        tool_event = events[0]
        assert tool_event["tool_name"] == "get_time"
        assert tool_event["arguments"] == {"zone": "utc"}
        assert tool_event["result"] == "It is noon."
        assert tool_event["failed"] is False
        assert executed == [("get_time", {"zone": "utc"})]
        assert "".join(e["token"] for e in events if e["type"] == "token") == "It is noon."

        # The second request carried the assistant tool-call and tool result,
        # in the pairing the OpenAI-compatible API expects.
        second_messages = client.payloads[1]["messages"]
        tool_call_msg = next(m for m in second_messages if m.get("role") == "assistant")
        assert tool_call_msg["tool_calls"][0]["function"]["name"] == "get_time"
        tool_result_msg = next(m for m in second_messages if m.get("role") == "tool")
        assert tool_result_msg["content"] == "It is noon."
        assert tool_result_msg["tool_call_id"] == "call_9"

    def test_unknown_tool_reports_failure_without_calling_mcp(self, monkeypatch):
        from app.services import mcp_client

        called = []

        async def fake_call_tool(server_cfg, tool_name, arguments):
            called.append(tool_name)

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)
        # The tool registry is empty (autouse reset): no server owns "get_time".

        class RoundClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                if len(self.payloads) == 1:
                    lines = [
                        tool_call_delta_line(0, id="c1", type="function",
                                             function={"name": "get_time", "arguments": "{}"}),
                        finish_line("tool_calls"),
                    ]
                else:
                    lines = [token_line("ok"), finish_line("stop")]
                return FakeStreamResponse(lines)

        patch_llm_client(monkeypatch, RoundClient([]))

        events = _collect(llm.stream_chat_with_tools([{"role": "user", "content": "time?"}], []))

        tool_event = next(e for e in events if e["type"] == "tool_call")
        assert tool_event["failed"] is True
        assert tool_event["result"].startswith("Error: unknown tool 'get_time'")
        assert called == []  # the MCP server was never contacted

    def test_unparseable_arguments_not_executed_with_max_tokens_hint(self, monkeypatch):
        """A call truncated mid-arguments must be refused, not executed,
        and the LLM must be told it hit max_tokens (finish_reason=length)."""
        from app.services import mcp_client

        executed = []

        async def fake_call_tool(server_cfg, tool_name, arguments):
            executed.append(tool_name)

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

        class RoundClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                if len(self.payloads) == 1:
                    lines = [
                        tool_call_delta_line(0, id="c1", type="function",
                                             function={"name": "get_time",
                                                       "arguments": '{"zone": "ut'}),
                        finish_line("length"),  # truncated at max_tokens
                    ]
                else:
                    lines = [token_line("sorry"), finish_line("stop")]
                return FakeStreamResponse(lines)

        patch_llm_client(monkeypatch, RoundClient([]))

        events = _collect(llm.stream_chat_with_tools([{"role": "user", "content": "time?"}], []))

        tool_event = next(e for e in events if e["type"] == "tool_call")
        assert tool_event["failed"] is True
        assert "not valid JSON" in tool_event["result"]
        assert "max_tokens" in tool_event["result"]
        assert executed == []  # never executed

    def test_mcp_error_result_sets_failed_flag(self, monkeypatch):
        from app.services import mcp_client, tool_registry
        from tests.factories import make_mcp_server

        tool_registry._server_map["boom"] = make_mcp_server()

        async def fake_call_tool(server_cfg, tool_name, arguments):
            return "Error: connection refused"

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

        class RoundClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                if len(self.payloads) == 1:
                    lines = [
                        tool_call_delta_line(0, id="c1", type="function",
                                             function={"name": "boom", "arguments": "{}"}),
                        finish_line("tool_calls"),
                    ]
                else:
                    lines = [token_line("ok"), finish_line("stop")]
                return FakeStreamResponse(lines)

        patch_llm_client(monkeypatch, RoundClient([]))

        events = _collect(llm.stream_chat_with_tools([{"role": "user", "content": "boom?"}], []))

        tool_event = next(e for e in events if e["type"] == "tool_call")
        assert tool_event["failed"] is True
        assert tool_event["result"] == "Error: connection refused"

    def test_iteration_cap_forces_final_toolless_round(self, monkeypatch):
        """With max_tool_iterations=1: round 0 may use tools, the final
        round is sent WITHOUT tools, and a tool call there is dropped."""
        from app.config import MCPConfig

        monkeypatch.setattr(
            app_config, "_settings_cache",
            make_settings(mcp=MCPConfig(max_tool_iterations=1)),
        )

        from app.services import mcp_client, tool_registry
        from tests.factories import make_mcp_server

        tool_registry._server_map["loop"] = make_mcp_server()

        async def fake_call_tool(server_cfg, tool_name, arguments):
            return "still looping"

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

        class RoundClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                # Both rounds: the model keeps asking for the tool.
                lines = [
                    tool_call_delta_line(
                        0, id=f"c{len(self.payloads)}", type="function",
                        function={"name": "loop", "arguments": "{}"},
                    ),
                    finish_line("tool_calls"),
                ]
                return FakeStreamResponse(lines)

        client = RoundClient([])
        patch_llm_client(monkeypatch, client)
        tool_list = [{"type": "function", "function": {"name": "loop"}}]

        events = _collect(llm.stream_chat_with_tools([{"role": "user", "content": "loop"}], tool_list))

        # Two LLM rounds total: the allowed tool round + the forced final one.
        assert len(client.payloads) == 2
        assert client.payloads[0]["tools"] == tool_list  # round 0 offers tools
        assert "tools" not in client.payloads[1]  # final round is tool-less
        # The final round's tool call was dropped, not executed.
        tool_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_events) == 1
        assert events[-1]["type"] == "tool_call"
        assert tool_events[0]["failed"] is False
