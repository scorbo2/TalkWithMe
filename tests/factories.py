"""Shared test factories and fake HTTP clients.

Everything a test needs to build config objects, parse SSE streams, and
stand in for the LLM/TTS/STT/MCP servers lives here so individual test
modules stay focused on behaviour, not plumbing.
"""

import json
import json as _json  # alias: FakeMCPClient.post has a `json` *parameter* that
                     # shadows the module, so its SSE handlers use _json.dumps
from typing import Any, Callable, List, Optional

import httpx

from app.config import (
    AppSettings,
    ChatRoom,
    ChatRoomsConfig,
    GeneralConfig,
    LLMSettings,
    MCPConfig,
    MCPServerConfig,
    Persona,
    PersonasConfig,
    STTConfig,
    TTSConfig,
)


# ---------------------------------------------------------------------------
# Config factories
# ---------------------------------------------------------------------------

def make_settings(
    *,
    llm: Optional[LLMSettings] = None,
    tts: Optional[TTSConfig] = None,
    stt: Optional[STTConfig] = None,
    general: Optional[GeneralConfig] = None,
    mcp: Optional[MCPConfig] = None,
) -> AppSettings:
    """Build an AppSettings with sane test defaults (TTS/STT inactive)."""
    return AppSettings(
        llm=llm or LLMSettings(base_url="http://llm.local:8080", model="test-model"),
        tts=tts or TTSConfig(enabled=False, base_url=None),
        stt=stt or STTConfig(enabled=False, base_url=None),
        general=general or GeneralConfig(),
        mcp=mcp or MCPConfig(),
    )


def make_personas() -> PersonasConfig:
    """Two stock personas: Alex (TTS-incapable) and Luna (TTS-capable)."""
    return PersonasConfig(
        personas=[
            Persona(
                name="Alex",
                description="A friendly assistant",
                system_prompt="You are Alex, a friendly assistant.",
                router_hints="general questions",
            ),
            Persona(
                name="Luna",
                description="A philosophical poet",
                system_prompt="You are Luna, a philosophical poet.",
                router_hints="philosophy, feelings",
                reference_audio="reference/luna.wav",
                reference_audio_transcript="reference/luna.txt",
                reference_audio_language="en",
            ),
        ]
    )


def make_chatrooms() -> ChatRoomsConfig:
    """One stock chat room containing both personas."""
    return ChatRoomsConfig(
        chat_rooms=[ChatRoom(name="TNG", persona_names=["Alex", "Luna"], echo_chamber=False)]
    )


def make_mcp_server(name: str = "tools-1", url: str = "http://mcp.local:9000") -> MCPServerConfig:
    return MCPServerConfig(name=name, url=url, timeout=5.0)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def parse_sse_events(body: str) -> List[dict]:
    """Parse an SSE response body into a list of event dicts."""
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def sse_events_by_type(events: List[dict], event_type: str) -> List[dict]:
    return [e for e in events if e.get("type") == event_type]


# ---------------------------------------------------------------------------
# Fake httpx clients
# ---------------------------------------------------------------------------

class FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient (non-streaming).

    `responder` is called as responder(method, url, **request_kwargs) and
    must return an httpx.Response or raise. All calls are recorded in
    `.calls` for assertions.
    """

    def __init__(self, responder: Callable[..., httpx.Response], *args, **kwargs):
        self.responder = responder
        self.calls: List[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def _record(self, method: str, url: str, kwargs: dict) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        response = self.responder(method, url, **kwargs)
        # Ensure the response carries a request: httpx 0.24 raises if
        # response.url / raise_for_status() are touched without one.
        # (Note: the public .request getter raises when unset, so check
        # the private attribute.)
        if response._request is None:
            response.request = httpx.Request(method, url)
        return response

    async def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)


def json_response(
    status_code: int,
    payload: Any,
    headers: Optional[dict] = None,
    method: str = "POST",
    url: str = "http://fake.local/rpc",
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        headers=headers or {},
        request=httpx.Request(method, url),
    )


def sse_response(
    status_code: int,
    body: str,
    method: str = "POST",
    url: str = "http://fake.local/rpc",
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/event-stream"},
        request=httpx.Request(method, url),
    )


class FakeStreamResponse:
    """Mimics the context manager returned by httpx.AsyncClient.stream()."""

    def __init__(self, lines: List[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://fake"),
                response=httpx.Response(self.status_code),
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeLLMClient:
    """httpx.AsyncClient stand-in for the LLM endpoints.

    `lines` is the list of SSE lines served for every streamed request;
    `payloads` records the JSON payload of each request. For non-streaming
    calls, pass `post_response` (used by chat_completion).
    """

    def __init__(
        self,
        lines: List[str],
        post_response: Optional[httpx.Response] = None,
        *args,
        **kwargs,
    ):
        self.lines = lines
        self.post_response = post_response
        self.payloads: List[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url, json=None):
        self.payloads.append(json)
        return FakeStreamResponse(self.lines)

    async def post(self, url, json=None):
        self.payloads.append(json)
        if self.post_response is None:
            raise RuntimeError("FakeLLMClient: post called without post_response")
        response = self.post_response
        if response._request is None:  # public getter raises when unset
            response.request = httpx.Request("POST", url)
        return response


class FakeMCPClient:
    """httpx.AsyncClient stand-in for an MCP server (Streamable HTTP).

    Routes on the JSON-RPC method in the request body:
      initialize               -> serverInfo result (+ Mcp-Session-Id header)
      notifications/initialized-> 202 empty
      tools/list               -> `tools` (MCP shape)
      tools/call               -> `call_result` (MCP result object)

    `posts` records (url, json_body, headers) for assertions.
    """

    def __init__(
        self,
        tools: Optional[List[dict]] = None,
        call_result: Optional[dict] = None,
        call_result_sse: bool = False,
        session_id: Optional[str] = "sess-123",
        *args,
        **kwargs,
    ):
        self.tools = tools if tools is not None else []
        self.call_result = call_result
        self.call_result_sse = call_result_sse
        self.session_id = session_id
        self.posts: List[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json, dict(headers or {})))
        method = json.get("method")
        req = httpx.Request("POST", url)

        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": json["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "serverInfo": {"name": "fake-server", "version": "1.0"},
                    },
                },
                headers={"Mcp-Session-Id": self.session_id} if self.session_id else {},
                request=req,
            )

        if method == "notifications/initialized":
            return httpx.Response(202, request=req)

        if method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": json["id"],
                "result": {"tools": self.tools},
            }
            if self.call_result_sse:
                return sse_response(200, f"data: {_json.dumps(payload)}\n\n")
            return json_response(200, payload)

        if method == "tools/call":
            payload = {
                "jsonrpc": "2.0",
                "id": json["id"],
                "result": self.call_result if self.call_result is not None else {},
            }
            if self.call_result_sse:
                return sse_response(200, f"data: {_json.dumps(payload)}\n\n")
            return json_response(200, payload)

        raise ValueError(f"FakeMCPClient: unexpected method {method!r}")
