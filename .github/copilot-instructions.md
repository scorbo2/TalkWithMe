# TalkWithMe — Copilot Instructions

## Running the App

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

There is no test suite. Manual testing is done by running the app and using the browser UI at `http://localhost:8000`.

## Architecture

**Backend**: FastAPI app in `app/`. Config is loaded from `settings.yaml` (LLM, TTS, and STT endpoints), `personas.yaml` (persona definitions), and `chatrooms.yaml` (chat room groupings) at startup. All three are cached in module-level globals in `app/config.py` — always use `get_settings()` / `get_personas()` / `get_chatrooms()` rather than calling `load_*()` directly in request handlers.

**Session**: Single global `session` singleton in `app/session.py` (`SessionManager`). This is intentional — the app is single-user. History is a list of `ChatMessage` objects. `session.build_llm_messages()` constructs the per-call LLM payload, interleaving multi-persona history by prefixing other personas' turns with `[Name]: <text>` so the responding LLM understands the group chat context. The session tracks `current_room` and persists messages to disk automatically via `app/persistence.py`.

**Persistence**: Per-room JSON + audio files under `chatrooms/<room>/`. `app/persistence.py` is framework-agnostic and handles `persist_message()`, `persist_audio()`, `load_history()`, and `clear_room()`. `app/routers/persistence.py` provides the audio upload and playback serving endpoints. Created lazily on first write.

**Chat flow** (`app/routers/chat.py`):
1. `POST /api/chat` accepts `chat_room` (which room to persist to) and `message_id` (frontend-generated UUID for audio association)
2. Resolves `who_answers` → persona name via `_pick_persona()`
3. `_pick_persona()` supports three modes: `"router"` (ask the LLM to pick), `"random"`, or an explicit persona name
4. Responses stream as SSE with typed JSON events: `start`, `token`, `done`, `error`, `complete`
5. The `done` event returns `message_id` (server-generated UUID for the assistant message)

**Frontend** (`static/`): Vanilla JS single-page app split across multiple modules. Each module is a plain script (no bundler) — they communicate through shared globals defined in `state.js`.

| File | Responsibility |
|------|---------------|
| `state.js` | Shared globals (personas, session state, chat room state, TTS/STT flags, audio queue, message IDs) |
| `app.js` | Bootstrap, health checks, event listener setup, session management |
| `chat.js` | Message rendering, SSE stream handling, sending messages, persisted history rendering, audio playback buttons |
| `persistence.js` | History loading, audio upload helpers, audio URL generation |
| `persona.js` | Persona sidebar rendering and persona editor modal (CRUD) |
| `chatrooms.js` | Chat room dropdown, room filtering, room editor modal, persona picker, room switching with history load |
| `settings.js` | Settings modal (LLM, TTS, STT, general config) |
| `tts.js` | TTS synthesis, audio queues, Web Audio API playback, audio persistence |
| `stt.js` | Microphone recording, STT proxy, transcript insertion, audio persistence |
| `theme.js` | Dark/light theme toggle |
| `utils.js` | Shared utility helpers |

## Chat Persistence

Every message is persisted to disk automatically — no configuration toggle needed.

- **Location**: `chatrooms/<room_name>/history.json` + audio files alongside it.
- **Format**: JSON with `datetime` (ISO-8601) and `messages` array. Each message has `id` (UUID), `sender` ("USER" or persona name), `text`, and `audio` (array of filenames).
**Audio files**: Named `<message_uuid>_<index>.<ext>` (e.g. `d4ee3044_1.wav`). Extension derived from MIME type, falls back to `.bin`. Audio that arrives *before* the message row exists (STT recordings upload before the chat request creates the user message; streaming TTS sentences upload while the reply is still streaming) is staged as `<message_uuid>_pending_<hex8>.<ext>` and automatically attached to the message's audio list when `persist_message()` runs — the staging registry lives in `app/persistence.py` (`_pending_audio`), in-memory only, so a process restart in that window leaves the (valid) file unreferenced.
- **Room switching**: `GET /api/session/load-room/<room_name>` loads persisted history and populates the in-memory session.
- **New Chat**: `POST /api/session/new` clears both in-memory history and deletes all files in the room's persistence directory.
- **Audio upload**: `POST /api/persist/audio?room=<room>` accepts base64 audio and appends it to the message's audio list.
- **Audio playback**: `GET /api/persist/audio/<room>/<filename>` serves persisted audio files.

The `SessionManager.add_user_message()` and `add_assistant_message()` methods take a `message_id` parameter and call `persistence.persist_message()` automatically.

