# TalkWithMe — Development Plan

## Overview

A local single-user chat webapp built with Python + FastAPI that connects to a locally running **llama.cpp** server (OpenAI-compatible API, no auth required) and supports **multi-persona group chats** with optional **TTS playback** and **STT user speech parsing** via a custom local REST server.

---

## Goals & Features

- Chat with one or more AI personas (simulated group chat)
- A "Who should answer" chooser with options "Let the router decide", "Surprise me (random)", and "Selected persona"
- AI personas defined in a config file (name, system prompt, router hints, avatar color, optional avatar image, etc)
- Persona answers rendered via streaming
- Optional TTS: each AI response is automatically spoken aloud after it finishes streaming (if supported)
- Optional STT: user can use microphone to speak instead of typing a message (transcribed via TTS/STT server)
- Fully local — no internet required, no auth
- Single active chat session (in-memory); "New Chat" clears history
- Top-right theme chooser with Dark (default), Light, Matrix, and Blues options (persisted in browser storage)

---

## Project Structure

```
TalkWithMe/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # YAML config loading and validation
│   ├── models.py            # Pydantic request/response models
│   ├── session.py           # In-memory session state (history, active persona)
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── chat.py          # POST /api/chat → SSE stream
│   │   ├── personas.py      # GET /api/personas, GET /api/personas/{name}/avatar
│   │   ├── session.py       # GET /api/session, POST /api/session/new, POST /api/session/personas
│   │   └── tts.py           # POST /api/tts → proxy to TTS/STT server
│   └── services/
│       ├── __init__.py
│       ├── llm.py           # llama.cpp streaming client (httpx)
│       └── tts_client.py    # TTS /synthesize and STT /parse client (httpx)
├── static/
│   ├── style.css            # Dark theme, bubble layout, sidebar
│   └── app.js               # Chat logic, SSE handling, TTS audio queue
├── templates/
│   └── index.html           # Main page (Jinja2 template)
├── personas.yaml            # Persona definitions (user-editable)
├── settings.yaml            # LLM/TTS/STT server config (user-editable)
├── requirements.txt
└── README.md
```

---

## Configuration Files

### `settings.yaml`

```yaml
llm:
  base_url: http://localhost:8080
  model: "default"
  max_tokens: 1024
  temperature: 0.8

tts:
  enabled: true
  base_url: http://localhost:5500
  num_steps: 10
  guidance_scale: 3.0
  seed: null
```

### `personas.yaml`

```yaml
personas:
  - name: "Alex"
    description: "A curious and friendly AI assistant"
    system_prompt: "You are Alex, a curious and friendly AI. Keep responses concise and helpful."
    router_hints: "general questions, science, math, history"
    avatar_color: "#4A90D9"
    avatar_image: null
    reference_audio: null
    reference_audio_transcript: null

  - name: "Luna"
    description: "A philosophical and poetic thinker"
    system_prompt: "You are Luna, a thoughtful and poetic AI. You speak in a contemplative, lyrical tone."
    router_hints: "philosophy, emotions, feelings, art"
    avatar_color: "#9B59B6"
    avatar_image: "/home/user/pictures/luna.png"
    reference_audio: "/home/user/speech/luna.wav"
    reference_audio_transcript: "/home/user/speech/luna.txt"
```

Avatar display priority: if `avatar_image` is set and the file exists, show the image; otherwise fall back to a colored circle using `avatar_color` with the persona's initial letter.

TTS support: reference audio and transcript are both required to enable TTS for this persona. UI should show a "mute"
or "no volume" or similar symbol with the avatar if the persona cannot speak.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve chat UI |
| GET | `/api/personas` | Return all configured personas |
| GET | `/api/personas/{name}/avatar` | Serve persona's local avatar image (404 if not configured) |
| GET | `/api/session` | Return current session (history + active personas) |
| POST | `/api/session/new` | Clear history and reset session |
| POST | `/api/session/personas` | Update the active persona list for current session |
| POST | `/api/chat` | Send user message; returns SSE stream of AI responses |
| POST | `/api/tts` | Proxy text to TTS server; returns `{audio_base64, sample_rate}` |
| POST | `/api/stt` | Proxy base64 audio to STT server; returns `{text, language}` |

---

## Chat / Streaming Flow

1. User types or transcribes a message and hits Send
2. Frontend POSTs `{message, who_answers}` to `/api/chat` - `who_answers` is either "router", "random", or a persona name.
3. If router is used to decide who answers, a small non-streaming request is made to the LLM with the user's prompt,
   the router hints from all personas, last N conversation turns (N up to and including 3), and a request to return 
   the name of the most appropriate persona, constrained using grammar to the set of all persona names. 
   If the router fails, times out, or returns an unknown persona name, fall back to random persona selection.
   If `who_answers` is "random", choose any persona to respond. 
   If `who_answers` is neither "router" nor "random", then use the value as-is for the persona name, no computation.
   If `who_answers` is neither "router" nor "random" nor any recognized persona name, fall back to "random".
