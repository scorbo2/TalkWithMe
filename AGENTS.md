# TalkWithMe — Agent Instructions

## Run the app

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Requires a locally running **llama.cpp** server with an OpenAI-compatible API. TTS and STT servers are optional.
Open `http://localhost:8000` in a browser. **There is no test suite.** All verification is manual via the browser UI.

## Config — three YAML files, cached at startup

| File | Purpose |
|------|---------|
| `settings.yaml` | LLM, TTS, STT endpoints and parameters |
| `personas.yaml` | Persona definitions (name, system prompt, TTS voice, etc.) |
| `chatrooms.yaml` | Chat room groupings (may not exist; code handles gracefully) |

All three are loaded once at startup and cached as module-level globals in `app/config.py`.
In request handlers, **always use** `get_settings()`, `get_personas()`, `get_chatrooms()` — never call `load_*()` directly.
To force a re-read of all three files, call `app.config.reload_all()`.

## Architecture

- **Backend**: FastAPI. Entry point: `app/main.py`. Routers in `app/routers/`, external service clients in `app/services/`.
- **Session**: Single global `session` singleton in `app/session.py`. Intentional — this is a single-user app. No auth, no database. Tracks `current_room` and persists messages to disk automatically.
- **Persistence**: Per-room JSON + audio files under `chatrooms/<room>/`. Handled by `app/persistence.py` (framework-agnostic) and `app/routers/persistence.py` (audio upload/serving endpoints). Created lazily on first write.
- **Frontend**: Vanilla JS SPA, no bundler. Modules in `static/` communicate via shared globals in `state.js`. See the table below:

| File | Responsibility |
|------|---------------|
| `state.js` | Shared globals (personas, session state, chat room state, TTS/STT flags, audio queue, message IDs) |
| `app.js` | Bootstrap, health checks, event listener setup, session management |
| `chat.js` | Message rendering, SSE stream handling, sending messages, persisted history rendering, audio playback buttons |
| `persistence.js` | History loading, audio upload helpers, audio URL generation |
| `persona.js` | Persona sidebar + editor modal (CRUD) |
| `chatrooms.js` | Chat room dropdown, room filtering, room editor, room switching with history load |
| `settings.js` | Settings modal (LLM/TTS/STT config) |
| `tts.js` | TTS synthesis, audio queues, Web Audio playback, audio persistence |
| `stt.js` | Microphone recording, STT proxy, transcript insertion, audio persistence |
| `theme.js` | Theme toggle |
| `utils.js` | Shared helpers |

## Chat flow

`POST /api/chat` streams SSE with typed JSON events: `start`, `token`, `done`, `error`, `complete`.
The request body includes `chat_room` (which room to persist to) and `message_id` (frontend-generated UUID for audio association).
The `done` event returns `message_id` (server-generated UUID for the assistant message).
The frontend switches on `type` in `handleSSEEvent()` in `chat.js`.

Persona selection modes: `"router"` (LLM picks, low-temp 16-token call), `"random"`, or an explicit persona name.

## Chat persistence

Every message is persisted to disk automatically — no configuration toggle needed.

- **Location**: `chatrooms/<room_name>/history.json` + audio files alongside it.
- **Format**: JSON with `datetime` (ISO-8601) and `messages` array. Each message has `id` (UUID), `sender` ("USER" or persona name), `text`, and `audio` (array of filenames).
- **Audio files**: Named `<message_uuid>_<index>.<ext>` (e.g. `d4ee3044_1.wav`). Extension derived from MIME type, falls back to `.bin`.
- **Room switching**: `GET /api/session/load-room/<room_name>` loads persisted history and populates the in-memory session.
- **New Chat**: `POST /api/session/new` clears both in-memory history and deletes all files in the room's persistence directory.
- **Audio upload**: `POST /api/persist/audio?room=<room>` accepts base64 audio and appends it to the message's audio list.
- **Audio playback**: `GET /api/persist/audio/<room>/<filename>` serves persisted audio files.

The `SessionManager.add_user_message()` and `add_assistant_message()` methods take a `message_id` parameter and call `persistence.persist_message()` automatically.

## TTS and STT are independent

Each has its own `enabled` + `base_url` in `settings.yaml`. Use the `is_active` property (requires both enabled AND a non-empty base_url) instead of checking `enabled` alone.

Both TTS and STT capture audio and persist it to the current room via `persistence.js`. TTS audio is associated with the assistant message ID from the `done` event. STT audio is associated with the user message ID generated before `sendMessage()`.

## Persona CRUD cascades

Renaming or deleting a persona cascades to `chatrooms.yaml` via `_cascade_persona_rename()` / `_cascade_persona_delete()` in `app/routers/personas.py`. Keep this in sync if data models change.

## Pydantic models

All request/response shapes and config models live in `app/models.py` and `app/config.py`. Add new fields there, not inline in routers.
