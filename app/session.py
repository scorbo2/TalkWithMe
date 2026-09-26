"""In-memory session state with disk persistence.

Single-user app means one global session object.
History is a simple list of dicts: {"role": "user"|"assistant", "content": str, "persona": str|None}.

Messages are automatically persisted to disk per chat room as they arrive.
"""

from typing import Dict, List, Optional

from app.models import ChatMessage
from app import persistence

# Appended to the system prompt when the history slice contains another
# persona's message (the one rendered as "[Name]: <text>"). Small models
# (observed: Llama-3.2-1B) parrot the transcript format: they begin their
# own reply with the "[Name]: " prefix of the most recent prefixed message.
# The app then persists that prefix into the history, where it compounds
# turn after turn ([Luna]: [Alex]: [Luna]: ...). A capable model simply
# ignores the note, and a content filter could not tell a parroted prefix
# from a legitimate one — so the prompt is the only safe line of defense.
# Live-validated against Llama-3.2-1B: with the note, 16/16 two-persona
# rounds produced no prefixed replies; without it, 3/4 did.
_ANTI_MIMICRY_NOTE = (
    "Note: in this conversation, messages spoken by OTHER personas are "
    "displayed as '[Name]: text' — the prefix only identifies who spoke "
    "that message. It is NOT part of your reply and you must NEVER begin "
    "your own reply with a '[Name]:' prefix."
)


class SessionManager:
    """Manages the single active chat session."""

    def __init__(self):
        self._history: List[ChatMessage] = []
        self._active_personas: List[str] = []
        self._current_room: str = "default"

    # -- Public API ----------------------------------------------------------

    @property
    def current_room(self) -> str:
        return self._current_room

    def set_current_room(self, room_name: str):
        """Switch the active chat room.

        Messages are persisted individually as they arrive, so no bulk
        flush is needed here. Just updates the room tracker.
        """
        if room_name == self._current_room:
            return
        self._current_room = room_name

    def reset(self):
        """Wipe history, clear persistence for current room, and reset personas.
        Called on 'New Chat'.
        """
        persistence.clear_room(self._current_room)
        self._history.clear()
        self._active_personas.clear()

    def load_room(self, room_name: str):
        """Load persisted history for a room into the active session.

        Clears any existing in-memory history first, then populates from disk.
        Uses no-persist variants since messages are already on disk.
        """
        self._history.clear()
        self._current_room = room_name
        persisted = persistence.load_history(room_name)
        for msg in persisted:
            # Carry the persisted IDs over so ID-based operations (selective
            # deletion) keep working on reloaded rooms. .get(): a hand-edited
            # history.json row without an "id" degrades to ID-less rather
            # than crashing the room load.
            if msg["sender"] == "USER":
                self.add_user_message_no_persist(msg["text"], msg.get("id"))
            else:
                self.add_assistant_message_no_persist(msg["text"], msg["sender"], msg.get("id"))

    def set_active_personas(self, names: List[str]):
        """Replace the active persona list."""
        self._active_personas = list(names)

    @property
    def active_personas(self) -> List[str]:
        return list(self._active_personas)

    @property
    def history(self) -> List[ChatMessage]:
        return list(self._history)

    def add_user_message(self, content: str, message_id: str):
        """Append a user message to history and persist it."""
        message = ChatMessage(role="user", content=content, id=message_id)
        self._history.append(message)
        persistence.persist_message(self._current_room, message, message_id)

    def add_assistant_message(self, content: str, persona: str, message_id: str):
        """Append an assistant message to history and persist it."""
        message = ChatMessage(role="assistant", content=content, persona=persona, id=message_id)
        self._history.append(message)
        persistence.persist_message(self._current_room, message, message_id)

    def add_user_message_no_persist(self, content: str, message_id: Optional[str] = None):
        """Append a user message to history without persisting.

        Used when loading from disk (messages are already persisted).
        The ID is carried over so ID-based operations keep working.
        """
        self._history.append(ChatMessage(role="user", content=content, id=message_id))

    def add_assistant_message_no_persist(
        self, content: str, persona: str, message_id: Optional[str] = None
    ):
        """Append an assistant message to history without persisting.

        Used when loading from disk (messages are already persisted).
        The ID is carried over so ID-based operations keep working.
        """
        self._history.append(
            ChatMessage(role="assistant", content=content, persona=persona, id=message_id)
        )

    def remove_message_by_id(self, message_id: str) -> bool:
        """Remove the in-memory history entry with the given ID, if any.

        Matching is by ID only — never by text or sender — so two
        identical messages can't be mistaken for each other.
        Returns False (and changes nothing) when no entry carries the ID.
        """
        for i, msg in enumerate(self._history):
            if msg.id == message_id:
                del self._history[i]
                return True
        return False

    def build_llm_messages(
        self,
        system_prompt: str,
        responding_persona: str,
        max_turns_for_context: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """Build the messages list for an LLM call.

        - System message with the responding persona's system prompt.
          When the slice contains other personas' messages, the
          anti-mimicry note (see _ANTI_MIMICRY_NOTE) is appended to it.
        - Conversation history, reformatted so:
            * User messages keep role "user".
            * This persona's messages keep role "assistant".
            * Other personas' messages become "user" with prefix "[Name]: <text>".
        - Optionally limited to the last *max_turns_for_context* history entries.
        """
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]

        history_slice = self._history
        if max_turns_for_context is not None:
            history_slice = self._history[-max_turns_for_context:]

        saw_other_persona = False
        for msg in history_slice:
            if msg.role == "user":
                messages.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                if msg.persona == responding_persona:
                    messages.append({"role": "assistant", "content": msg.content})
                else:
                    # Another persona spoke — use role "user" to avoid consecutive
                    # assistant messages (which many LLMs reject with 400) and to
                    # prevent the model from treating another persona's words as its own.
                    saw_other_persona = True
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[{msg.persona}]: {msg.content}",
                        }
                    )

        if saw_other_persona:
            # Gated on the sliced history, not the full one: the note is
            # only worth the tokens when the "[Name]:" format is actually
            # in the context the model sees.
            messages[0]["content"] = (
                messages[0]["content"].rstrip()
                + "\n\n"
                + _ANTI_MIMICRY_NOTE
            )

        return messages

    def get_history_dicts(self) -> List[dict]:
        """Return history as plain dicts for JSON serialization."""
        return [m.model_dump() for m in self._history]


# Singleton instance — single-user app, one session to rule them all.
session = SessionManager()