4. With the selected persona:
   - Backend appends `{role: "user", content: message}` to session history
   - Build messages: `[{role: "system", content: persona.system_prompt}, ...history_formatted]`
   - History formatting: user msgs → `role: "user"`; this persona's msgs → `role: "assistant"`; other personas' msgs → `role: "assistant", content: "[OtherName]: <text>"`
   - Stream from llama.cpp `/v1/chat/completions` with `stream: true`
   - Emit SSE: `{"type":"start","persona":"Alex"}` → `{"type":"token","persona":"Alex","token":"..."}` → `{"type":"done","persona":"Alex","text":"<full>"}`
   - Append full response to session history
   - emit `{"type":"complete"}`

---

## STT Flow

1. User clicks the Microphone button next to the text entry field
2. `getUserMedia` -> `MediaRecorder` records user's speech
3. User clicks Microphone again to stop speaking
4. Input audio is Base64-encoded
5. POST `{audio_base64}` to `/api/stt` endpoint on the app (backend proxies to `/parse` on the TTS/STT server)
6. If this returns 5xx, show an error and disable the microphone button
7. Otherwise, parse `text` from the Json response and populate the text input box (append to existing contents, never replace)
8. On success, implicitly invoke the chat/streaming flow as though the user clicked Send

---

## TTS Flow

### When not in streaming mode

When the `streaming` config property is set to `false` (or when it is omitted):

1. Frontend receives `done` SSE event for a persona
2. If TTS enabled and supported for this persona: enqueue `{persona_name, text}` in FIFO audio queue
3. POST `{text}` to `/api/tts`
4. Backend calls TTS server `/synthesize` with `{text, num_steps, prompt_text, audio_base64, guidance_scale, seed}`.
   `text` is the text to be synthesized into speech.
   `prompt_text` is the reference audio transcript from the persona. If no transcript provided, disable speech for this persona.
   `audio_base64` is the Base64-encoded reference audio from the persona. If no audio provided, disable speech for this persona.
   Note: backend must perform Base64-encoding on the supplied reference audio WAV file.
5. Returns `{audio_base64, sample_rate}` to frontend
6. Frontend decodes base64 WAV → Web Audio API → plays audio
7. Queue processes next item after current audio finishes

### When streaming mode is enabled

When the `streaming` config property is set to `true`:

1. Incoming text streams from the personas are chunked into sentences using a regex to split on common punctuation.
2. At each sentence boundary, a TTS request for that sentence is queued.
3. A separate audio playback queue is used to queue up audio responses from the TTS server.
4. As sentence 1 is being played, sentence 2 can be processing on the TTS server, and so on for sentence 3.
5. This mode reduces the initial lag time for audio playback to begin, at the expense of possibly causing longer delays between sentences.

---

## Frontend Design

- **Theme chooser** in top bar with Dark (default), Light, Matrix, and Blues palettes
- **Left sidebar** (~250px):
   - "Who should answer?" chooser with options "LLM decides", "Surprise me", and "Selected persona"
   - Beneath that chooser, show all personas with colored avatar (image or initial circle)
   - Allow clicking to select a persona in a "exactly one selected at all times" way (default to first persona selected)
   - Show selection by changing the background/foreground colors of the persona to highlight the selected one
- **Main chat panel**: scrollable bubbles; user right-aligned; AI left-aligned with persona avatar + name
   - Persona response bubbles must be accompanied by avatar (image or initial circle) AND persona name.
- **Streaming text** renders incrementally inside AI bubble.
- **Bottom input bar**: text input + Microphone button + Send button; Enter sends, Shift+Enter for newline, Microphone transcribes speech
- **Top bar**: app title, "New Chat" button, TTS toggle (speaker icon)
- **Loading indicator**: subtle "..." bubble while a persona is generating

---

## Implementation Todos

1. Project scaffolding and dependencies (`setup`)
2. Config loading (`config`)
3. Pydantic models and session state (`models`)
4. llama.cpp streaming client (`llm-service`)
5. TTS client (`tts-service`)
6. Personas router (`router-personas`)
7. Session router (`router-session`)
8. Chat SSE streaming router (`router-chat`)
9. TTS proxy router (`router-tts`)
10. FastAPI main app (`app-main`)
11. HTML template (`frontend-html`)
12. CSS styles (`frontend-css`)
13. JavaScript logic (`frontend-js`)
14. README (`readme`)

---

## Dependencies

```
fastapi
uvicorn[standard]
httpx
jinja2
pyyaml
python-multipart
aiofiles
```

---

## Running the App

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`. Ensure llama.cpp is running at the URL in `settings.yaml`.
TTS is optional — if unreachable, the app logs a warning and disables TTS gracefully.
The TTS server exposes a simple GET /health endpoint - if this returns 200, assume TTS is available.
If TTS is available, UI should default the TTS toggle to on. Otherwise, default it to off.

