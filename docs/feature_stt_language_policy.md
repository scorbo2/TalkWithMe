# STT language policy

This document describes an addition to the TalkWithMe app.

The goal is to improve speech-to-text behavior for language-learning chat rooms,
where short learner utterances can be misidentified as the wrong language by
automatic language detection.

## Current state

TalkWithMe sends recorded microphone audio to an OpenAI-compatible STT server.

The STT backend is currently allowed to auto-detect the language of every
utterance. This works well for many cases, but short language-learning
utterances can be ambiguous.

For example, a short Italian phrase may be detected as Portuguese, Spanish,
Latin, or another language with low confidence, causing the transcription
itself to be decoded in the wrong language.

The user may also intentionally switch to English while practicing another
language, so simply forcing one language for every recording is not sufficient.

## Desired state

Add a configurable STT language policy.

The policy supports three modes:

- `auto`
- `fixed`
- `primary_fallback`

### Auto

No language is sent to the STT backend.

The backend performs its normal automatic language detection.

This is the default and preserves the previous TalkWithMe behavior.

### Fixed

Always send the configured primary language to the STT backend.

For example:

```yaml
mode: fixed
primary_language: it
```

causes every recording to be transcribed as Italian.

### Primary + fallback

This mode is intended primarily for language-learning rooms.

Example:

```yaml
mode: primary_fallback
primary_language: it
fallback_language: en
fallback_threshold: 0.80
```

The first STT pass uses automatic language detection.

If the fallback language is detected with confidence greater than or equal to
the configured threshold, the recording is transcribed again using the fallback
language.

Otherwise, the recording is transcribed again using the primary language.

Conceptually:

```text
Clearly English?
    yes -> transcribe as English
    no  -> transcribe as Italian
```

This intentionally ignores weak detections of unrelated languages.

For example:

```text
en 0.96 -> en
en 0.82 -> en
en 0.79 -> it
it 0.91 -> it
it 0.45 -> it
pt 0.25 -> it
la 0.24 -> it
```

The point is not to ask Whisper to choose among every supported language.

In an Italian-learning room, the more useful question is:

```text
Is this clearly the fallback language?
If not, assume the primary language.
```

## Global configuration

The Servers settings dialog defines the global/default STT language policy.

The global policy is part of the existing STT configuration.

Existing installations with no language-policy configuration continue to use
`auto` mode.

## Chat room override

Each non-default chat room may optionally define its own STT language policy.

If no room override is configured, the global STT policy is used.

The implicit `default` room always uses the global policy.

Room overrides replace the global policy wholesale rather than merging
individual fields.

For example, a global setting might use:

```yaml
mode: auto
```

while an Italian-learning room overrides it with:

```yaml
mode: primary_fallback
primary_language: it
fallback_language: en
fallback_threshold: 0.80
```

## UI changes

The global STT policy is configured in the STT section of the Servers dialog.

A per-room control is displayed in the sidebar for non-default chat rooms.

The room control supports:

- Use global default
- Auto-detect
- Fixed language
- Primary + fallback

Selecting "Use global default" clears the room override and causes the room to
inherit the global STT policy.

Selecting any of the other modes creates an explicit room-level override.

When STT is unavailable or disabled, the room controls remain visible but are
disabled.

Disabling the controls does not clear or modify the saved room policy.

If STT becomes available again, the controls are re-enabled with their previous
values intact.

## STT request flow

TalkWithMe is responsible for interpreting the language policy.

The STT backend remains generic and receives only the normal optional
`language` field supported by its transcription endpoint.

### Auto

One STT request is made with no language field.

Conceptually:

```text
audio
  -> STT backend
  -> automatic language detection
  -> transcription
```

### Fixed

One STT request is made with the configured primary language.

For example:

```text
language=it
```

The STT backend skips automatic language selection and transcribes the recording
as Italian.

### Primary + fallback

Two STT requests are made using the same recorded audio.

The first pass performs automatic language detection:

```text
audio
  -> STT backend
  -> language + language_probability
```

TalkWithMe then applies the policy.

For example:

```python
if (
    detected_language == fallback_language
    and language_probability >= fallback_threshold
):
    selected_language = fallback_language
else:
    selected_language = primary_language
```

The same audio is then sent to the STT backend again using the selected language:

```text
audio
  -> STT backend
  -> language=it or language=en
  -> final transcription
```

The second transcription becomes the user message.

The second pass is important because simply relabeling the first result would
not fix text that was already decoded under the wrong language.

For example, if an Italian sentence was initially decoded as Portuguese, the
audio must be transcribed again while explicitly constrained to Italian.

## Missing or uncertain language detection

`primary_fallback` is intentionally biased toward the primary language.

If the first pass reports the fallback language with sufficient confidence, the
fallback is used.

All other cases use the primary language, including:

- low-confidence fallback detection
- detection of an unrelated language
- weak detection of the primary language
- missing `language_probability`

For an Italian-learning room with English fallback:

```text
en 0.96 -> en
en 0.82 -> en
en 0.79 -> it
pt 0.25 -> it
la 0.24 -> it
it 0.45 -> it
```

This is deliberate behavior, not an attempt to improve Whisper's general
language classifier.

## Validation

The language-policy configuration is validated before it is used.

`auto` requires no language fields.

`fixed` requires:

```text
primary_language
```

`primary_fallback` requires:

```text
primary_language
fallback_language
```

The primary and fallback languages must be different.

The fallback threshold must be between `0.0` and `1.0`.

Invalid policies are rejected instead of silently degrading to automatic
language detection.

## Persistence

The global language policy is persisted as part of the STT configuration in
`settings.yaml`.

A room-specific override is persisted in `chatrooms.yaml`.

The room policy is optional.

If it is absent, the room inherits the global STT policy.

Older configuration files that do not contain any STT language-policy fields
remain valid and preserve the application's previous behavior.

## API changes

The microphone STT request includes the current chat room so the backend can
resolve the effective language policy before transcription.

Chat rooms expose API operations to:

- set a room-specific STT language policy
- clear the room-specific policy and return to global inheritance

The STT client itself accepts an optional language parameter.

If no language is supplied, the `language` field is omitted from the request to
the STT backend.

## Backward compatibility

The feature is additive.

If no language policy is configured anywhere:

```text
no room override
    +
global mode = auto
    ↓
no language field sent
    ↓
STT backend auto-detects
```

This matches TalkWithMe's behavior before the feature was added.

Existing `settings.yaml` and `chatrooms.yaml` files continue to load without
modification.

Existing room operations also preserve any configured STT language-policy
override when personas are added or removed or when other room properties are
changed.

## Design decision

The language-selection policy belongs in TalkWithMe rather than in the STT
backend.

TalkWithMe knows the conversational context:

```text
this is an Italian-learning room
English is an allowed fallback
use English only when clearly detected
```

The STT backend does not need to know about chat rooms, learning modes, or
fallback policy.

It remains a generic transcription service that accepts audio plus an optional
language code.

This keeps the STT service reusable and keeps language-learning behavior in the
application layer where the relevant context exists.