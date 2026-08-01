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
- **Session**: Single global `session` singleton in `app/session.py`. Intentional — this is a single-user app. No auth, no database.
- **Frontend**: Vanilla JS SPA, no bundler. Modules in `static/` communicate via shared globals in `state.js`. See the table below:

| File | Responsibility |
|------|---------------|
| `state.js` | Shared globals (personas, session state, TTS/STT flags, audio queue) |
| `app.js` | Bootstrap, SSE stream handling, message sending |
| `chat.js` | Message rendering, scroll behavior |
| `persona.js` | Persona sidebar + editor modal (CRUD) |
| `chatrooms.js` | Chat room dropdown, room filtering, room editor |
| `settings.js` | Settings modal (LLM/TTS/STT config) |
| `tts.js` | TTS synthesis, audio queue, Web Audio playback |
| `stt.js` | Microphone recording, STT proxy, transcript insertion |
| `theme.js` | Theme toggle |
| `utils.js` | Shared helpers |

## Chat flow

`POST /api/chat` streams SSE with typed JSON events: `start`, `token`, `done`, `error`, `complete`.
The frontend switches on `type` in `handleSSEEvent()` in `app.js`.

Persona selection modes: `"router"` (LLM picks, low-temp 16-token call), `"random"`, or an explicit persona name.

## TTS and STT are independent

Each has its own `enabled` + `base_url` in `settings.yaml`. Use the `is_active` property (requires both enabled AND a non-empty base_url) instead of checking `enabled` alone.

## Persona CRUD cascades

Renaming or deleting a persona cascades to `chatrooms.yaml` via `_cascade_persona_rename()` / `_cascade_persona_delete()` in `app/routers/personas.py`. Keep this in sync if data models change.

## Pydantic models

All request/response shapes and config models live in `app/models.py` and `app/config.py`. Add new fields there, not inline in routers.

## Chat history is in-memory only

History lives in the `SessionManager` singleton. "New Chat" clears it. No persistence to disk.
