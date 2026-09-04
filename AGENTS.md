# TalkWithMe — Agent Instructions

## Run the app

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
deactivate
```

Requires a locally running **llama.cpp** server with an OpenAI-compatible API. TTS and STT servers are optional.
Open `http://localhost:8000` in a browser.

## Testing — pytest, fully offline

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest # config in pytest.ini
deactivate
```

- **No servers needed.** The suite is hermetic: every external HTTP endpoint (LLM, TTS, STT, MCP) is faked via `tests/factories.py` (fake `httpx.AsyncClient`s, config factories, SSE helpers). It must run — and pass — with nothing but Python installed.
- **Isolation**: `tests/conftest.py` has an autouse fixture that points every module-level global at per-test `tmp_path` state: the config caches in `app/config.py`, `_PERSISTENCE_ROOT` (in both `app/persistence.py` and `app/routers/persistence.py` — the router imported it *by value*, so it needs its own patch), the `session` singleton, the MCP tool registry, and the TTS capabilities cache in `app/services/tts_client.py`. Real `settings.yaml` / `Personas/` / `chatrooms.yaml` / `chatrooms/` data is never read or written. **If you add a new module-level global to the app, add it to that fixture.**
- **Lifespan**: the `client` fixture uses `TestClient` *without* the startup lifespan, because the lifespan re-reads the real YAML files (clobbering test caches) and attempts MCP discovery. Tests that exercise the lifespan do so explicitly with a local `TestClient` in a `with` block and monkeypatched `load_*`/`load_tools` (see `tests/test_main.py`).
- **Coverage map**: `test_config.py` (config models + YAML load/save), `test_persona_store.py` (persona directory discovery: frontmatter, file ops, language/avatar helpers, memory-file append/purge, YAML→dir migration), `test_builtin.py` (the built-in `add_memory` tool: registry, availability gating, save/error paths), `test_models.py` (API request/response models), `test_persistence.py` (disk persistence + audio staging), `test_session_manager.py`, `test_llm.py` (SSE parsing + agentic tool loop), `test_mcp_client.py`, `test_tool_registry.py`, `test_tts_stt_clients.py`, `test_chat_sse.py` (the `/api/chat` SSE endpoint — LLM stubbed, selection/persistence/echo/tool events for real), `test_main.py`, `test_docs.py` (the AGENTS.md API endpoints table must match the routes actually registered on the app — run it after adding/removing endpoints and update the table), one `test_routers_*.py` per API router, and `test_persona_form.js` (plain Node, **not** part of pytest — see below: the persona editor form logic in `static/persona.js`).
- **Frontend tests (Node)**: `tests/test_persona_form.js` runs with plain Node 20+ — no npm packages, no network: `node tests/test_persona_form.js`. It evaluates the real `static/utils.js` + `state.js` + `persona.js` in a fresh `vm.Context` per test against a minimal DOM stub, stubs `fetch` to capture the multipart `FormData`, and drives the real `openPersonaForm()`, Remove-button listeners, file-input change handlers, and `submitPersonaForm()`. It exists to lock in the invariants that `remove_avatar_image` / `remove_reference_audio` are sent **only** after an explicit "Remove" click — never derived from server-side file presence (that bug silently deleted a persona's avatar and reference audio on every plain text save) — and, by the same rule, that `clear_memories` is sent only after an explicit "Clear saved memories" click while `memory_size` is **always** sent (the update endpoint requires it; an omitted value must not silently reset the persona's memory budget). It is kept out of the pytest suite on purpose: pytest must stay runnable with nothing but Python installed.
- **Two expected deprecation warnings**: a clean run is "all tests pass", even when the `warnings summary` contains exactly two third-party deprecation warnings. Both come from Starlette 1.x's own testclient (pulled in via the `fastapi.testclient` import), not from app or test code: a `StarletteDeprecationWarning` that the httpx-based testclient will one day be replaced by a new `httpx2` package, and an `anyio.abc.BlockingPortal` alias deprecation raised from inside `starlette/testclient.py`. Neither is harmful today — the testclient works fine on httpx as of Starlette 1.6 — so don't chase them and don't "fix" app/test code in response. The eventual resolution, if it ever comes, is a future Starlette/httpx2 bump, not a code change here.
- **Rules**:
  - Every code change must be followed by a clean run: `.venv/bin/python -m pytest`, all green. No exceptions, no skipped tests. Changes to `static/persona.js` (or anything else covered by the Node tests) additionally require `node tests/test_persona_form.js` all green.
  - New functionality or API endpoints require new tests in the matching `test_*.py` file before the change is complete.
  - API tests use the `client` fixture + config caches (re-point `app.config._settings_cache` / `_personas_cache` / `_chatrooms_cache` via `monkeypatch`); router-level stubs are applied at the router's import site (e.g. `app.routers.chat.stream_chat`), since routers import service functions by name.
  - All fake responses in `tests/factories.py` attach an `httpx.Request` before returning: the httpx versions this project supports (0.24–0.28) raise `RuntimeError` from `response.url` / `raise_for_status()` if no `request` is attached to a `Response`, and the public `.request` *getter* itself raises when unset (check the private `response._request` attribute instead).

## Config — YAML files + a persona directory, cached at startup

| Source | Purpose |
|--------|---------|
| `settings.yaml` | LLM, TTS, STT endpoints and parameters, general chat parameters, MCP server list. `general.personas_directory` (default `Personas`) names the persona directory — **yaml-only**, no API/UI field |
| `Personas/` (one directory per persona) | Persona definitions — the single source of truth. See **Persona storage** below |
| `personas.yaml` | **Legacy only.** If present (and no `Personas/` dir), it is migrated to directories once at startup, then renamed to `personas.yaml.bak` and ignored forever |
| `chatrooms.yaml` | Chat room groupings (may not exist; code handles gracefully) |

All three are loaded once at startup and cached as module-level globals in `app/config.py`.
In request handlers, **always use** `get_settings()`, `get_personas()`, `get_chatrooms()` — never call `load_*()` directly.
To force a re-read of all three files, call `app.config.reload_all()`.
**Caveat:** `reload_all()` does NOT re-run MCP tool discovery — the tool cache in `app/services/tool_registry.py` is built once at startup. Changes to the `mcp:` section of settings.yaml require a full app restart.

## Persona storage

Personas live in `Personas/<Name>/` (directory = `sanitize_persona_dirname(name)`; name may differ, e.g. `O'Brien` → `OBrien`). All file I/O lives in `app/services/persona_store.py` (framework-agnostic; routers and config import from there).

