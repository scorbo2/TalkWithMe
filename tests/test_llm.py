"""Tests for app/services/llm.py — SSE parsing and the agentic tool loop.

The httpx client is replaced with FakeLLMClient (see tests/factories.py);
no network access is involved.
"""

import asyncio
import json
import logging

import pytest

import app.config as app_config
import app.services.llm as llm
import app.services.llm_auth as llm_auth
from app.config import Persona
from app.services import builtin
from tests.factories import (
    FakeLLMClient,
    FakeStreamResponse,
    json_response,
    make_settings,
)


def _tool_persona(tmp_path=None) -> Persona:
    """Persona passed to stream_chat_with_tools().

    MCP tool tests never touch its directory (built-in dispatch is a no-op
    for them); the built-in dispatch test passes tmp_path to give
    add_memory a real memories.txt to write.
    """
    if tmp_path is None:
        return Persona(name="Mindy", system_prompt="p")
    persona_dir = tmp_path / "Mindy"
    persona_dir.mkdir(parents=True)
    return Persona(name="Mindy", system_prompt="p", persona_dir=persona_dir)


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
    def _factory(*args, **kwargs):
        # Record the constructor kwargs (timeout, headers) of every client
        # build so tests can assert on them (e.g. the API key header).
        client.client_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(llm.httpx, "AsyncClient", _factory)


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

    def test_stream_chat_inband_server_error_logged_with_its_message(self, monkeypatch, caplog):
        """llama.cpp signals a mid-stream failure (e.g. its tool-call parser
        rejecting the model's output) as an in-band error object — valid JSON,
        no 'choices' key, HTTP still 200. The log must carry the server's own
        message, not the old cryptic "Malformed SSE chunk ... 'choices'"."""
        # GIVEN a stream that receives an in-band error object mid-flight:
        lines = [
            sse_line({"choices": [{"delta": {"role": "assistant"}}]}),
            sse_line({"error": {
                "code": 500,
                "message": "The model produced output that does not match "
                           "the expected peg-native format",
                "type": "server_error",
            }}),
            token_line("hi"),
            finish_line("stop"),
        ]
        patch_llm_client(monkeypatch, FakeLLMClient(lines))

        # WHEN the stream is consumed,
        with caplog.at_level(logging.WARNING):
            tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN the remaining tokens still arrive and the server's own error
        # message (not a bare KeyError) is in the log:
        assert tokens == ["hi"]
        assert "LLM server error mid-stream" in caplog.text
        assert "peg-native format" in caplog.text
        assert "Malformed SSE chunk from LLM: 'choices'" not in caplog.text

    def test_stream_chat_chunk_without_choices_or_error_is_malformed(self, monkeypatch, caplog):
        """A non-dict JSON value (previously an uncaught TypeError) and a dict
        without either key are logged as malformed, not fatal."""
        lines = [
            sse_line({"foo": "bar"}),
            "data: 42",
            token_line("ok"),
            finish_line("stop"),
        ]
        patch_llm_client(monkeypatch, FakeLLMClient(lines))

        with caplog.at_level(logging.WARNING):
            tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        assert tokens == ["ok"]
        assert caplog.text.count("no 'choices' key") == 2

    def test_stream_chat_choices_not_a_list_is_logged_and_skipped(self, monkeypatch, caplog):
        """A PRESENT but wrongly-typed 'choices' value (string or object)
        must be logged and skipped — before the shape validation it was
        yielded as chunk['choices'][0] (a single CHARACTER for a string)
        and crashed downstream with AttributeError instead of the
        documented log-and-skip behaviour."""
        lines = [
            sse_line({"choices": "oops"}),
            sse_line({"choices": {"weird": True}}),
            token_line("ok"),
            finish_line("stop"),
        ]
        patch_llm_client(monkeypatch, FakeLLMClient(lines))

        with caplog.at_level(logging.WARNING):
            tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        assert tokens == ["ok"]
        assert caplog.text.count("'choices' not a list") == 2

    def test_stream_chat_choices_first_element_not_a_dict_is_logged_and_skipped(
        self, monkeypatch, caplog
    ):
        """A list whose first element is not an object (scalar items) must
        be logged and skipped — it would otherwise be yielded and crash
        downstream on .get('delta')."""
        lines = [
            sse_line({"choices": ["not-a-dict"]}),
            sse_line({"choices": [42]}),
            token_line("ok"),
            finish_line("stop"),
        ]
        patch_llm_client(monkeypatch, FakeLLMClient(lines))

        with caplog.at_level(logging.WARNING):
            tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        assert tokens == ["ok"]
        assert caplog.text.count("'choices[0]' not an object") == 2

    def test_stream_chat_retries_once_when_stream_aborts_before_any_content(self, monkeypatch, caplog):
        # GIVEN the first stream ends with an in-band error (no content, no
        # finish_reason) and the second completes normally:
        aborted = [
            sse_line({"choices": [{"delta": {"role": "assistant"}}]}),
            sse_line({"error": {"code": 500, "message": "boom", "type": "server_error"}}),
        ]
        clean = [token_line("hi"), finish_line("stop")]

        class RetryClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                lines = aborted if len(self.payloads) == 1 else clean
                return FakeStreamResponse(lines)

        client = RetryClient([])
        patch_llm_client(monkeypatch, client)

        # WHEN the chat stream is consumed,
        with caplog.at_level(logging.WARNING):
            tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN the request was sent exactly twice (one retry) with an
        # identical payload, and the caller only ever sees the retry's tokens:
        assert len(client.payloads) == 2
        assert client.payloads[0] == client.payloads[1]
        assert tokens == ["hi"]
        assert "retrying once" in caplog.text

    def test_stream_chat_does_not_retry_when_content_already_streamed(self, monkeypatch):
        # GIVEN a stream that delivers tokens and then dies mid-stream:
        lines = [
            token_line("Hel"),
            sse_line({"error": {"code": 500, "message": "boom", "type": "server_error"}}),
        ]
        client = FakeLLMClient(lines)
        patch_llm_client(monkeypatch, client)

        # WHEN the stream is consumed,
        tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN the partial reply is returned as-is — re-sending would
        # duplicate tokens the caller has already consumed:
        assert len(client.payloads) == 1
        assert tokens == ["Hel"]

    def test_stream_chat_does_not_retry_a_clean_empty_stop(self, monkeypatch):
        # GIVEN a stream that ends in a legitimate empty completion
        # (finish_reason present, no content):
        client = FakeLLMClient([finish_line("stop")])
        patch_llm_client(monkeypatch, client)

        # WHEN the chat stream is consumed,
        tokens = _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN it is never retried — a healthy stop is not an abort:
        assert tokens == []
        assert len(client.payloads) == 1

    def test_stream_chat_raises_when_both_attempts_abort(self, monkeypatch):
        # GIVEN a server that aborts every stream (in-band error, no
        # content, no finish_reason):
        aborted = [
            sse_line({"choices": [{"delta": {"role": "assistant"}}]}),
            sse_line({"error": {"code": 500, "message": "boom", "type": "server_error"}}),
        ]
        client = FakeLLMClient(aborted)
        patch_llm_client(monkeypatch, client)

        # WHEN the chat stream is consumed,
        with pytest.raises(llm.LLMStreamAborted):
            _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN exactly one retry happened (no retry storm) and the caller
        # sees the exception, not a silent empty reply the chat router
        # would persist into history:
        assert len(client.payloads) == 2
        assert client.payloads[0] == client.payloads[1]


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
# API key (docs/feature_api_key.md)
# ---------------------------------------------------------------------------