Both TTS and STT capture audio and persist it to the current room via `persistence.js`. TTS audio is associated with the assistant message ID from the `done` event. STT audio is associated with the user message ID generated before `sendMessage()`.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/personas` | List all personas (summary) |
| `GET` | `/api/personas/{name}/detail` | Full persona detail |
| `POST` | `/api/personas` | Create a new persona |
| `PUT` | `/api/personas/{name}` | Update a persona (cascades renames to chat rooms) |
| `DELETE` | `/api/personas/{name}` | Delete a persona (cascades to chat rooms) |
| `POST` | `/api/personas/{name}/clone` | Clone a persona with a numeric suffix |
| `GET` | `/api/personas/{name}/avatar` | Serve a persona's avatar image file |
| `GET` | `/api/chatrooms` | List all chat rooms (excluding implicit "default") |
| `GET` | `/api/chatrooms/all` | List all chat rooms including "default" |
| `GET` | `/api/chatrooms/{name}` | Get a single chat room |
| `POST` | `/api/chatrooms` | Create a chat room |
| `DELETE` | `/api/chatrooms/{name}` | Delete a chat room |
| `PUT` | `/api/chatrooms/{name}/personas` | Add personas to a room |
| `DELETE` | `/api/chatrooms/{name}/personas/{persona_name}` | Remove a persona from a room |
| `GET` | `/api/session` | Get current session state (history + active personas + current room) |
| `POST` | `/api/session/new` | Clear history and reset session (also clears persisted files) |
| `POST` | `/api/session/personas` | Update active personas for the session |
| `GET` | `/api/session/load-room/{room_name}` | Load persisted history for a room into the active session |
| `POST` | `/api/chat` | Send a message; returns SSE stream |
| `POST` | `/api/persist/audio?room=<room>` | Upload base64 audio for a persisted message |
| `GET` | `/api/persist/audio/{room}/{filename}` | Serve a persisted audio file for playback |
| `GET` | `/api/tts/health` | TTS availability status |
| `POST` | `/api/tts` | Proxy text → TTS `/synthesize`; returns `{audio_base64, sample_rate}` |
| `GET` | `/api/stt/health` | STT availability status |
| `POST` | `/api/stt` | Proxy audio → STT `/v1/audio/transcriptions`; returns `{text, language}` |
| `GET` | `/api/settings` | Get current settings |
| `PUT` | `/api/settings` | Update and persist settings to `settings.yaml` |

**STT flow**: The microphone button in the input bar uses `getUserMedia` + `MediaRecorder`. Click to start, click again to stop. The recorded blob is base64-encoded and POSTed to `/api/stt`, which proxies to `/v1/audio/transcriptions` at `settings.stt.base_url`. On success, the transcribed text is appended to the input box (never replaces) and `sendMessage()` is called automatically. The recorded audio is also persisted to the current room via `persistence.js`. A 5xx response disables the mic button for the session. STT is independently enabled/disabled via `settings.stt.enabled` — it has no dependency on TTS.

**TTS server** (`app/routers/tts.py`, `app/services/tts_client.py`): The `/api/tts` and `/api/tts/health` routes live in `app/routers/tts.py`. The client functions `synthesize()` and `check_tts_health()` are in `app/services/tts_client.py`. TTS is independently enabled/disabled via `settings.tts.enabled`. A persona is TTS-capable only when both `reference_audio` and `reference_audio_transcript` are set in `personas.yaml` (computed as a `@property` on `Persona` in `app/config.py`). TTS supports a `streaming` mode (sentence-by-sentence) controlled by `settings.tts.streaming`. TTS audio is automatically persisted to the current room after synthesis.

**STT server** (`app/routers/stt.py`, `app/services/stt_client.py`): The `/api/stt` route lives in `app/routers/stt.py`. The client function `parse_audio()` is in `app/services/stt_client.py`. STT is independently enabled/disabled via `settings.stt.enabled` and has its own `base_url` and `timeout` in `settings.yaml`. STT requires no per-persona config.

**Settings API** (`app/routers/settings.py`): `GET /api/settings` returns the full `AppSettings` object. `PUT /api/settings` validates, normalizes (blank base URLs → `None`, seed `0` → `None`), and persists to `settings.yaml`. Changes take effect immediately without a restart.

**Chat rooms** (`app/routers/chatrooms.py`, `app/config.py`): Chat rooms are stored in `chatrooms.yaml` and managed via `get_chatrooms()` / `save_chatrooms()`. The implicit `"default"` room always contains all personas and cannot be created, edited, or deleted. Renaming or deleting a persona cascades to all chat room assignments automatically (handled in `app/routers/personas.py`). Switching rooms loads persisted history from disk rather than clearing the session.

## Key Conventions

- **Pydantic everywhere**: all request/response shapes and config models are in `app/models.py` and `app/config.py`. Add new fields there.
- **SSE event schema**: every event is `data: <JSON>\n\n`. The `type` field is always present. Frontend switches on it in `handleSSEEvent()` in `chat.js`.
- **Chat request body**: `POST /api/chat` includes `chat_room` (which room to persist to) and `message_id` (frontend-generated UUID for audio association). The `done` event returns `message_id` (server-generated UUID for the assistant message).
- **Config hot-reload**: call `app.config.reload_all()` to force re-read all three YAML files (settings, personas, chatrooms). The `--reload` uvicorn flag handles Python file changes only; YAML changes require `reload_all()` or a restart.
- **Persona router** uses a low-temperature (0.1) non-streaming LLM call capped at 16 tokens to pick a persona name. If the LLM returns an unrecognized name, it falls back to random.
- **Routers** live in `app/routers/`, **external service clients** (LLM, TTS, STT) live in `app/services/`. Keep that separation.
- **`is_active` property**: both `TTSConfig` and `STTConfig` expose an `is_active` property that returns `True` only when `enabled=True` AND `base_url` is non-empty. Use this instead of checking `enabled` alone.
- **Persona CRUD cascades**: persona rename and delete both cascade to `chatrooms.yaml` via `_cascade_persona_rename()` and `_cascade_persona_delete()` in `app/routers/personas.py`. Keep this in sync if the persona or chatroom data model changes.
- **General settings**: `settings.general.persona_name_mentions` controls whether the frontend prefixes assistant messages with the persona's name. It has no backend effect.
- **No auth, no database** — by design. The `chatrooms/` directory is the only persistent storage. Don't add user management without revisiting the single-user assumption throughout.