Per-persona files:

| File | Content | Notes |
|------|---------|-------|
| `prompt.md` | YAML frontmatter (`description`, `router_hints`, `avatar_color`, `allow_tool_calls`, `memory_size`) + system prompt body | `name` is stored only when it differs from the directory name; parsed/serialized by `parse_frontmatter()` / `build_prompt_md()`. `memory_size` (0–16384 bytes, default 8192) is the persona's memory budget — `0` disables memories; an invalid/out-of-range value warns and falls back to the default. Malformed frontmatter degrades the whole file to the prompt body with a warning — never a crash |
| `memories.txt` | One memory per line (newlines inside a memory are flattened) | Persistent memory for the persona, written by the built-in `add_memory` tool and purged oldest-first when the file exceeds `memory_size`. Absent = no memories yet |
| `language.txt` | Single line: reference-audio language code | Absent → `en` (with warning) |
| `ref.wav` | TTS reference audio | Fixed filename — never user-chosen |
| `ref.txt` | Transcript of `ref.wav` | A persona is TTS-capable only when **both** are present (and non-blank) |
| `image.<ext>` | Avatar (png/jpg/jpeg/gif/webp) | One per persona; the editor's "replace image" uploads a new file and deletes the old one |

Startup decision matrix in `app/config.py::load_personas()` (lazy-imports `persona_store` to dodge a cycle):

| `personas.yaml` | `Personas/` dir | Behaviour |
|-----------------|-----------------|-----------|
| no | no | Create the (empty) dir, warn once; empty persona list |
| no | yes | Scan |
| yes | no | **Migrate** (see below) |
| yes | yes | Skip the YAML with a warning (dir wins); both sources kept on disk |

