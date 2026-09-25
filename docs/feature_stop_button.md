# Stop button

This document describes a new feature for the TalkWithMe application.
The goal is to provide a single "stop" button in the top bar of the application
that, if clicked, will stop any currently-playing audio.

## Current state

This application currently has an audio playing queue in the front end, and it works well.
The only problem is, there's no way for the user to stop playback that's in progress
(either when playing audio that just arrived from the TTS server, or after clicking a
"replay" button underneath an existing chat bubble). If an audio message is very long,
the user must listen to the whole thing. Or, if multiple personas reply to the same user
prompt, all of the persona audio messages get queued up and played automatically, one by
one, with no way for the user to interrupt the process.

## Desired state

Add a "stop" button in the top bar, in between the "New Chat" button and the audio indicator.
The "stop" button should have an appropriate icon, and should be disabled when audio is not playing.
Whenever audio is playing, this button should be enabled.

### What does "stop" mean?

Clicking "stop" should immediately disable the stop button, stop playing the current audio,
and mark all subsequent queued audio messages for persist-only.

It's important that queued audio messages should still be associated with the correct chat bubble.
That is, a "replay" button should appear for each audio message underneath its corresponding chat bubble.

It's important that incoming audio from the TTS server should still be persisted to disk, even if
the user has clicked "stop".

"Stop" does not mean "cancel all audio in progress". It just means "stop playing audio until the next
user prompt, or until the user clicks a replay button under any existing chat bubble".

### Simple example

- `max_persona_replies` is 1.
- sentence-by-sentence streaming mode is disabled.
- TTS is enabled and we are connected to a valid server.
- The user issues a prompt.
- The LLM returns a text response.
- A **single** TTS request is sent (message is not chunked, as sentence-by-sentence mode is disabled).
- The TTS server returns a single audio file.
- We begin playing the audio.
- Halfway through playback, the user clicks "stop".

Expected outcome:
- the audio stops playing immediately.
- a single "replay" button for this audio message appears under the persona's chat bubble.
- the audio is successfully persisted to disk.

Next steps:
- the user can click the new "replay" button to hear the entire audio message.
- or, the user can issue another prompt to trigger another response.
- either way, audio messages should play as normal.

### Complicated example:

- `max_persona_replies` is 4.
- sentence-by-sentence streaming mode is enabled.
- TTS is enabled and we are connected to a valid server.
- The user issues a prompt.
- We receive four LLM responses (one for each persona who replies).
- Each response is chunked by sentence-ending punctuation (because sentence-by-sentence streaming mode is enabled).
- Multiple TTS requests are sent, one for each sentence.
- The TTS server begins to send audio responses.
- We begin playing the audio.
- Halfway through playback of the first persona's response, **before** TTS requests for the subsequent personas' responses have been sent, the user clicks "stop".

Expected outcome:
- the audio stops playing immediately.
- TTS requests and responses continue!
- "replay" buttons are added for each sentence, for each persona's responses.
- an audio file is persisted to disk for each sentence of each persona's response.
- effectively, we continue processing TTS requests and responses as normal, with the exception that no audio is played.

Next steps:
- same as the simple example. User can click any "replay" button or issue another prompt.
- audio messages should play as normal.

### What NOT to do

- "stop" never discards audio! It must always persist to disk.
- "stop" never prevents a replay button from being added to its chat bubble! They must always appear.
- "stop" never interrupts TTS requests or responses! They continue as normal, just without audio playback.
- "stop" does not persist across user prompts, or across manual replays!

### Summary

The "stop" button is actually a temporary "mute" function. It does not interfere with the usual flow
of events, other than disabling audio playback until the next user action.

