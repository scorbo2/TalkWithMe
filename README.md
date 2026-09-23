# TalkWithMe

A local single-user chat web application that connects to a locally running **llama.cpp** server and supports **multi-persona group chats** with optional **TTS playback**.

![Main chat interface](screenshots/chat_panel.png)

Follow the development of this app on my YouTube channel:

- Initial creation: https://www.youtube.com/watch?v=1VPydYNt4R8
- Multi-lingual voice cloning: https://www.youtube.com/watch?v=1yiyFYaUlU4
- Better TTS support: https://www.youtube.com/watch?v=jDudeaWppSE
- Persona-to-persona chat: https://www.youtube.com/watch?v=4J3Ao2RitKs
- Cleaning audio samples for better voice cloning: https://www.youtube.com/watch?v=s33vyuiKDfs
- MCP integrations: https://www.youtube.com/watch?v=XhD9soU3hFM
- Externalizing persona persistence: https://www.youtube.com/watch?v=Vj9rUy06Dcw
- Adding persistent memories: https://www.youtube.com/watch?v=YD6cInSuQZs
- Generifying TTS settings / cloning voices with emotion: https://www.youtube.com/watch?v=WuIiyz9ESfQ

## Features

- Chat with one or more AI personas in a simulated group chat
- Set up chat rooms and assign personas to them
- Smart persona routing: let the LLM decide, pick randomly, or choose manually
- Optional TTS: AI responses spoken aloud via a TTS server
- Optional STT: Click the microphone icon to speak your prompt
- Optional MCP tools: let any persona call tools served by MCP servers (e.g. fetch web pages, run queries)
- Fully local — no internet required, no authentication. You can connect to remote LLMs with an API key if you wish, but TalkWithMe can be run 100% locally. NOTE: only connect to remote LLMs that you trust.
- Theme chooser in the top-right: Dark (default), Light, Matrix, and Blues
- Each room persists its text and audio messages

## Prerequisites