**Migration** (`persona_store.migrate_from_legacy_yaml()`): converts the old schema to per-persona directories. Each persona gets a `prompt.md` (frontmatter + system prompt) and `language.txt`, and the files referenced by its `avatar_image` / `reference_audio` / `reference_audio_transcript` **paths** are copied in as `image<ext>` / `ref.wav` / `ref.txt`. Success → the YAML is renamed to `personas.yaml.bak` so it never re-migrates. **Fatal** error (malformed YAML, unwritable directory, disk full) → raise `PersonaMigrationError` with the YAML left **untouched** and the partially created `Personas/` directory removed best-effort, so the next startup retries cleanly. **Minor** error (a referenced file missing, unreadable, or the wrong format — e.g. a non-wav `reference_audio`) → logged, that file skipped, migration continues. The YAML is the source of truth until the rename succeeds.

**Directory is never renamed.** A persona *name* change (editor) writes a new `prompt.md` `name:` field but keeps the directory; deleting a persona deletes the directory. `GET /api/personas` returns personas in raw directory/creation order — sorting is a frontend concern (see **Persona list ordering**).

## Architecture

- **Backend**: FastAPI. Entry point: `app/main.py`. Routers in `app/routers/`, external service clients in `app/services/`.
- **Session**: Single global `session` singleton in `app/session.py`. Intentional — this is a single-user app. No auth, no database — the `chatrooms/` directory is the only persistent storage. Tracks `current_room` and persists messages to disk automatically. `session.build_llm_messages()` constructs the per-call LLM payload: the responding persona's system prompt, then history remapped so user messages stay `user`, the responder's own messages stay `assistant`, and *other personas'* messages are re-mapped to `user` with a `[Name]: <text>` prefix — this avoids consecutive `assistant` messages (which many LLMs reject with 400) and stops the model treating another persona's words as its own. History is optionally capped at the last `general.max_turns_for_context` entries (default 6).
- **Persistence**: Per-room JSON + audio files under `chatrooms/<room>/`. Handled by `app/persistence.py` (framework-agnostic) and `app/routers/persistence.py` (audio upload/serving endpoints). Created lazily on first write.
- **MCP tools**: `app/services/mcp_client.py` speaks MCP (JSON-RPC 2.0 over the Streamable HTTP transport, protocol version 2025-03-26) with a *stateless, per-call* session — `initialize` runs on every discovery/call, no persistent sessions. `app/services/tool_registry.py` caches the discovered tools once at startup (called from `app/main.py` lifespan) and maps tool name → server; duplicate tool names across servers: first listed server wins. The cache is not refreshed by `reload_all()` — `mcp:` changes need a full restart. The `mcp:` section of `settings.yaml` is **yaml-only** (no UI, no API field) — `update_settings` in `app/routers/settings.py` copies it over from the current cache, otherwise a UI settings save would wipe it. Personas with `allow_tool_calls` run `stream_chat_with_tools()` in `app/services/llm.py` (agentic loop; the final round is sent without `tools` to force a text answer). Tool rounds are local to the loop — they are NOT persisted to the chat history. MCP server URLs are validated at config load (must start with `http://` or `https://`) so a scheme-less typo fails loudly at startup instead of surfacing as per-request connection timeouts.