class TestLlmClientHeaders:
    """The optional Authorization: Bearer header on LLM calls."""

    def test_stream_chat_sends_bearer_header_when_key_from_env(self, monkeypatch):
        # GIVEN an API key in the environment:
        monkeypatch.setenv(llm_auth.ENV_VAR_NAME, "sk-test-123")
        llm_auth.invalidate_llm_api_key()
        client = FakeLLMClient([token_line("x"), "data: [DONE]"])
        patch_llm_client(monkeypatch, client)

        # WHEN a streamed chat request is made,
        _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN the client carries the Bearer header:
        assert client.client_kwargs["headers"] == {"Authorization": "Bearer sk-test-123"}

    def test_chat_completion_sends_bearer_header_when_key_from_file(self, monkeypatch, tmp_path):
        # GIVEN a key file in the (tmp) project root — the autouse fixture
        # points llm_auth._PROJECT_ROOT at this same tmp_path:
        (tmp_path / llm_auth.KEY_FILENAME).write_text(
            "# a comment line\nllm_api_key = sk-file-456\n", encoding="utf-8",
        )
        llm_auth.invalidate_llm_api_key()
        resp = json_response(200, {"choices": [{"message": {"content": "Luna"}}]})
        client = FakeLLMClient([], post_response=resp)
        patch_llm_client(monkeypatch, client)

        # WHEN a non-streaming router call is made,
        _run(llm.chat_completion([{"role": "user", "content": "pick"}], max_tokens=16))

        # THEN the client carries the Bearer header:
        assert client.client_kwargs["headers"] == {"Authorization": "Bearer sk-file-456"}

    def test_stream_chat_sends_no_auth_header_without_key(self, monkeypatch):
        # GIVEN no env var and no key file (the fixture defaults):
        monkeypatch.delenv(llm_auth.ENV_VAR_NAME, raising=False)
        llm_auth.invalidate_llm_api_key()
        client = FakeLLMClient([token_line("x"), "data: [DONE]"])
        patch_llm_client(monkeypatch, client)

        # WHEN a streamed chat request is made,
        _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN no Authorization header is sent at all:
        assert client.client_kwargs.get("headers") is None

    def test_api_key_never_reaches_the_log(self, monkeypatch, caplog):
        # GIVEN a sentinel key in the environment:
        monkeypatch.setenv(llm_auth.ENV_VAR_NAME, "sk-secret-do-not-log")
        llm_auth.invalidate_llm_api_key()
        client = FakeLLMClient([token_line("x"), "data: [DONE]"])
        patch_llm_client(monkeypatch, client)

        # WHEN the key is loaded and a request is made,
        with caplog.at_level(logging.INFO):
            _collect(llm.stream_chat([{"role": "user", "content": "hi"}]))

        # THEN no log record contains the key value:
        assert "sk-secret-do-not-log" not in caplog.text


