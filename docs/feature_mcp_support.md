# MCP Server Support — Implementation Plan

## Problem

TalkWithMe personas have no ability to call external tools. We want to add MCP (Model Context
Protocol) server support so that personas can invoke tools from remote MCP servers during a chat
response, using a fully agentic loop in the backend.

## Approach

1. Add `allow_tool_calls: bool = False` to each `Persona` in `personas.yaml` / `app/config.py`.
2. Add an `mcp` section to `settings.yaml` with a list of named MCP server configs (SSE/HTTP transport only) and a `max_tool_iterations` cap (default 8).
3. On startup, query all configured MCP servers for their tool lists and cache them. Log warning on tool enumeration errors, and continue without those tools.
4. When a persona with `allow_tool_calls=True` responds, pass the cached tool list to the LLM.
5. Run an agentic loop: if the LLM returns a `tool_calls` response, invoke the tool on the appropriate MCP server, append the result, and re-query the LLM. Loop until a text response is produced or `max_tool_iterations` is reached.
6. Stream `tool_call` SSE events to the frontend for each tool invocation (with a new general setting `show_tool_calls: bool = True` to control visibility in the UI).

---

## Todos

### 1. Config: Persona `allow_tool_calls` field
- Add `allow_tool_calls: bool = False` to `Persona` in `app/config.py`.
- Add it to `PersonaCreateRequest`, `PersonaUpdateRequest`, `PersonaDetailResponse` in `app/models.py`.
- Update `save_personas()` to serialise it.
- Update the persona editor modal to expose this field as a checkbox.

### 2. Config: MCP section in `AppSettings`
- Add `MCPServerConfig(BaseModel)` with fields: `name`, `url`, `timeout: float = 10.0`.
- Add `MCPConfig(BaseModel)` with fields: `servers: List[MCPServerConfig]`, `max_tool_iterations: int = 8`.
- Add `mcp: MCPConfig = MCPConfig()` to `AppSettings`.
- Update `load_settings()` to parse the new section.
- Update `save_settings()` to serialise it.
- **Do not** expose `mcp` in the Settings UI (yaml-only for now).

### 3. Config: `show_tool_calls` in `GeneralConfig`
- Add `show_tool_calls: bool = True` to `GeneralConfig` in `app/config.py`.
- Add it to `GeneralSettingsRequest` and `GeneralSettingsResponse` in `app/models.py`.
- Add it to the General Settings modal in `static/gen-settings.js` (toggle checkbox).

### 4. MCP client service (`app/services/mcp_client.py`)
- Implement async HTTP client for MCP SSE/HTTP transport.
- `discover_tools(server: MCPServerConfig) -> List[dict]` — GET `/tools/list` (or MCP tool discovery endpoint), return OpenAI-format tool definitions.
- `call_tool(server: MCPServerConfig, tool_name: str, arguments: dict) -> str` — POST to the MCP tool call endpoint, return result as a string.
- Handle errors gracefully (log and return empty list / error string).

### 5. Tool registry (`app/services/tool_registry.py`)
- Module-level cache: `_tool_cache: dict[str, List[dict]]` (server name → tools) and `_server_map: dict[str, MCPServerConfig]` (tool name → server).
- `async load_tools() -> None` — query all configured MCP servers at startup, populate cache.
- `get_all_tools() -> List[dict]` — return the full merged tool list for the LLM.
- `get_server_for_tool(tool_name: str) -> Optional[MCPServerConfig]` — look up which server owns a tool.

### 6. Startup: initialise tool registry
- In `app/main.py` `lifespan`, after loading settings: call `await load_tools()`.
- Log the number of tools discovered and which servers responded.

### 7. Agentic tool-call loop in LLM service (`app/services/llm.py`)
- Add `stream_chat_with_tools(messages, tools) -> AsyncGenerator[event_dict, None]` — wraps the agentic loop.
  - Pass `tools` in the LLM payload if non-empty.
  - On a `tool_calls` finish reason: yield a `{"type": "tool_call", ...}` event, call `mcp_client.call_tool()`, append a `tool` role message, and loop.
  - On a `stop` finish reason: yield token events as usual.
  - Respect `max_tool_iterations` from settings.
  - Yields dicts (not SSE strings) so `chat.py` can format them uniformly.

### 8. Chat router: wire up tool calls (`app/routers/chat.py`)
- In `_chat_stream`, after resolving the persona, check `persona.allow_tool_calls`.
- If True, use `stream_chat_with_tools` with `get_all_tools()` instead of `stream_chat`.
- Handle the new `tool_call` event dict: emit a `tool_call` SSE event. If `show_tool_calls` is False, suppress the SSE emission but still process the loop.
- Accumulate only `token` type events into `full_text` for persistence.

### 9. Frontend: `tool_call` SSE event handling (`static/chat.js`)
- In `handleSSEEvent()`, add a `"tool_call"` case.
- Render a distinct inline indicator in the chat bubble (e.g. a small chip/badge showing the tool name) when `show_tool_calls` is enabled.
- The indicator is non-interactive (display only).

### 10. Persona editor modal: `allow_tool_calls` toggle (`static/persona.js`)
- Add a checkbox "Allow tool calls" to the persona editor modal.
- Populate from `PersonaDetailResponse`, submit in `PersonaUpdateRequest`.

---

## Key decisions

- **MCP transport**: SSE/HTTP only. No stdio support.
- **Agentic loop**: fully in the backend; frontend receives only `tool_call` events and final tokens.
- **Tool call visibility**: controlled by `general.show_tool_calls` (default `true`), added to the General Settings modal.
- **Max iterations**: `mcp.max_tool_iterations` (default 8) in settings.yaml.
- **Tool list scope**: all tools from all configured servers are merged into one flat list. The LLM chooses which tool to call; the registry maps the tool name back to the right server.
- **No Settings UI for MCP servers**: servers are configured in `settings.yaml` directly.

---

## Files to change

| File | Changes |
|------|---------|
| `app/config.py` | Add `MCPServerConfig`, `MCPConfig`, update `AppSettings`, `Persona`, `GeneralConfig`, load/save functions |
| `app/models.py` | Add `allow_tool_calls` to persona models; add `show_tool_calls` to general settings models |
| `app/services/mcp_client.py` | **New** — MCP SSE/HTTP discovery + invocation |
| `app/services/tool_registry.py` | **New** — startup tool caching, tool→server mapping |
| `app/services/llm.py` | Add `stream_chat_with_tools()` with agentic loop |
| `app/routers/chat.py` | Wire tool-call path; emit `tool_call` SSE events conditionally |
| `app/main.py` | Call `load_tools()` on startup |
| `static/chat.js` | Handle `tool_call` SSE event, render tool-call indicator |
| `static/gen-settings.js` | Add `show_tool_calls` checkbox to General Settings modal |
| `static/persona.js` | Add `allow_tool_calls` checkbox to persona editor modal |
| `settings.yaml` | Add new `mcp` section with an initially empty list of servers |
| `README.md` | Document new MCP feature (include example `settings.yaml` mcp entry) |
| `AGENTS.md` | Add or update information in this file as needed |

