# Chat persistence

This document describes how the TalkWithMe app should handle a new feature - chat persistence.

## Current state

The app currently maintains a single chat session in-memory. 
Selecting "New Chat" clears any existing chat history and begins a new blank chat.
Switching between chat rooms also clears chat history and begins a new chat.

## Desired state

Each chat room (including the implicit "default" room) should persist its chat history to disk.
Switching from room A to room B should clear the chat display and then load/display the persisted chat history for room B.
Switching back to room B should clear the chat display and then load/display the persisted chat history for room A.

Chat messages should be persisted as they arrive (either from the remote LLM or from user input).

### Persistence format

A new top-level directory called `chatrooms` will be used for persistence.
It is not an error if `chatrooms` does not exist on startup - it can be created as needed.

Each chat room will persist its chat messages to a dedicated subdirectory within the top-level `chatrooms` directory.
For example, a chat room called "chit-chat" would persist to `chatrooms/chit-chat/`. The implicit "default" chatroom
would persist to `chatrooms/default/`.

Within the chatroom's subdirectory, a single json file will be used to store all chat messages.

```
{
  "datetime": "<ISO-8601 timestamp of most recent message, in the system's local timezone>",
  "messages": [
    {
      "id": "<unique UUID for this message>",
      "sender": "<persona name OR the fixed string USER for user messages>",
      "text": "<escaped text content suitable for Json storage>",
      "audio": [
        "filename1",
        "filename2",
        ...
      ]
    },
    ...
  ]
}
```

Audio files (if any) associated with each message are written to the chatroom's dedicated subdirectory
using the message UUID and an appended index as the filename. For example:

```
d4ee3044-5f77-4af5-8b3c-54c07e5d45e0_1.wav
d4ee3044-5f77-4af5-8b3c-54c07e5d45e0_2.wav
```

These filenames can then be listed in the `audio` array for the message:

```
  "audio": [
    "d4ee3044-5f77-4af5-8b3c-54c07e5d45e0_1.wav",
    "d4ee3044-5f77-4af5-8b3c-54c07e5d45e0_2.wav"
  ]
```

It is not an error condition for the `audio` array to be empty. There may legitimately be no audio associated with a given message.

### New messages

Every time a new message is added to a chat, whether it came from the remote LLM or from user input, the chat persistence
file for the current chatroom is created or updated as needed. A random v4 UUID will be generated for each new message,
and used as that message's unique id.

### Audio

Some messages may have audio associated with them:

- user messages can be input from the microphone with MediaRecorder and supplied to a configured STT server for transcription.
- persona messages can be sent to a configured TTS server to synthesize speech from them.

In both cases, the generated audio should be captured and written to the chatroom's persistence subdirectory, using
the unique UUID for the associated message.

It's possible for more than one audio file to be associated with a message. This can happen with the TTS server, if
`streaming` mode is enabled - the persona's message is chunked into sentences and sent up as multiple TTS synthesis requests.
In this case, all audio files must be captured in the proper sequence and persisted. The file extension should be driven
from the mime type of the audio, with ".bin" as a fallback extension if the mime type cannot be determined.

## Clearing the chat

When the user selects "New Chat", the chat history for that room can be cleared. This is a simple matter of deleting
all files in that chat room's dedicated persistence subdirectory. The subdirectory itself need not be removed.

Switching between chat rooms does NOT remove persisted chat history. This is a change from the current in-memory behavior,
where chat history from the room that the user is leaving is instantly lost. 

## Configuration

This new feature is not optional. There is no need to add a new configuration option for it. Chat persistence simply
happens automatically in every chat room, including the implicit "default" chat room. 

