# TalkWithMe — Copilot Instructions

## Running the App

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

There is no test suite. Manual testing is done by running the app and using the browser UI at `http://localhost:8000`.

## Architecture

**Backend**: FastAPI app in `app/`. Config is loaded from `settings.yaml` (LLM, TTS, and STT endpoints), `personas.yaml` (persona definitions), and `chatrooms.yaml` (chat room groupings) at startup. All three are cached in module-level globals in `app/config.py` — always use `get_settings()` / `get_personas()` / `get_chatrooms()` rather than calling `load_*()` directly in request handlers.

**Session**: Single global `session` singleton in `app/session.py` (`SessionManager`). This is intentional — the app is single-user. History is a list of `ChatMessage` objects. `session.build_llm_messages()` constructs the per-call LLM payload, interleaving multi-persona history by prefixing other personas' turns with `[Name]: <text>` so the responding LLM understands the group chat context.

**Chat flow** (`app/routers/chat.py`):
1. `POST /api/chat` resolves `who_answers` → persona name via `_pick_persona()`
2. `_pick_persona()` supports three modes: `"router"` (ask the LLM to pick), `"random"`, or an explicit persona name
3. Responses stream as SSE with typed JSON events: `start`, `token`, `done`, `error`, `complete`

**Frontend** (`static/`): Vanilla JS single-page app split across multiple modules. Each module is a plain script (no bundler) — they communicate through shared globals defined in `state.js`.

| File | Responsibility |
|------|---------------|
| `state.js` | Shared globals (personas, session state, TTS/STT flags, audio queue, chat room state) |
| `app.js` | App bootstrap, SSE stream handling, message sending |
| `chat.js` | Chat message rendering and scroll behavior |
| `persona.js` | Persona sidebar rendering and persona editor modal (CRUD) |
| `chatrooms.js` | Chat room dropdown, room filtering, room editor modal, persona picker |
| `settings.js` | Settings modal (LLM, TTS, STT, general config) |
| `tts.js` | TTS synthesis, audio queue, Web Audio API playback |
| `stt.js` | Microphone recording, STT proxy, transcript insertion |
| `theme.js` | Dark/light theme toggle |
| `utils.js` | Shared utility helpers |

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
| `GET` | `/api/session` | Get current session state (history + active personas) |
| `POST` | `/api/session/new` | Clear history and reset session |
| `POST` | `/api/session/personas` | Update active personas for the session |
| `POST` | `/api/chat` | Send a message; returns SSE stream |
| `GET` | `/api/tts/health` | TTS availability status |
| `POST` | `/api/tts` | Proxy text → TTS `/synthesize`; returns `{audio_base64, sample_rate}` |
| `GET` | `/api/stt/health` | STT availability status |
| `POST` | `/api/stt` | Proxy audio → STT `/v1/audio/transcriptions`; returns `{text, language}` |
| `GET` | `/api/settings` | Get current settings |
| `PUT` | `/api/settings` | Update and persist settings to `settings.yaml` |

**STT flow**: The microphone button in the input bar uses `getUserMedia` + `MediaRecorder`. Click to start, click again to stop. The recorded blob is base64-encoded and POSTed to `/api/stt`, which proxies to `/v1/audio/transcriptions` at `settings.stt.base_url`. On success, the transcribed text is appended to the input box (never replaces) and `sendMessage()` is called automatically. A 5xx response disables the mic button for the session. STT is independently enabled/disabled via `settings.stt.enabled` — it has no dependency on TTS.

**TTS server** (`app/routers/tts.py`, `app/services/tts_client.py`): The `/api/tts` and `/api/tts/health` routes live in `app/routers/tts.py`. The client functions `synthesize()` and `check_tts_health()` are in `app/services/tts_client.py`. TTS is independently enabled/disabled via `settings.tts.enabled`. A persona is TTS-capable only when both `reference_audio` and `reference_audio_transcript` are set in `personas.yaml` (computed as a `@property` on `Persona` in `app/config.py`). TTS supports a `streaming` mode (sentence-by-sentence) controlled by `settings.tts.streaming`.

**STT server** (`app/routers/stt.py`, `app/services/stt_client.py`): The `/api/stt` route lives in `app/routers/stt.py`. The client function `parse_audio()` is in `app/services/stt_client.py`. STT is independently enabled/disabled via `settings.stt.enabled` and has its own `base_url` and `timeout` in `settings.yaml`. STT requires no per-persona config.

**Settings API** (`app/routers/settings.py`): `GET /api/settings` returns the full `AppSettings` object. `PUT /api/settings` validates, normalizes (blank base URLs → `None`, seed `0` → `None`), and persists to `settings.yaml`. Changes take effect immediately without a restart.

**Chat rooms** (`app/routers/chatrooms.py`, `app/config.py`): Chat rooms are stored in `chatrooms.yaml` and managed via `get_chatrooms()` / `save_chatrooms()`. The implicit `"default"` room always contains all personas and cannot be created, edited, or deleted. Renaming or deleting a persona cascades to all chat room assignments automatically (handled in `app/routers/personas.py`).

## Key Conventions

- **Pydantic everywhere**: all request/response shapes and config models are in `app/models.py` and `app/config.py`. Add new fields there.
- **SSE event schema**: every event is `data: <JSON>\n\n`. The `type` field is always present. Frontend switches on it in `handleSSEEvent()` in `app.js`.
- **Config hot-reload**: call `app.config.reload_all()` to force re-read all three YAML files (settings, personas, chatrooms). The `--reload` uvicorn flag handles Python file changes only; YAML changes require `reload_all()` or a restart.
- **Persona router** uses a low-temperature (0.1) non-streaming LLM call capped at 16 tokens to pick a persona name. If the LLM returns an unrecognized name, it falls back to random.
- **Routers** live in `app/routers/`, **external service clients** (LLM, TTS, STT) live in `app/services/`. Keep that separation.
- **`is_active` property**: both `TTSConfig` and `STTConfig` expose an `is_active` property that returns `True` only when `enabled=True` AND `base_url` is non-empty. Use this instead of checking `enabled` alone.
- **Persona CRUD cascades**: persona rename and delete both cascade to `chatrooms.yaml` via `_cascade_persona_rename()` and `_cascade_persona_delete()` in `app/routers/personas.py`. Keep this in sync if the persona or chatroom data model changes.
- **General settings**: `settings.general.persona_name_mentions` controls whether the frontend prefixes assistant messages with the persona's name. It has no backend effect.
- **No auth, no database** — by design. Don't add persistent storage or user management without revisiting the single-user assumption throughout.
