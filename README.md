# TalkWithMe

A local single-user chat web application that connects to a locally running **llama.cpp** server and supports **multi-persona group chats** with optional **TTS playback**.

As seen on YouTube!: https://www.youtube.com/watch?v=1VPydYNt4R8

## Features

- Chat with one or more AI personas in a simulated group chat
- Smart persona routing: let the LLM decide, pick randomly, or choose manually
- Streaming responses rendered incrementally
- Optional TTS: AI responses spoken aloud via a local TTS server
- Optional STT: Click the microphone icon to speak your prompt
- **In-app Persona Editor**: create, edit, clone and delete personas without touching `personas.yaml`
- Fully local — no internet required, no authentication
- Theme chooser in the top-right: Dark (default), Light, Matrix, and Blues
- Theme preference persists between visits in local browser storage

## Prerequisites

- Python 3.10+
- A locally running llama.cpp server with OpenAI-compatible API (e.g., `--api` flag)
- (Optional) A local TTS REST server with `/synthesize` and `/health` endpoints.
   You can use my [server.py](https://github.com/scorbo2/ai-playground/blob/master/dots.tts/server.py)
   in front of a [dots.tts](https://github.com/studio-dots-ai/dots.tts) server (that's what I use.)
- (Optional) An OpenAI-compatible STT server that exposes a `/v1/audio/transcriptions` endpoint
   accepting multipart form uploads. The `stt.base_url` in `settings.yaml` should point to the
   server's root (e.g., `http://localhost:8181`), and the app will POST to
   `{base_url}/v1/audio/transcriptions`.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in your browser.

## Configuration

### `settings.yaml`

Configure your LLM, TTS, and STT server endpoints:

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
  timeout: 60
  streaming: false

stt:
  enabled: true
  base_url: http://localhost:8181
  timeout: 30
```

The STT `base_url` should point to an OpenAI-compatible server. The app sends audio
as a multipart form POST to `{base_url}/v1/audio/transcriptions` with
`response_format=json`. The server must return a JSON object containing at least a
`text` field. Optional response fields `language` (defaults to `"en"`) and
`language_probability` are also supported.

Note that TTS and STT are both optional! You can mark them as disabled
and/or leave the base_url field blank or null. The only mandatory
configuration here is the LLM.

### `personas.yaml`

Define your AI personas:

```yaml
personas:
  - name: "Alex"
    description: "A curious and friendly AI assistant"
    system_prompt: "You are Alex, a curious and friendly AI."
    router_hints: "general questions, science, math, history"
    avatar_color: "#4A90D9"
    avatar_image: null
    reference_audio: null
    reference_audio_transcript: null
    language: "en"
```

#### Persona fields

| Field | Description |
|-------|-------------|
| `name` | Unique persona name |
| `description` | Short description shown in the sidebar |
| `system_prompt` | System prompt sent to the LLM for this persona |
| `router_hints` | Keywords the router uses to pick this persona |
| `avatar_color` | Hex color for the avatar circle fallback |
| `avatar_image` | Path to a local image file (optional) |
| `reference_audio` | Path to a WAV file for TTS voice cloning (optional) |
| `reference_audio_transcript` | Path to a TXT file with the audio transcript (required with `reference_audio`) |
| `language` | Two-letter language code describing the reference audio (defaults to `en`) |

**TTS support**: Both `reference_audio` and `reference_audio_transcript` must be set for a persona to have TTS capability.

## In-App Persona Editor

Click the **✎ Personas** button in the top-right to open the Persona Editor. You can:

- **Create** a new persona with the **+ New Persona** button
- **Edit** any existing persona's properties inline
- **Clone** a persona (a numeric suffix is added to the name, e.g. `Mark_2`)
- **Delete** a persona (with a confirmation prompt)

Changes are persisted immediately to `personas.yaml` and the sidebar persona list is refreshed automatically. No server restart is needed.

> **Note**: renaming or deleting a persona does not modify messages already visible in the chat panel — those retain the name they were created with.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Chat UI |
| `GET` | `/api/personas` | List all personas |
| `POST` | `/api/personas` | Create a new persona |
| `GET` | `/api/personas/{name}/detail` | Full persona detail (all editable fields) |
| `PUT` | `/api/personas/{name}` | Update a persona |
| `DELETE` | `/api/personas/{name}` | Delete a persona |
| `POST` | `/api/personas/{name}/clone` | Clone a persona |
| `GET` | `/api/personas/{name}/avatar` | Serve persona avatar image |
| `GET` | `/api/session` | Current session state |
| `POST` | `/api/session/new` | Reset session |
| `POST` | `/api/session/personas` | Update active personas |
| `POST` | `/api/chat` | Send message (SSE stream response) |
| `GET` | `/api/tts/health` | TTS availability status |
| `POST` | `/api/tts` | Synthesize speech from text |
| `POST` | `/api/stt` | Transcribe speech from voice |

## Project Structure

```
TalkWithMe/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # YAML config loading
│   ├── models.py            # Pydantic models
│   ├── session.py           # In-memory session state
│   ├── routers/             # API route handlers
│   └── services/            # LLM and TTS clients
├── static/                  # CSS and JavaScript
├── templates/               # Jinja2 HTML templates
├── personas.yaml            # Persona definitions
├── settings.yaml            # Server configuration
└── requirements.txt
```

## Cloning non-English voices

If your reference audio is in English, you're all set.

If your reference audio is in some other language, you must specify the language code in the `language` field for the persona in question. This helps the voice cloner understand the reference audio. This may also prevent the cloned voice from speaking in languages other than the reference audio language, but your mileage may vary.

## Streaming TTS responses

If `streaming` is enabled in the TTS configuration, text responses from AI personas will be chunked into sentences using common punctuation, and each sentence will be queued up as a separate TTS request. A separate audio playback queue is used to queue up and play the responses sequentially. 

- Advantage: the initial lag time before playback begins is reduced. The user only has to wait for the first sentence to generate and not the entire text response. As each sentence plays, the next sentence is being processed by the TTS service. Ideally, the lag between sentences is minimal.
- Disadvantage: sentence length variance can lead to large pauses between sentences. A short sentence followed by a long sentence is the worst case scenario, because the short sentence will process and play very quickly, but the longer sentence will take much longer for the TTS server to process.

If you prefer to hear the persona's response in one clear, contiguous audio playback, and you don't mind the lag time for audio playback to begin, leave streaming mode disabled in configuration (this is the default).

If you want to hear each sentence as soon as it has been synthesized, without having to wait for the ENTIRE response to be synthesized, and you don't mind the occasional pause in between sentences, then enable streaming mode in configuration.

## Notes

- The TTS server is optional. If unreachable, TTS is silently disabled.
- The STT server is optional. If unreachable or misconfigured, STT is gracefully disabled with an error message.
- Single-user design: only one active chat session exists at a time.
- "New Chat" clears all conversation history.

## Release history

- **2026-07-27** v1.0
  - initial release
  - basic text input only
  - manual configuration of personas
  - optional TTS
- **2026-07-29** v2.0
  - Add multi-language support (#1)
  - Add streaming TTS audio output option (#2)
  - Better size and positioning of avatar images (#3)
  - Allow microphone voice input for prompting (#6)
  - Color theme chooser with persistence (#12)
- **TODO** v3.0
  - In-app persona editor: create, edit, clone, and delete personas from the browser UI (#11)
  - Migrate STT to OpenAI-compatible `/v1/audio/transcriptions` endpoint (#21)
  - Split TTS and STT into separate features with separate configuration (#19)

## License

This project is licensed under the [MIT License](LICENSE)

