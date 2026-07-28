# TalkWithMe — Copilot Instructions

## Running the App

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

There is no test suite. Manual testing is done by running the app and using the browser UI at `http://localhost:8000`.

## Architecture

**Backend**: FastAPI app in `app/`. Config is loaded from `settings.yaml` (LLM/TTS endpoints) and `personas.yaml` (persona definitions) at startup. Both are cached in module-level globals in `app/config.py` — always use `get_settings()` / `get_personas()` rather than calling `load_*()` directly in request handlers.

**Session**: Single global `session` singleton in `app/session.py` (`SessionManager`). This is intentional — the app is single-user. History is a list of `ChatMessage` objects. `session.build_llm_messages()` constructs the per-call LLM payload, interleaving multi-persona history by prefixing other personas' turns with `[Name]: <text>` so the responding LLM understands the group chat context.

**Chat flow** (`app/routers/chat.py`):
1. `POST /api/chat` resolves `who_answers` → persona name via `_pick_persona()`
2. `_pick_persona()` supports three modes: `"router"` (ask the LLM to pick), `"random"`, or an explicit persona name
3. Responses stream as SSE with typed JSON events: `start`, `token`, `done`, `error`, `complete`

**Frontend** (`static/app.js`): Vanilla JS single-page app. Reads the SSE stream manually with `ReadableStream`. Maintains a FIFO `audioQueue` for TTS playback via the Web Audio API. The `who_answers` value comes from the sidebar radio buttons.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/tts/health` | TTS availability status |
| `POST` | `/api/tts` | Proxy text → TTS `/synthesize`; returns `{audio_base64, sample_rate}` |
| `POST` | `/api/stt` | Proxy audio → STT `/parse`; returns `{text, language}` |

**STT flow**: The microphone button in the input bar uses `getUserMedia` + `MediaRecorder`. Click to start, click again to stop. The recorded blob is base64-encoded and POSTed to `/api/stt`, which proxies to `/parse` on the same server as TTS (`settings.tts.base_url`). On success, the transcribed text is appended to the input box (never replaces) and `sendMessage()` is called automatically. A 5xx response disables the mic button for the session.

**TTS/STT server**: Both `/api/tts` and `/api/stt` routes share one `APIRouter` (no prefix) in `app/routers/tts.py`. The client functions `synthesize()` and `parse_audio()` are in `app/services/tts_client.py`. A persona is TTS-capable only when both `reference_audio` and `reference_audio_transcript` are set in `personas.yaml` (computed as a `@property` on `Persona` in `app/config.py`). STT requires no per-persona config — it uses the same `tts.base_url` endpoint.

## Key Conventions

- **Pydantic everywhere**: all request/response shapes and config models are in `app/models.py` and `app/config.py`. Add new fields there.
- **SSE event schema**: every event is `data: <JSON>\n\n`. The `type` field is always present. Frontend switches on it in `handleSSEEvent()`.
- **Config hot-reload**: call `app.config.reload_all()` to force re-read both YAML files (e.g., for dev tooling). The `--reload` uvicorn flag handles Python file changes only; YAML changes require `reload_all()` or a restart.
- **Persona router** uses a low-temperature (0.1) non-streaming LLM call capped at 16 tokens to pick a persona name. If the LLM returns an unrecognized name, it falls back to random.
- **Routers** live in `app/routers/`, **external service clients** (LLM, TTS) live in `app/services/`. Keep that separation.
- **No auth, no database** — by design. Don't add persistent storage or user management without revisiting the single-user assumption throughout.