- **Built-in tools**: `app/services/builtin.py` registers tools that run *locally* instead of over MCP (currently `add_memory`, which appends to the persona's `memories.txt` under its `memory_size` budget). They are offered on top of the MCP tools to tool-enabled personas, are dispatched before any MCP lookup in `stream_chat_with_tools()`, and never touch the network. Built-in names are **reserved**: `load_tools()` silently skips (with a warning) any MCP server advertising a built-in name. Availability is per persona (`memory_size > 0`) and gated by `general.enable_persona_memories` — see **Persona memories** in Chat flow.
- **Settings update contract**: `PUT /api/settings` treats the `general:` section as a *partial update* — omitted fields (or an absent `general` section) keep their current values; only fields explicitly sent override. `GeneralSettingsRequest` in `app/models.py` uses `Optional` fields defaulting to `None`, and `update_settings` in `app/routers/settings.py` merges via `model_dump(exclude_none=True)`. Dialogs that don't edit general settings (the Servers dialog) must NOT send a `general` section, and the router must NOT rebuild `GeneralConfig` from request-body defaults — doing so is exactly how `show_tool_calls` got reset to `true` on every Servers-dialog save. If you add a new field to `GeneralConfig`, the merge preserves it automatically; no per-field wiring needed. The LLM/TTS/STT sections are full replacements, normalized on save: blank base URLs → `None` (which deactivates the feature via `is_active`). Changes take effect immediately — no restart. TTS engine parameters are a generic untyped map, `tts.parameters` (name → value; the hard-coded `num_steps`/`guidance_scale`/`seed` fields are gone — see `docs/feature_TTS_generification.md`): a legacy `tts:` block in settings.yaml is folded into `parameters` at **load** time (`TTSConfig` before-validator, `app/config.py`), so the old keys leave disk on the first save; "absent key = let the engine decide" replaced the seed 0→None convention. On save, `tts.parameters` is 422-validated against the cached TTS capabilities doc — **only** when that doc belongs to the exact `base_url` being saved (stale doc after an engine switch, negative cache, or empty cache → skip; the server's own 422 is the backstop), and a successful save always calls `tts_client.invalidate_capabilities()` (slot clear only, no inline refetch — the save path is synchronous and offline-safe).
- **Logging**: `app/main.py` configures logging via `logging.basicConfig(level=..., ...)` at import time (a no-op if the root logger already has handlers), because uvicorn's default config leaves the root logger at WARNING, which silently swallows every app `logger.info()` call (this is why per-server MCP discovery lines were invisible while failure `WARNING`s showed). The level defaults to INFO but is overridden for the run by `TALKWITHME_LOG_LEVEL` (a standard level name, case-insensitive; see `app/main.py::_resolve_root_log_level` and the README "Logging" section) — an invalid value warns and falls back to INFO. The `httpx` logger is separately pinned to WARNING because it logs one line per HTTP request. Note: `uvicorn --log-level` only affects uvicorn's own loggers, not the app's.
- **Frontend**: Vanilla JS SPA, no bundler. Modules in `static/` communicate via shared globals in `state.js`. See the table below:

| File | Responsibility |
|------|---------------|
| `state.js` | Shared globals (personas, session state, chat room state, TTS/STT flags, audio queue, message IDs) |
| `app.js` | Bootstrap, health checks, event listener setup, session management |
| `chat.js` | Message rendering, SSE stream handling (incl. `tool_call` chips), sending messages, persisted history rendering, audio playback buttons |
| `persistence.js` | History loading, audio upload helpers, audio URL generation |
| `persona.js` | Persona sidebar + editor modal (CRUD, incl. memory size field + "Clear saved memories") |
| `chatrooms.js` | Chat room dropdown, room filtering, room editor (incl. echo chamber toggle), persona picker modal, room switching with history load |
| `settings.js` | Servers modal (LLM/TTS/STT config) |
| `gen-settings.js` | General settings modal (max persona replies, name mentions, context turns, tool-call visibility, persona memories toggle) |
| `tts.js` | TTS synthesis, audio queues, Web Audio playback, audio persistence |
| `stt.js` | Microphone recording, STT proxy, transcript insertion, audio persistence |
| `theme.js` | Theme toggle |
| `utils.js` | Shared helpers (incl. `comparePersonasByName()`, the shared case-insensitive persona-name comparator) |

**Persona list ordering**: Anywhere a list of personas is shown to the user (the sidebar, the persona editor modal, the persona picker modal, and anything added in the future), it must be sorted alphabetically and case-insensitively using `comparePersonasByName()` from `utils.js`. The shared `personas` global is pre-sorted in `loadPersonas()` (`app.js`), but each render function sorts its own input list rather than trusting the caller's order. **Do not** rely on `GET /api/personas` returning any particular order — the backend intentionally returns personas in raw directory/creation order; sorting is a display concern and belongs in the frontend only.

