# Persona auto-discovery

This document describes an addition to the TalkWithMe app.
The goal is to deprecate the old `personas.yaml` configuration file for Persona configuration
in favor of an structured "Personas" directory that contains enough information to allow
the application to build a list of configured Personas.

## Current state

All personas are stored in a single `personas.yaml` file in the project directory.
The example file packaged with the application looks like this:

```
personas:
- name: Alex
  description: A friendly AI assistant
  system_prompt: You are Alex, a curious and friendly AI. Keep responses concise and
    helpful.
  router_hints: general questions, science, math, history
  avatar_color: '#4A90D9'
  avatar_image: null
  reference_audio: null
  reference_audio_transcript: null
  reference_audio_language: en
  allow_tool_calls: false
- name: Luna
  description: A philosophical poet
  system_prompt: You are Luna, a thoughtful and poetic AI. You speak in a contemplative,
    lyrical tone.
  router_hints: philosophy, emotions, feelings, art
  avatar_color: '#9B59B6'
  avatar_image: null
  reference_audio: null
  reference_audio_transcript: null
  reference_audio_language: en
  allow_tool_calls: false
```

Each configured Persona defines the following core fields:

- `name`
- `description`
- `system_prompt`
- `router_hints`
- `avatar_color`
- `allow_tool_calls`

Additionally, the following optional fields can be supplied:

- `avatar_image`: full path to any image file
- `reference_audio`: full path to any reference audio file
- `reference_audio_transcript`: full path to any text file
- `reference_audio_language`: language code describing the reference audio

If the reference audio fields are missing or null, TTS is disabled for this Persona (no voice clone possible).

## Proposed state

A Personas directory (defaulting to `Personas/` within the project dir) can be configured in `settings.yaml`, in the `general` section.
If this directory exists and is readable, it is scanned on startup. The expected structure:

```
Personas/
  Alex/
    ref.wav
    ref.txt
    language.txt
    prompt.md
    image.png
  Luna/
    ref.wav
    ref.txt
    language.txt
    prompt.md
    image.png
```

Each subdirectory contains the information relevant to the persona in question. Yaml frontmatter can be
used in the `prompt.md` file to contain information about the Persona. Example `prompt.md` for Alex:

```
---
name: Alex
description: A friendly AI assistant
router_hints: general questions, science, math, history
avatar_color: '#4A90D9'
allow_tool_calls: false
---

You are Alex, a curious and friendly AI. Keep responses concise and helpful.
```

Note that the Yaml frontmatter may declare a `name` field - if this name differs from the containing
directory name, the Yaml frontmatter name is used instead. This allows persona names to contain
characters that are unacceptable in directory names (for example: "O'Brien").

Additional fields added in future versions can be added to the Yaml frontmatter, with sensible defaults
for missing values. Additional files can be added as needed in future versions. For example, additional
images in different resolutions for use in different places within the application.

### Supported file formats

The examples above use `image.png` for the Persona image, but any browser-supported image format is allowed.
The file basename must be `image`. Examples: `image.jpg`, `image.png`, `image.gif`. If multiple `image.*` files
exist in the same persona directory, log a warning and use the first one alphabetically. For example,
if both `image.jpg` and `image.png` exist for Luna, a warning "Persona Luna has multiple image files;
 using image.jpg" is logged, and `image.jpg` is used.

For voice cloning purposes, the reference audio must be supplied in `.wav` format. Any existing persona that
defines a different audio format (examples: `luna.mp3`, `reference.ogg`) should generate a warning and should
NOT copy the audio file. Log warning: "Migration: ignoring unsupported audio file 'luna.mp3' for persona Luna.
 Only wav audio is supported."

The copied image and audio files should be renamed to fit the expected structure:
- example: `luna.wav` gets copied as `ref.wav`
- example: `luna_reference.txt` gets copied as `ref.txt`
- example: `luna.png` gets copied as `image.png`
- example: `alex.jpg` gets copied as `image.jpg`

The `language.txt` file must contain a two-letter language code (`en`, `de`, `fr`, etc) and should be
stripped of leading/trailing newlines and whitespace. Any resulting value from this file that is not
exactly two characters should generate a warning on load.

The `ref.txt` file should also be stripped of leading/trailing newlines and whitespace. An empty
resulting value should generate a warning on load.

## Automatic (forced) one-time migration

On startup, if `personas.yaml` is detected, and the configured Personas directory does NOT exist (or is empty),
an information log output should indicate that automatic migration is in progress.
The contents of the file are read, and the Personas directory is generated (created if needed).

On serious error (directory not writable, out of disk space, `personas.yaml` is malformed/fails to parse to a valid PersonasConfig):
- abort the migration
- do NOT delete or rename `personas.yaml`
- output log information about the failure
- delete the Personas directory so that migration can be re-tried. This is best-effort. Deleting the directory may fail; acceptable.
- abort application startup.