class TestWarnIfPlaintextLlm:
    """The once-per-URL cleartext warning for http:// LLM endpoints."""

    def test_http_url_logs_cleartext_warning(self, caplog):
        # GIVEN a plain-http LLM base URL,
        with caplog.at_level(logging.WARNING):
            # WHEN the warning check runs,
            llm.warn_if_plaintext_llm("http://llm.local:8080")
        # THEN the exact warning is logged:
        assert "Warning: your LLM connection uses http; your chats are sent in cleartext." in caplog.text

    def test_https_url_logs_nothing(self, caplog):
        # GIVEN an https LLM base URL,
        with caplog.at_level(logging.WARNING):
            llm.warn_if_plaintext_llm("https://llm.local:8080")
        # THEN no cleartext warning is logged:
        assert "cleartext" not in caplog.text

    def test_none_or_blank_url_logs_nothing(self, caplog):
        # GIVEN a missing or blank base URL,
        with caplog.at_level(logging.WARNING):
            llm.warn_if_plaintext_llm(None)
            llm.warn_if_plaintext_llm("   ")
        # THEN nothing is logged:
        assert "cleartext" not in caplog.text

    def test_warning_is_logged_once_per_distinct_url(self, caplog):
        # GIVEN several http base URLs, two of them identical,
        with caplog.at_level(logging.WARNING):
            llm.warn_if_plaintext_llm("http://a.local:1")
            llm.warn_if_plaintext_llm("http://a.local:1")  # duplicate: deduped
            llm.warn_if_plaintext_llm("http://b.local:2")

        # THEN one warning per distinct URL:
        warnings = [r for r in caplog.records if "cleartext" in r.getMessage()]
        assert len(warnings) == 2


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
                _tool_persona(),
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

        events = _collect(
            llm.stream_chat_with_tools([{"role": "user", "content": "time?"}], [], _tool_persona())
        )

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

        events = _collect(
            llm.stream_chat_with_tools([{"role": "user", "content": "time?"}], [], _tool_persona())
        )

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

        events = _collect(
            llm.stream_chat_with_tools([{"role": "user", "content": "boom?"}], [], _tool_persona())
        )

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

        events = _collect(
            llm.stream_chat_with_tools(
                [{"role": "user", "content": "loop"}], tool_list, _tool_persona(),
            )
        )

        # Two LLM rounds total: the allowed tool round + the forced final one.
        assert len(client.payloads) == 2
        assert client.payloads[0]["tools"] == tool_list  # round 0 offers tools
        assert "tools" not in client.payloads[1]  # final round is tool-less
        # The final round's tool call was dropped, not executed.
        tool_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_events) == 1
        assert events[-1]["type"] == "tool_call"
        assert tool_events[0]["failed"] is False

    def test_builtin_tool_runs_locally_against_persona_never_mcp(self, monkeypatch, tmp_path):
        """add_memory is a built-in: it executes against the persona's own
        directory, the MCP registry is never consulted, and the result
        (the saved-memory confirmation) is fed back into the loop."""
        from app.services import mcp_client

        mcp_calls = []

        async def fake_call_tool(server_cfg, tool_name, arguments):
            mcp_calls.append(tool_name)

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

        class RoundClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                if len(self.payloads) == 1:
                    lines = [
                        tool_call_delta_line(
                            0, id="c1", type="function",
                            function={"name": builtin.ADD_MEMORY_NAME,
                                      "arguments": '{"memory": "The user likes tea."}'},
                        ),
                        finish_line("tool_calls"),
                    ]
                else:
                    lines = [token_line("Noted."), finish_line("stop")]
                return FakeStreamResponse(lines)

        client = RoundClient([])
        patch_llm_client(monkeypatch, client)
        persona = _tool_persona(tmp_path)

        events = _collect(
            llm.stream_chat_with_tools(
                [{"role": "user", "content": "remember that I like tea"}],
                [builtin.ADD_MEMORY_SPEC],
                persona,
            )
        )

        tool_event = next(e for e in events if e["type"] == "tool_call")
        assert tool_event["tool_name"] == builtin.ADD_MEMORY_NAME
        assert tool_event["arguments"] == {"memory": "The user likes tea."}
        assert tool_event["failed"] is False
        assert tool_event["result"] == "The memory was saved successfully."
        assert mcp_calls == []  # no MCP server was ever contacted
        # The memory landed in the persona's own directory...
        assert (persona.persona_dir / "memories.txt").read_text() == "The user likes tea.\n"
        # ...and the confirmation went back to the LLM as the tool result.
        tool_result_msg = next(
            m for m in client.payloads[1]["messages"] if m.get("role") == "tool"
        )
        assert tool_result_msg["content"] == "The memory was saved successfully."

    def test_aborted_round_retried_once_then_tool_loop_proceeds(self, monkeypatch):
        """Round 1 dies mid-stream (in-band error, nothing yielded). The
        same conversation is re-sent once; the retry's tool call then drives
        the loop normally."""
        from app.services import mcp_client, tool_registry
        from tests.factories import make_mcp_server

        tool_registry._server_map["get_time"] = make_mcp_server()

        async def fake_call_tool(server_cfg, tool_name, arguments):
            return "It is noon."

        monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

        aborted = [
            sse_line({"choices": [{"delta": {"role": "assistant"}}]}),
            sse_line({"error": {"code": 500, "message": "boom", "type": "server_error"}}),
        ]
        tool_call = [
            tool_call_delta_line(0, id="c1", type="function",
                                 function={"name": "get_time",
                                           "arguments": '{"zone": "utc"}'}),
            finish_line("tool_calls"),
        ]
        final_text = [token_line("ok"), finish_line("stop")]
        sequences = [aborted, tool_call, final_text]

        class RetryClient(FakeLLMClient):
            def stream(self, method, url, json=None):
                self.payloads.append(json)
                index = min(len(self.payloads) - 1, len(sequences) - 1)
                return FakeStreamResponse(sequences[index])

        client = RetryClient([])
        patch_llm_client(monkeypatch, client)

        events = _collect(
            llm.stream_chat_with_tools(
                [{"role": "user", "content": "time?"}],
                [{"type": "function", "function": {"name": "get_time"}}],
                _tool_persona(),
            )
        )

        assert [e["type"] for e in events] == ["tool_call", "token"]
        # Three requests: the aborted attempt, its identical retry, and the
        # round after the tool result was fed back.
        assert len(client.payloads) == 3
        assert client.payloads[1]["messages"] == client.payloads[0]["messages"]
        assert "tools" in client.payloads[0] and "tools" in client.payloads[1]

    def test_persistently_aborted_round_retried_exactly_once_then_raises(self, monkeypatch):
        # GIVEN a server that aborts every attempt (no content, no tool
        # calls, no finish_reason):
        aborted = [
            sse_line({"choices": [{"delta": {"role": "assistant"}}]}),
            sse_line({"error": {"code": 500, "message": "boom", "type": "server_error"}}),
        ]
        client = FakeLLMClient(aborted)
        patch_llm_client(monkeypatch, client)

        # WHEN the loop runs,
        with pytest.raises(llm.LLMStreamAborted):
            _collect(
                llm.stream_chat_with_tools([{"role": "user", "content": "hi"}], [], _tool_persona())
            )

        # THEN the round is retried exactly once (no retry storm) and the
        # abort is RAISED, not returned as an empty plain-text reply —
        # the chat router turns the exception into a visible error event
        # and skips persistence, so the double abort can never surface as
        # a silently persisted empty row in history:
        assert len(client.payloads) == 2
        assert client.payloads[0] == client.payloads[1]

    def test_aborted_round_with_partial_tool_call_is_not_retried(self, monkeypatch):
        """A round that died AFTER tool-call deltas started has in-flight
        state; retrying it would risk double-execution. It keeps the
        existing truncation-refusal handling instead."""
        from app.config import MCPConfig

        monkeypatch.setattr(
            app_config, "_settings_cache",
            make_settings(mcp=MCPConfig(max_tool_iterations=1)),
        )

        # The same aborted lines serve every request — if a retry happened
        # this test would need a third sequence to absorb it.
        aborted = [
            tool_call_delta_line(0, id="c1", type="function",
                                 function={"name": "get_time",
                                           "arguments": '{"zone": "ut'}),
            # stream ends here: no finish_reason — the server aborted mid-call
        ]
        client = FakeLLMClient(aborted)
        patch_llm_client(monkeypatch, client)

        events = _collect(
            llm.stream_chat_with_tools([{"role": "user", "content": "time?"}], [], _tool_persona())
        )

        # Round 0: refused (invalid JSON, no max_tokens hint — the abort
        # carries no finish_reason), NOT retried. The final tool-less round
        # drops the call without executing it.
        assert len(client.payloads) == 2
        assert [e["type"] for e in events] == ["tool_call"]
        assert events[0]["failed"] is True
        assert "not valid JSON" in events[0]["result"]
        assert "max_tokens" not in events[0]["result"]