- Python 3.10+
- A locally running llama.cpp server with OpenAI-compatible API (e.g., `--api` flag)
- (Optional) A running [tts-serve](https://github.com/scorbo2/tts-serve) instance (for TTS output).
- (Optional) An OpenAI-compatible STT server that exposes a `/v1/audio/transcriptions` endpoint
   accepting multipart form uploads. The `stt.base_url` in `settings.yaml` should point to the
   server's root (e.g., `http://localhost:8181`), and the app will POST to
   `{base_url}/v1/audio/transcriptions`. I strongly recommend [whisper-fastapi](https://github.com/heimoshuiyu/whisper-fastapi)
    as it is very easy to get up and running (and it is in fact what I use with this app).
- (Optional) One or more MCP (Model Context Protocol) servers exposing the
    [Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports/streamable-http).
    See [MCP tools](#mcp-tools-optional) for setup.

## Quick Start - upgrading

**If upgrading from an older version to 7.1 or higher, do this first**:

```bash
# Back up your settings and chatroom files:
mv -i settings.yaml settings.yaml.keep 2>/dev/null
mv -i chatrooms.yaml chatrooms.yaml.keep 2>/dev/null

# These files are no longer tracked as of 7.1:
git pull

# Restore your settings and chatroom files:
mv settings.yaml.keep settings.yaml 2>/dev/null
mv chatrooms.yaml.keep chatrooms.yaml 2>/dev/null
```

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` in your browser.

## Configuration

Most settings can be changed in the UI. Behind the scenes, configuration is stored on disk:

- `settings.yaml` stores LLM, TTS, STT, and MCP server endpoints plus general chat parameters
- `chatrooms.yaml` stores configured chat rooms (if any)
- the `Personas/` directory stores all personas — one subdirectory per persona, each holding a `prompt.md` (frontmatter + system prompt), an optional `language.txt`, `ref.wav` + `ref.txt` (TTS voice reference), and an optional `image.<ext>` avatar. This directory does not exist on a fresh clone: on first startup, the two stock example personas (Alex and Luna) are created from the tracked `personas.yaml.example` template, which the app reads but never modifies. A legacy `personas.yaml` from an older version, if still present, is migrated to this layout automatically once on first startup (then renamed to `personas.yaml.bak` and ignored).

### Server settings

The UI offers a "Settings" control in the top right, which brings up the server settings dialog:

![Server settings](screenshots/server_settings.png)

The `settings.yaml` file stores general application settings. This file does not exist
on a fresh clone - default values are used on first run, and the file is created the
first time you visit the Settings dialog and save. Here is an example of this file:

```yaml
llm:
  base_url: http://localhost:8080
  model: "default"
  max_tokens: 1024
  temperature: 0.8

tts:
  enabled: true
  base_url: http://localhost:5500
  timeout: 60
  streaming: false
  # Engine parameters: a free-form map of parameter name -> value. Which names
  # exist, their types, and their allowed ranges are defined by the TTS
  # engine's /capabilities document, not by this app. Omitted keys (or a null
  # value) are never sent, so the engine falls back to its own default.
  # Example for OmniVoice (a different engine advertises a different set):
  #   parameters:
  #     num_steps: 16          # int 4-128
  #     guidance_scale: 2.0    # number 0-10
  #     seed: 42               # int 1-1000; omit for a random seed
  parameters: {}

stt:
  enabled: true
  base_url: http://localhost:8181
  timeout: 30

general:
  persona_name_mentions: true
  max_persona_replies: 1
  max_turns_for_context: 6
  show_tool_calls: true
  personas_directory: Personas/

mcp:
  servers: []
  max_tool_iterations: 8
```

Note that TTS, STT, and MCP are all optional! You can mark them as disabled
and/or leave the base_url field blank or null. The only mandatory
configuration here is the LLM.

The `mcp` section currently has no UI — it is edited in `settings.yaml` directly and only
read on startup (restart the app after changes).

### LLM API key (remote LLMs)

TalkWithMe was built assuming a local LLM that needs no credentials. If your LLM is
remote (OpenAI, Groq, a hosted server with auth, ...), you can optionally configure
an API key. The key is deliberately **not** in `settings.yaml` — it is resolved once
at startup from two sources, in priority order:

1. the `TALKWITHME_LLM_API_KEY` environment variable (the raw key value; wins over the file)
2. an `llm_api_key` file in the project root (`llm_api_key = <your key>`)

If neither is set, no key is sent and everything works exactly as before. When
configured, every LLM request carries an `Authorization: Bearer <key>` header.

To use the file, copy the example and fill in your key:

```bash
cp llm_api_key.example llm_api_key
# edit llm_api_key and replace your_key_here
chmod 600 llm_api_key    # recommended: keep the key readable only by you
```

Notes and gotchas:

- `llm_api_key` is git-ignored; only `llm_api_key.example` is committed.
- The key is never shown in the UI, cannot be viewed or changed at runtime, and is
  never logged — the startup log reports only *whether* a key is configured, not its
  value.
- Changing the key requires a restart.
- If your LLM `base_url` uses `http://` instead of `https://`, the app logs a
  warning: your chats (and the API key) are sent in cleartext.
- Note that using a remote LLM may incur usage costs.

### Dynamic TTS parameters

The Servers dialog does not show a fixed list of TTS parameter fields. When
you open it (or change the TTS base URL), the app fetches the engine's
`GET /capabilities` document and renders exactly the parameters that engine
advertises — sliders for integer/number ranges, checkboxes for booleans,
dropdowns for enums, plain inputs for strings — and validates your values
against that document before saving. Values you leave blank are not sent, so
the engine's own defaults apply. Point the base URL at a different engine and
only that engine's parameters are shown and sent; the old engine's parameters
are never transmitted to it (they remain harmlessly in `settings.yaml` until
you delete them).

After some experimenting you may have forgotten what the parameters used to
be. The **Reset to defaults** button above the parameter list re-initializes
every dynamic field exactly as a first connection would — sliders and
dropdowns back at the engine's declared defaults, everything else blank
("let the engine decide") — without touching the Base URL, timeout, or
streaming settings. It changes nothing on the server until you click
**Save**; closing the dialog without saving discards the reset.

A legacy `settings.yaml` that still carries `num_steps`, `guidance_scale`,
and/or `seed` directly under `tts:` loads fine: those keys are folded into
`parameters` at startup and rewritten in the new shape on the next settings
save.

Refer to the [tts-serve](https://github.com/scorbo2/tts-serve) documentation
to see the full list of supported TTS servers!

### Personas

Select the "Personas" control in the top right to bring up the Personas editor:

![Personas editor](screenshots/persona_setup.png)

In this editor, you can:

- **Create** a new persona with the **+ New Persona** button
- **Edit** any existing persona's properties inline
- **Clone** a persona (a numeric suffix is added to the name, e.g. `Mark_2`)
- **Delete** a persona (with a confirmation prompt)

Changes are persisted immediately to the `Personas/` directory (configured in `settings.general.personas_directory`) and the sidebar persona list is refreshed automatically. No server restart is needed.

> **Note**: renaming or deleting a persona does not modify messages already visible in the chat panel — those retain the name they were created with. Renaming a persona also renames its on-disk directory (best-effort — see [Renaming a persona](#renaming-a-persona) below), so a directory name may occasionally differ from the persona's displayed name.

Each persona is one directory: `Personas/<Name>/`. Its settings are persisted across these files:

```
Personas/Alex/
├── prompt.md        # YAML frontmatter + system prompt body
├── language.txt     # reference-audio language code (optional)
├── ref.wav          # TTS reference audio (optional, fixed name)
├── ref.txt          # transcript of ref.wav (optional)
└── image.png        # avatar (optional, png/jpg/jpeg/gif/webp)
```

`prompt.md` holds the persona's properties as YAML frontmatter followed by the system prompt, e.g.:

```markdown
---
description: A curious and friendly AI assistant
router_hints: general questions, science, math, history
avatar_color: "#4A90D9"
allow_tool_calls: false
---
You are Alex, a curious and friendly AI.
```

A `name:` frontmatter line is written only when the persona's name differs from its directory name.

(Note that the reference-audio language does not control what language the persona speaks. It refers
specifically to the language of the supplied reference audio, if any, so that voice cloning
can be more accurate)

#### Renaming a persona

Renaming a persona also renames its directory, so `Personas/<Name>/` keeps matching the
persona's displayed name. This means the common "clone a persona, then rename the clone"
workflow doesn't leave numbered directories like `Alex_2`, `Alex_3`, ... on disk.

The directory rename is best-effort. In the following cases the persona's name is updated
but the directory keeps its old name (the new name is recorded in the `name:` frontmatter
line of `prompt.md`, so nothing is lost):

- The new name contains no characters that can be used in a directory name (only letters,
  numbers, spaces, hyphens, and underscores are allowed), so no directory name can be
  derived from it.
- The sanitized new name would collide with an existing directory. Different persona names
  can sanitize to the same directory name (for example `O'Brien` and `O*Brien` both become
  `OBrien`), and one persona's directory is never clobbered by another.
- The filesystem refuses the rename (permissions, a locked directory, ...). The save still
  succeeds; only the directory keeps its old name.

A plain edit that doesn't change the name never moves the directory, and a new name whose
sanitized form is already the directory's name has nothing to move either.

#### Persona fields

| Field | Where it lives | Description |
|-------|----------------|-------------|
| `name` | directory name / `prompt.md` | Unique persona name |
| `description` | `prompt.md` frontmatter | Short description shown in the sidebar |
| `system_prompt` | `prompt.md` body | System prompt sent to the LLM for this persona |
| `router_hints` | `prompt.md` frontmatter | Keywords the router uses to pick this persona |
| `avatar_color` | `prompt.md` frontmatter | Hex color for the avatar circle fallback |
| avatar image | `image.<ext>` file | Uploaded image for the persona (optional) |
| reference audio | `ref.wav` file | WAV for TTS voice cloning (optional) |
| reference transcript | `ref.txt` file | Transcript of the reference audio (required for TTS) |
| reference audio language | `language.txt` file | Two-letter code describing the reference audio |
| `memory_size` | `prompt.md` frontmatter | If 0, memories are disabled for this persona. Otherwise, maximum byte size of the memories file. |
| `allow_tool_calls` | `prompt.md` frontmatter | If `true`, this persona may call MCP tools while replying (if at least one MCP server is configured) |

**TTS support**: Both `ref.wav` and a non-blank `ref.txt` must be present for a persona to have TTS capability.

#### Who answers next?

The "Who should answer?" chooser in the UI offers the following options:
- **LLM decides** - based on your prompt, and the personas currently in the room, the LLM will decide who is best suited to answer.
- **Surprise me** - each prompt causes a randomly-selected persona in the current room to answer.
- **Selected persona** - the highlighted persona in the persona list will answer next.

Note that if `persona_name_mentions` is `true` in `settings.yaml`, mentioning a specific persona in your prompt will override
the above settings and force that persona to answer you. For example, prompting "What do you think, Alex?" will automatically
switch "Who should answer?" to "Selected persona", and make Alex the selected persona, before proceeding with the chat flow.
If you don't like this feature, you can set `persona_name_mentions` to `false` and restart the application. (There is currently
no UI control over this setting - it has to be hand-edited in `settings.yaml` and is only read once on startup).

### Chat rooms

Selecting the "Chat rooms" control in the top right brings up the chat room editor:

![Chat room setup](screenshots/chatroom_setup.png)

Here, you can:

- **Create** a new chat room (names must be unique)
- **Delete** a chat room (and its chat history)

The `chatrooms.yaml` file does not exist on first run. It defaults to an
empty list (i.e. only the "default" chat room will be available), and is created
automatically the first time you create a chat room. Here is an example of this file:

```yaml
chat_rooms:
- name: TNG
  persona_names:
  - Worf
  - Troi
  - Data
  - Picard
  echo_chamber: false
- name: Language_learning
  persona_names:
  - English expert
  - German expert
  - Spanish expert
- name: chit-chat
  persona_names:
  - kstew
  echo_chamber: true
# The implicit "default" room (all personas) has no entry in chat_rooms;
# its echo chamber flag lives in this top-level key instead.
default_echo_chamber: false
```

Personas can be added/removed to a chat room via the main chat interface's left panel:

![Left panel](screenshots/left_panel.png)

The "Chat room" control at the top allows you to switch chat rooms. The messages in the current
chat room are persisted, so you can come back later without losing anything.

Select "Add persona" to add personas to the current room.

Click the red "x" control next to a persona in the list to unassign them from this room.
This does not delete the persona - they are still available to be assigned to other rooms.
A persona can be assigned to any number of rooms simultaneously.

## API Endpoints and project structure

Moved to [AGENTS.md](AGENTS.md)

## Cloning non-English voices

If your reference audio is in English, you're all set.

If your reference audio is in some other language, you must specify the language code in the `reference_audio_language` field for the persona in question. This helps the voice cloner understand the reference audio. This may also prevent the cloned voice from speaking in languages other than the reference audio language, but your mileage may vary.

## Streaming TTS responses

If `streaming` is enabled in the TTS configuration, text responses from AI personas will be chunked into sentences using common punctuation, and each sentence will be queued up as a separate TTS request. A separate audio playback queue is used to queue up and play the responses sequentially. 

- Advantage: the initial lag time before playback begins is reduced. The user only has to wait for the first sentence to generate and not the entire text response. As each sentence plays, the next sentence is being processed by the TTS service. Ideally, the lag between sentences is minimal.
- Disadvantage: sentence length variance can lead to large pauses between sentences. A short sentence followed by a long sentence is the worst case scenario, because the short sentence will process and play very quickly, but the longer sentence will take much longer for the TTS server to process.

If you prefer to hear the persona's response in one clear, contiguous audio playback, and you don't mind the lag time for audio playback to begin, leave streaming mode disabled in configuration (this is the default).

If you want to hear each sentence as soon as it has been synthesized, without having to wait for the ENTIRE response to be synthesized, and you don't mind the occasional pause in between sentences, then enable streaming mode in configuration.

For lowest lag time, consider OmniVoice as the TTS server. It is considerably faster than `dots.tts` or `Qwen3-TTS`.

## Persona-to-persona chat

By default, only one AI persona in the current chat room will answer your prompt. You can make it feel more like a group chat by turning up the `max_persona_replies` option in `settings.yaml` (or by visiting the settings dialog). You can choose any number between 1 and 12. The given number of AI personas will answer your prompt (or reply to the persona who responded before them). Your personas may argue amongst themselves, depending on their respective system prompts!

## MCP tools (optional)

If you want your personas to be able to *do* things — fetch a web page, query a database, check the weather — you can connect one or more [MCP (Model Context Protocol)](https://modelcontextprotocol.io) servers. When a persona with tools enabled replies, TalkWithMe runs an agentic loop: the LLM may request tool calls, TalkWithMe executes them against the configured MCP servers, feeds the results back to the LLM, and repeats until the LLM produces a final text answer.

Be careful connecting MCP servers, especially if you are connecting to a remote LLM. You are giving the LLM the ability to execute arbitrary tools, which might be a privacy or security concern.

### Compatibility note

Some models — especially very small ones — cannot reliably follow the
tool-calling protocol; `Llama-3.2-1B-Instruct` is a confirmed example.
If you run one of these models, keep **"Allow tool calls"** off on your
personas. (Side effect: the persona can no longer save *new* memories —
ones it saved earlier are still used normally.)

If "Allow tool calls" is on and you see any of the following, it is
almost certainly the model, not an app bug:

- A persona "replying" in raw JSON that mentions `add_memory` — often
  repeated verbatim by the personas that answer after it.
- Tool-call chips repeating the same memory over and over while the
  model never answers your actual question.
- Empty replies.
- In the server log: `LLM server error mid-stream: ... peg-native
  format ...` (or, on versions before the #128 fix, the cryptic
  `Malformed SSE chunk from LLM: 'choices'`).

The app now logs the server's own error message and re-sends an aborted
request once — but it cannot teach a model the protocol. If your model
shows these symptoms, turn tool calls off (or switch to a larger model!)

## Persona memories

Every time you select "New Chat" in a given chat room, the chat history of that room is wiped. But, your personas have access to a new feature (added in V6) to allow them to persist certain memories across chat sessions, and across chat rooms. To enable this for a persona, the following conditions must be met:

- `settings.general.enable_persona_memories` must be enabled.
- `memory_size` must be greater than 0 for the persona in question.
- `allow_tool_calls` must be enabled for the persona in question (so they can save new memories - this is not needed for recalling existing memories).

If the above conditions are met, the persona may save memories related to things that you've told it. If `settings.general.show_tool_calls` is enabled, you will see the `add_memory` tool being used. Hovering over the tool chip with the mouse cursor will show you the exact memory that was saved. Memories are persisted to `memories.txt` inside the persona's directory. You can view and even edit this file directly if you wish.

You can clear persisted memories in the Persona Editor by selecting "Clear". This deletes the `memories.txt` file for the persona in question, once the dialog is confirmed.

Each persona has a `memory_size` property, which is a limit (in bytes) to the size of the `memories.txt` file. If a new memory is saved when the file is already at or over the limit, older memories will be purged automatically to make room for the new memory. Setting `memory_size` to 0 effectively disables memory storage for that persona. The maximum value for `memory_size` is 16384.

Because the limit is also checked every time a persona's memories are loaded into the LLM's context, if you (or any other process) edit `memories.txt` directly while the app is running, the change is picked up on the persona's next reply. If the file is over the limit at that point, the oldest memories are purged first — both before they are shown to the LLM and on disk — so an over-limit file is never handed to the LLM verbatim.

If a persona never saves memories even though the conditions above are met, see the [Logging](#logging) section: a short debug-logging run will show whether the `add_memory` tool is being offered to the LLM at all. The most likely cause is your choice of LLMs: in testing, it was noted that Gemma 4 will often ignore the tool unless you specifically prompt it. For example, "My favorite color is blue" does not cause Gemma 4 to call the `add_memory` tool, but specifically saying "Use the `add_memory` tool to store a memory that my favorite color is blue" will work. This is highly annoying. Qwen models in general seem better about this, but your mileage may vary. You can adjust the system prompt of your persona to be more forceful about this. For example, consider adding this to your system prompt: "Use the `add_memory` tool to store a memory whenever the user tells you about something that they like or dislike."


### 1. Configure your MCP server(s)

Edit the `mcp` section of `settings.yaml` directly (there is no UI for this yet):

```yaml
mcp:
  servers:
    - name: web
      url: http://localhost:9000/mcp    # the server's Streamable HTTP transport endpoint
      timeout: 10                       # per-request timeout in seconds (default 10)
  max_tool_iterations: 8                # max tool-call rounds per reply, 1-50 (default 8)
```

Restart the app after changes. Tools are discovered at startup, and the log will show a line like `MCP tools available: 5`. If a server is down or unreachable at startup, a warning is logged and its tools are simply unavailable — the app keeps working fine without them.

### 2. Enable tools for a persona

Open the persona editor and tick **"Allow tool calls"** for any persona that should get tool access. Personas without the flag never see the tools, no matter how many servers you have configured.

### 3. (Optional) Hide the tool chips

By default, every tool a persona calls shows up in the chat as a small chip (e.g. `🔧 get_time`); hover over a chip to see the arguments and the result. If you'd rather not see them, untick **"Show tool calls"** in the general settings dialog. The tools still work — only the chips are hidden.

### Notes and gotchas

- **Your LLM must support tool calling.** The loop speaks OpenAI-style `tools`/`tool_calls`, so the underlying model needs to be capable of it (works with recent Gemma and Qwen models served via llama.cpp's `--api`).
- **Tool names are global across servers.** If two servers expose a tool with the same name, the first server listed wins and the duplicate is ignored (a warning is logged).
- **Only the final answer is persisted.** Chat history stores the persona's text reply; tool calls and results are not saved. Tool chips are a live, in-view decoration only — they disappear on page reload or room switch.
- **Errors become feedback.** If an MCP server fails or reports an error, the LLM receives a plain-text `Error: ...` result and can retry or explain the failure — the reply will never silently vanish because of a broken tool.
- **Connections are stateless.** Every tool call opens a fresh MCP session (`initialize` handshake) and closes it afterwards. If your MCP server keeps long-lived session state, TalkWithMe does not preserve it between calls.

## Global system prompt

Sometimes, you want to specify instructions that apply to **all** personas, not just one or two
selectively. You could do this by copy+pasting the instructions to each persona's system prompt,
but this makes it difficult to change those instructions over time (you have to modify EVERY
persona's system prompt). A better way is to use the global system prompt option, in the
general Settings dialog:

![General settings](screenshots/general_settings.jpg)

Any text added here is automatically appended to the end of each persona's system prompt.
Blank out the text field to disable this feature.

Adding or modifying text here takes effect immediately on save - no restart is needed.

Remember that the "echo chamber" feature bypasses the LLM entirely, so the
global prompt has no effect there.

## Chat persistence

Each chat room persists its chat history to a dedicated subdirectory in the top-level `chatrooms` directory.
For example, a chat room named `chit-chat` will persist to `<projectDir>/chatrooms/chit-chat`. All text and
audio are saved there. If the history gets too long, you may overflow the context limit of the LLM. You can
select "New Chat" at any time to clear the chat history and start over. 

Each chat room persists separately! Selecting "New Chat" in the `chit-chat-1` room will not clear the
history in the `chit-chat-2` room, and vice versa.

## Replaying audio

A small "replay" icon will appear underneath messages that have audio associated with them. This applies both
to persona-generated messages that were sent to the TTS server, and also user-supplied microphone input.
Clicking this "replay" button will replay the audio for that message. 

In non-streaming mode, a single "replay" button will be shown underneath each persona message:

![Chat replay non-streaming](screenshots/chat_audio_replay.png)

In streaming mode, there will be one replay icon per sentence in the response. Clicking each button
will play the respective sentence:

![Chat replay streaming](screenshots/chat_audio_replay_streaming.png)

## Echo chamber

Enabling the "echo chamber" option in a chat room will cause the responding persona to simply echo back
whatever you type or speak, verbatim. This is useful with TTS servers, if you want to hear a persona
speak a specific line of dialogue. This option is disabled by default.

The number of echoing personas follows the `max_persona_replies` setting (see above): the normally
selected persona echoes first, and additional personas are picked at random from the room (without
repeats) until that limit is reached or the room runs out of personas. Set it high enough and you can
hear **every** persona in the room speak the same line at once — handy for comparing TTS voices.

The checkbox works in every chat room, including the implicit "default" room. Because that room is not
stored in `chatrooms.yaml`, its flag is persisted in the top-level `default_echo_chamber` key of the
file (see the example above) rather than on a room entry.

## Logging

TalkWithMe logs to the console — the terminal where uvicorn is running. By default it runs at
**INFO** level, which shows startup information (personas loaded, MCP tools discovered), warnings,
and errors. Most people will never need to change this.

If you are troubleshooting something — for example, a persona that never calls a tool even though
tool calls and persona memories are both enabled — you can turn on **DEBUG** logging for the run
with the `TALKWITHME_LOG_LEVEL` environment variable:

```bash
# Level names are case-insensitive: debug, info, warning, error, critical
TALKWITHME_LOG_LEVEL=debug uvicorn app.main:app --host 0.0.0.0 --port 8000
```

At DEBUG level, each reply logs the tool decision trail: the persona's `allow_tool_calls`,
`memory_size`, and `enable_persona_memories` values as the app sees them, the exact tool list
offered to the LLM on every round, and the result of each built-in tool invocation (such as
`add_memory`). The lines are prefixed `Persona memory:`, so you can filter them out of the rest
of the console with `grep "Persona memory"`. That trail tells you quickly whether the tool was
never offered to the LLM at all, or was offered but the model chose not to call it (which is
often a model/prompt issue rather than an app issue).

### Notes and gotchas

- `uvicorn --log-level debug` does **not** enable the app's debug logging. That flag only changes
  uvicorn's own loggers; the app's lines would stay at INFO. `TALKWITHME_LOG_LEVEL` is the
  supported way to change the app's level.
- Invalid values (e.g. `TALKWITHME_LOG_LEVEL=verbose`) log a warning at startup and fall back to
  INFO — a typo never prevents the app from starting.
- The variable is unset or blank means INFO.

## Detailed setup guide

I have tested this application against `llama-server` running on a local server.
Security and authentication were **not** considered, as the intent is for everything
to run on a secure local network. Other LLM providers such as LMStudio should also
work, if they provide an OpenAI-compatible API.

Because both TTS and STT are optional, you have several options for running the
application, depending on how much VRAM you can throw at it.

Refer to the [tts-serve docs](https://github.com/scorbo2/tts-serve) for more
details about setting up a server-side TTS script — each engine has its own
standalone script under `impl/` with per-engine install notes.

### Minimal setup (~4GB VRAM)

- Recommended LLM: Gemma 4 E4B Q4
- Recommended TTS: (disabled)
- Recommended STT: `whisper-fastapi`, any model, running on CPU (not on cuda!)

### Modest setup (~12GB VRAM)

- Recommended LLM: Gemma 4 E4B Q4
- Recommended TTS: `OmniVoice`
- Recommended STT: `whisper-fastapi`, small model, running on CPU or cuda

### Large setup (~16GB VRAM)

- Recommended LLM: Gemma 4 E4B Q6
- Recommended TTS: Any of `OmniVoice`, `Qwen3-TTS`, or `dots.tts`
- Recommended STT: `whisper-fastapi`, large-v3-turbo, running on cuda

### X-Large setup (24GB or higher)

- Recommended LLM: Gemma 4 26B A4B
- Recommended TTS: Any of `OmniVoice`, `Qwen3-TTS`, or `dots.tts`
- Recommended STT: `whisper-fastapi`, large-v3-turbo, running on cuda

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
- **2026-08-02** v3.0
  - In-app persona editor: create, edit, clone, and delete personas from the browser UI (#11)
  - Migrate STT to OpenAI-compatible `/v1/audio/transcriptions` endpoint (#21)
  - Split TTS and STT into separate features with separate configuration (#19)
  - Add UI for server connection settings (#23)
  - Clicking a persona now updates "Who should answer?" to "Selected persona" (#24)
  - Added configurable chat rooms for grouping personas (#18)
  - Mentioning a persona causes them to answer next (can be disabled in settings.yaml) (#28)
  - Break up the `app.js` monolith for code maintainability (#29)
  - Chat persistence (#4)
  - Save generated audio and allow replay (#5)
  - Add screenshots and better setup guidance to README (#17)
  - Add read-only "server type" field in TTS server settings (Qwen3-TTS or dots.tts) (#36)
- **2026-08-18** v4.0
  - Relax chat room name restrictions to allow spaces (#45)
  - Rename `language` to `reference_audio_language` in persona config (#46)
  - Avoid Jinja2 version 3.1.5 as a mitigation for #50
  - Add "echo chamber" option to chat rooms (#51)
  - Add persona-to-persona chat with new option `max_persona_replies` (#43)
  - Fix scroll problem in Personas dialog (#58)
  - Fix audio misattribution bug (#62)
  - Expose `max_turns_for_context` in config, and wire it up properly (#61)
  - Force UTF-8 for history file writing, and make it atomic (#49)
  - Fix chatroom sorting in UI (#66)
- **2026-08-26** v5.0
  - Add MCP support with agentic tool calling (#47)
  - Bug fix: validation errors now properly displayed (#72)
  - Bug fix: broken INFO logging (#74)
  - Code cleanup: add comprehensive pytest suite (#79)
- **2026-09-03** v6.0
  - Minor bug fix: persona ordering was inconsistent in UI (#82)
  - Major changes to Persona persistence (#87)
  - Fix longstanding display issues in Persona/Chat Room modals (#94)
  - Bump dependency versions to something less ancient (#81)
- **2026-09-10** v7.0
  - Dynamic UI for TTS server configuration via `tts-serve` (#86)
  - Allow deletion of individual messages in a chat (#99)
  - Add API key option for LLM connections (#100)
  - Add global system prompt option (#101)
  - Bug fix: cloning a persona should rename its directory (#102)
  - Bug fix: two chatroom deletion issues (#105)
- **2026-09-15** v7.1
  - Minor: add favicon (#111)
  - Minor: remove prepackaged `settings.yaml` and `chatrooms.yaml` (#113)
  - Minor: `personas.yaml` -> `personas.yaml.example` and untrack `personas.yaml` (#119)
  - Add "reset to defaults" button on TTS server settings (#121)
- **Work in progress - release date goes here when ready** v7.2
  - Enable "echo chamber" option in default chat room (#125)
  - Bug fix: persona rename/delete no longer resets "echo chamber" across chatrooms (#125)
  - Increase `max_persona_replies` limit from 4 to 12 (#130)
  - Allow "echo chamber" to respect `max_persona_replies` (#131)
  - Fix handling of in-band LLM failures (#128)

## License

This project is licensed under the [MIT License](LICENSE)