## Chat flow

`POST /api/chat` streams SSE. Every event is a single `data: <JSON>\n\n` line; the `type` field is always present, and the frontend switches on it in `handleSSEEvent()` in `chat.js`. Event types: `start`, `token`, `tool_call`, `done`, `error`, `complete`.
The request body includes `chat_room` (which room to persist to) and `message_id` (frontend-generated UUID for audio association).

**Persona pool.** `_resolve_room_personas()` in `app/routers/chat.py` is the authoritative source of eligibility: the `"default"` room (or any room not present in `chatrooms.yaml`) includes all personas; a named room is limited to its assigned personas, filtered to those that still exist. It deliberately does NOT read the frontend-maintained `session.active_personas`.

**Persona selection modes:** `"router"` (LLM picks via a non-streaming `chat_completion()` at `temperature=0.1`, `max_tokens=16`; the prompt includes each eligible persona's `router_hints` plus the last `max_turns_for_context` turns of context; an unrecognized name or LLM failure falls back to random), `"random"` (uniform over eligible personas), or an explicit persona name (validated against the room; unknown values fall back to random).

**Multiple replies.** `general.max_persona_replies` (1–4, capped at the number of eligible personas) controls how many personas answer one user message: multiple `start`/`token`/`done` cycles, one per responding persona. The first responder comes from the selection mode; each subsequent one is a random non-repeating pick from the remaining eligible personas.

**Message IDs.** Both `start` and `done` carry `message_id` (server-generated UUID for that persona's assistant message); `start` also carries `user_message_id` (the frontend's ID, echoed back).
`start` carries the assistant ID first **on purpose**: the frontend stamps it onto streaming TTS items at enqueue time, so the ID must be generated *before* the `start` event is emitted — moving it back after the stream reintroduces cross-turn audio misattribution.

**Echo chamber.** Each room has an `echo_chamber` flag (set via `PUT /api/chatrooms/{name}/echo-chamber`, toggled in the room editor; the `default` room cannot be modified). When enabled for the active room, the LLM is bypassed entirely: exactly one persona (picked per the normal selection mode) echoes the user's message verbatim as a single `token` event, and `max_persona_replies` is forced to 1.

**Tool calls.** `tool_call` is only emitted while a tool-enabled persona's agentic loop is running, and only when `general.show_tool_calls` is true (the server suppresses the event, not the frontend); payload: `{type, persona, tool_name, arguments, result, failed}`. `failed` is a server-computed boolean (tool error, unknown tool, or unparseable/truncated arguments) — the frontend styles the chip from it, not by sniffing the result string. Tool calls whose arguments are not valid JSON (typically truncated at `max_tokens`) are never executed; the LLM receives an `Error: ...` result and can retry. The agentic loop is capped at `mcp.max_tool_iterations` rounds (default 8) per persona reply.

**Persona memories.** Each persona has a `memory_size` budget (0–16384 bytes, default 8192, 0 = disabled) stored in its `prompt.md` frontmatter, plus a `memories.txt` with one flattened line per memory. Two global/per-persona gates control the feature: `general.enable_persona_memories` (default true) and the persona's own budget > 0. When both pass, the chat router appends the persona's current memories to its system prompt before every LLM call, and the built-in `add_memory` tool is offered to tool-enabled personas — its success/error strings are returned to the LLM verbatim (it never sees exceptions). `memories.txt` is purged oldest-first whenever it exceeds the budget: on save (in `add_memory`), best-effort on persona update when the budget is lowered, and on the read path before memory injection (via `persona_store.purge_memories_to_limit`, a cheap no-op when the file is within budget) — so a file inflated by an external editor is normalized before it ever reaches the LLM. The file contents themselves are never cached: `read_memories()` re-reads the disk on every chat request. The editor's "Clear saved memories" sends `clear_memories=true` **only** after an explicit click, and the update endpoint's `memory_size` form field is **required** — an omitted value must 422 rather than silently reset the budget.

`general.persona_name_mentions` (default true) controls whether the frontend prefixes assistant bubbles with the persona's name. It is frontend-only — it has no backend or LLM effect.

## Chat persistence

Every message is persisted to disk automatically — no configuration toggle needed.

- **Location**: `chatrooms/<room_name>/history.json` + audio files alongside it.
- **Format**: JSON with `datetime` (ISO-8601) and `messages` array. Each message has `id` (UUID), `sender` ("USER" or persona name), `text`, and `audio` (array of filenames).
**Audio files**: Named `<message_uuid>_<index>.<ext>` (e.g. `d4ee3044_1.wav`). Extension derived from MIME type, falls back to `.bin`. Audio that arrives *before* the message row exists (STT recordings upload before the chat request creates the user message; streaming TTS sentences upload while the reply is still streaming) is staged as `<message_uuid>_pending_<hex8>.<ext>` and automatically attached to the message's audio list when `persist_message()` runs — the staging registry lives in `app/persistence.py` (`_pending_audio`), in-memory only, so a process restart in that window leaves the (valid) file unreferenced.
- **Room switching**: `GET /api/session/load-room/<room_name>` loads persisted history and populates the in-memory session.
- **New Chat**: `POST /api/session/new` clears both in-memory history and deletes all files in the room's persistence directory.
- **Audio upload**: `POST /api/persist/audio?room=<room>` accepts base64 audio and appends it to the message's audio list.
- **Audio playback**: `GET /api/persist/audio/<room>/<filename>` serves persisted audio files.

The `SessionManager.add_user_message()` and `add_assistant_message()` methods take a `message_id` parameter and call `persistence.persist_message()` automatically.

## TTS and STT are independent

Each has its own `enabled` + `base_url` in `settings.yaml`. Use the `is_active` property (requires both enabled AND a non-empty base_url) instead of checking `enabled` alone.

**TTS server** (`app/routers/tts.py`, `app/services/tts_client.py`): The `/api/tts` and `/api/tts/health` routes live in `app/routers/tts.py`. The client functions `synthesize()` and `check_tts_health()` are in `app/services/tts_client.py`. A persona is TTS-capable only when both `reference_audio` and `reference_audio_transcript` are set (computed as the `Persona.tts_capable` property in `app/config.py`); `/api/tts` returns 404 for an unknown persona, 400 for a non-TTS-capable one, and 503 if the reference files can't be read. TTS supports a `streaming` mode (sentence-by-sentence) controlled by `settings.tts.streaming`, reported in `/api/tts/health` so the frontend can pick its queueing strategy.

**STT server** (`app/routers/stt.py`, `app/services/stt_client.py`): The `/api/stt` and `/api/stt/health` routes live in `app/routers/stt.py`. The client functions `transcribe_audio()` and `check_stt_health()` are in `app/services/stt_client.py`, forwarding the audio as multipart form data to an OpenAI-compatible `/v1/audio/transcriptions` endpoint. `/api/stt` returns 503 when STT is not `is_active`. STT requires no per-persona config.

**STT flow**: The microphone button in the input bar (or **Ctrl+Space**) uses `getUserMedia` + `MediaRecorder`. Click to start, click again to stop. The recorded blob is base64-encoded and POSTed to `/api/stt` with the actual `MediaRecorder` MIME type (browsers differ: webm, ogg, mp4, …). On success, the transcribed text is appended to the input box (never replaces), the recording is persisted to the current room via `persistence.js` with a playback button injected into the live user bubble, and `sendMessage()` is called automatically. Any non-2xx response (or a fetch failure) leaves the mic button disabled for the rest of the session.

Both TTS and STT capture audio and persist it to the current room via `persistence.js`. TTS audio is associated with the assistant message ID issued in the `start` event (stamped onto each TTS item at enqueue time — never looked up in shared state at fetch-resolution time). STT audio is associated with the user message ID generated before `sendMessage()`.

## Chat rooms

Chat rooms are stored in `chatrooms.yaml` and managed via `get_chatrooms()` / `save_chatrooms()` in `app/config.py`.

- The implicit **`"default"` room** always exists and always contains all configured personas. It cannot be created, modified, or deleted (the API rejects attempts with 400/409), and `GET /api/chatrooms/all` / `GET /api/chatrooms/default` synthesize it on the fly from the persona list.
- Room names match case-insensitively and may only contain letters, numbers, spaces, hyphens, and underscores; creating a duplicate (or the reserved name `default`) is rejected with 409.
- New rooms start with zero personas assigned; assigning a nonexistent persona returns 422.
- Switching rooms loads persisted history from disk into the session rather than clearing it.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/personas` | List all personas (summary) |
| `GET` | `/api/personas/{name}/detail` | Full persona detail |
| `POST` | `/api/personas` | Create a new persona (multipart form: text fields + optional avatar/reference audio files) |
| `PUT` | `/api/personas/{name}` | Update a persona (multipart form; rename cascades to chat rooms, directory is never renamed) |
| `DELETE` | `/api/personas/{name}` | Delete a persona and its directory (cascades to chat rooms) |
| `POST` | `/api/personas/{name}/clone` | Clone a persona with a numeric suffix (`Name_2`, `Name_3`, …) |
| `GET` | `/api/personas/{name}/avatar` | Serve a persona's avatar image file |
| `GET` | `/api/personas/{name}/reference-audio` | Serve a persona's reference audio file (`ref.wav`) |
| `GET` | `/api/chatrooms` | List all chat rooms (excluding implicit "default") |
| `GET` | `/api/chatrooms/all` | List all chat rooms including "default" (feeds the frontend dropdown) |
| `GET` | `/api/chatrooms/{name}` | Get a single chat room (including "default") |
| `POST` | `/api/chatrooms` | Create a chat room (starts with no personas) |
| `DELETE` | `/api/chatrooms/{name}` | Delete a chat room |
| `PUT` | `/api/chatrooms/{name}/personas` | Add personas to a room |
| `DELETE` | `/api/chatrooms/{name}/personas/{persona_name}` | Remove a persona from a room |
| `PUT` | `/api/chatrooms/{name}/echo-chamber` | Set/clear the room's echo chamber flag |
| `GET` | `/api/session` | Get current session state (history + active personas + current room) |
| `POST` | `/api/session/new` | Clear history and reset session (also clears persisted files) |
| `POST` | `/api/session/personas` | Update active personas for the session |
| `GET` | `/api/session/load-room/{room_name}` | Load persisted history for a room into the active session |
| `POST` | `/api/chat` | Send a message; returns an SSE stream |
| `POST` | `/api/persist/audio?room=<room>` | Upload base64 audio for a persisted message |
| `GET` | `/api/persist/audio/{room_name}/{filename}` | Serve a persisted audio file for playback |
| `GET` | `/api/tts/health` | TTS availability status |
| `POST` | `/api/tts` | Proxy text → TTS `/synthesize`; returns `{audio_base64, sample_rate}` |
| `GET` | `/api/stt/health` | STT availability status |
| `POST` | `/api/stt` | Proxy audio → STT `/v1/audio/transcriptions`; returns `{text, language}` |
| `GET` | `/api/settings` | Get current settings |
| `PUT` | `/api/settings` | Update and persist settings to `settings.yaml` |

## Persona CRUD cascades

Renaming or deleting a persona cascades to `chatrooms.yaml` via `_cascade_persona_rename()` / `_cascade_persona_delete()` in `app/routers/personas.py`. Keep this in sync if data models change. Persona create/update/delete/clone are **multipart** (`POST`/`PUT` with `Form` text fields + optional `UploadFile` avatar / reference audio); there is no JSON request model for them — the `PersonaResponse`/`PersonaDetailResponse` in `app/models.py` are the only persona request/response shapes, and they carry file *contents* and boolean capability flags rather than paths. `memory_size` is a `Form` field on both create (default 8192) and update (**required** — no server-side default, so an omitted value 422s instead of silently resetting the budget); `clear_memories` is an optional boolean on update only; cloning carries the source's `memory_size` over.

## Pydantic models

All request/response shapes and config models live in `app/models.py` and `app/config.py`. Add new fields there, not inline in routers.
