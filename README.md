# TalkWithMe

A local single-user chat web application that connects to a locally running **llama.cpp** server and supports **multi-persona group chats** with optional **TTS playback**.

As seen on YouTube!: https://www.youtube.com/watch?v=1VPydYNt4R8

## Features

- Chat with one or more AI personas in a simulated group chat
- Smart persona routing: let the LLM decide, pick randomly, or choose manually
- Streaming responses rendered incrementally
- Optional TTS: AI responses spoken aloud via a local TTS server
- Optional STT: Click the microphone icon to speak your prompt
- Fully local — no internet required, no authentication
- Dark theme UI with sidebar persona management

## Prerequisites

- Python 3.10+
- A locally running llama.cpp server with OpenAI-compatible API (e.g., `--api` flag)
- (Optional) A local TTS REST server with `/synthesize`, `parse`, and `/health` endpoints.
   You can use my [server.py](https://github.com/scorbo2/ai-playground/blob/master/dots.tts/server.py)
   in front of a [dots.tts](https://github.com/studio-dots-ai/dots.tts) server (that's what I use.)

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

Configure your LLM and TTS server endpoints:

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
```

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

**TTS support**: Both `reference_audio` and `reference_audio_transcript` must be set for a persona to have TTS capability.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Chat UI |
| `GET` | `/api/personas` | List all personas |
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

## Notes

- The TTS server is optional. If unreachable, TTS/STT is silently disabled.
- Single-user design: only one active chat session exists at a time.
- "New Chat" clears all conversation history.

## Release history

- **2026-07-27** v1.0
  - initial release
  - basic text input only
  - manual configuration of personas
  - optional TTS
- **TODO** v2.0
  - release notes go here for v2

## License

This project is licensed under the [MIT License](LICENSE)