On minor error (reference audio format mismatch, referenced persona file(s) not found/can't be read):
- continue with the migration
- output log warnings about what happened and which persona was affected
- continue with application startup.

On completion of migration, rename `personas.yaml` to `personas.yaml.bak` so that the migration is
not triggered again.

On startup, if **neither** `personas.yaml` nor the configured `Personas` directory exist:
- log an error "No personas found!"
- log the configured personas directory full path. (Example: "Persona directory: /home/user/TalkWithMe/Personas")
- attempt to create the configured Personas directory (initially empty).
  - on success: start with an empty set of personas. User can add new ones.
  - on failure: abort startup with descriptive error.

On startup, if `settings.general.personas_directory` is missing or empty, assume the default: `<projectDir>/Personas/`.

### Handling conflicts

On startup, if **both** `personas.yaml` and the configured `Personas` directory exist, a "loud"
log warning should issue, indicating that the `personas.yaml` file is ignored in favor of the
Personas directory. Do not rename or delete `personas.yaml` - that is up to the user.

### Extra files present in Personas directory

Any unrecognized files in the configured Personas directory, or in any Persona subdirectory,
are simply ignored. No warning in the log is needed.

### Downgrading

If the user wishes to downgrade to a previous version, they can manually rename `personas.yaml.bak`
to `personas.yaml`. The older application version will simply ignore the `Personas` directory.
Any new personas added since the migration will not be available. Changes made to existing personas
since the migration will also not be visible. (We do not update `personas.yaml.bak` after the migration
completes - it is a snapshot of what `personas.yaml` looked like at the time of upgrade).

## Changes to the Persona editor

All changes/additions/deletions to Personas made through the Persona editor will ONLY update the
Personas directory. The application will no longer write changes to `personas.yaml`.

The existing `avatar_image`, `reference_audio`, and `reference_audio_transcript` fields are 
currently simple text entry fields that expect a full path from anywhere on the filesystem. These
field will change as follows:

- `avatar_image`: display the image file (if present) scaled down to fit in the modal. Show a placeholder
  image if no avatar image exists for the Persona. A "change" button is provided that allows the user
  to browse and select any image file. Upon confirmation, this file is copied to that persona's
  directory and renamed to `image.<extension>`. A "remove" option is provided to clear the avatar
  image. This deletes `image.<extension>` from the directory.
- `reference_audio`: a simple green check or red X is displayed to indicate the presence or absence
  of a reference audio file (`ref.wav` in the persona's subdirectory). If present, a "play" control
  is provided to allow the user to hear the reference audio. A "change" button is provided that
  allows the user to browse and select any wav audio file. Upon confirmation, this file is copied to
  that persona's directory and renamed to `ref.wav`. A "remove" option is provided to clear the
  reference audio. This deletes the `ref.wav` file from the directory. Removing `ref.wav` does
  NOT also automatically remove `ref.txt` - they are maintained separately.
- `reference_audio_transcript`: this field is converted to a multi-line text field, showing the
  contents of `ref.txt` (if it is present in the persona's directory). The user can directly
  edit this transcript text. Confirming the modal will strip leading/trailing whitespace and write
  this text to `ref.txt` in the persona's directory, overwriting previous contents. No "remove"
  control is needed here - if the contents are empty after being stripped, delete `ref.txt`.

The language field can remain a simple text entry field. Its contents are stripped and written
to `language.txt` in the persona's directory. Invalid/empty/missing contents in this file always
result in a default value of `en` being used (with log warning indicating such).

The editor modal needs to change its current Json request format to multipart, carrying
fields + files in a single request. This allows atomic creation/editing in a single submission.
Server-side validation should have an extension allowlist of png/jpg/jpeg/gif/webp for images
and wav only for audio. Size caps should also be implemented: 5MB for images and 20MB for audio
data.

A side effect of these changes is that the old approach of specifying an absolute path for
reference audio allowed multiple personas to point to the same file on disk. With this new
feature, that audio file must be duplicated into each persona's directory separately. Acceptable.

The change to a multi-line transcript field means that the **API contract** also changes,
not just the routing:

- `PersonaDetailResponse.reference_audio_transcript` becomes the file's contents, not a path.
  GET must therefore read the file, and return null/empty string if missing.
- `PersonaCreateRequest/PersonaUpdateRequest` now accept contents, and need a sensible
  `max_length` to avoid unbounded text input - 16KB is fine.
- The internal config `Persona` model and `tts.py` can remain path-based.

The new "play" control on reference audio requires a `GET /api/personas/{name}/reference-audio`
(FileResponse, avatar-endpoint pattern).

Every file mutation (create/update/delete/clone/upload/remove) MUST update the
in-memory `_personas_cache`, or the green-check/red-X and details view will be stale until restart.

### Creating a new persona

Personas can be created via the "create" control, or cloned from an existing persona using
the "clone" control. The directory name for the new persona should be sanitized using the
same rules as for chat room directory names (letters, numbers, underscores, hyphens, and spaces only).
If the resulting directory name does not match the persona name, then a `name` field must be
added to the frontmatter in the generated `prompt.md`, to preserve the persona's actual name.

Example: A new persona named "Miles O'Brien" is created. The directory is created as "Miles OBrien",
and the Yaml frontmatter in `prompt.md` contains:

```
---
name: Miles O'Brien
description: ...
---
```

Renaming an existing persona does NOT rename the persona directory, which already exists.
Simply update/add the `name` field in the frontmatter to reflect the new name. The user can
manually rename the directory in between application runs if desired.

Note that it's possible for the user to unintentionally create directory naming conflicts.
For example: two personas are created, named "Miles O'Brien" and "Miles O*Brien". These both
get stripped to "Miles OBrien". If such a conflict occurs, it is acceptable to append a numeric
suffix as needed to one or both directories to avoid conflicts (for example: "Miles OBrien1" and
"Miles OBrien2" for the directory names).

If the sanitized persona name resolves to nothing (example: `***`), prevent creation with a 422
("name must contain at least one letter or number" or similar message).

## Developer notes

- Add `Personas/` to `.gitignore` to avoid accidentally committing custom personas
- The existing example `personas.yaml` can be kept as-is, so that an immediate migration is triggered on fresh clone.
- Remove the `testsetup.sh` and `restore.sh` scripts, or modify them to work with directories.
- Mark `future_PersonaConfig.md` as obsolete - this feature doc supersedes it entirely.


