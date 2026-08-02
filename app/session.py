"""In-memory session state with disk persistence.

Single-user app means one global session object.
History is a simple list of dicts: {"role": "user"|"assistant", "content": str, "persona": str|None}.

Messages are automatically persisted to disk per chat room as they arrive.
"""

from typing import Dict, List, Optional

from app.models import ChatMessage
from app import persistence


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
            if msg["sender"] == "USER":
                self.add_user_message_no_persist(msg["text"])
            else:
                self.add_assistant_message_no_persist(msg["text"], msg["sender"])

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
        self._history.append(ChatMessage(role="user", content=content))
        persistence.persist_message(self._current_room, self._history[-1], message_id)

    def add_assistant_message(self, content: str, persona: str, message_id: str):
        """Append an assistant message to history and persist it."""
        self._history.append(ChatMessage(role="assistant", content=content, persona=persona))
        persistence.persist_message(self._current_room, self._history[-1], message_id)

    def add_user_message_no_persist(self, content: str):
        """Append a user message to history without persisting.

        Used when loading from disk (messages are already persisted).
        """
        self._history.append(ChatMessage(role="user", content=content))

    def add_assistant_message_no_persist(self, content: str, persona: str):
        """Append an assistant message to history without persisting.

        Used when loading from disk (messages are already persisted).
        """
        self._history.append(ChatMessage(role="assistant", content=content, persona=persona))

    def build_llm_messages(
        self,
        system_prompt: str,
        responding_persona: str,
        max_turns: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """Build the messages list for an LLM call.

        - System message with the responding persona's system prompt.
        - Conversation history, reformatted so:
            * User messages keep role "user".
            * This persona's messages keep role "assistant".
            * Other personas' messages become "assistant" with prefix "[Name]: <text>".
        - Optionally limited to the last *max_turns* history entries.
        """
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]

        history_slice = self._history
        if max_turns is not None:
            history_slice = self._history[-max_turns:]

        for msg in history_slice:
            if msg.role == "user":
                messages.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                if msg.persona == responding_persona:
                    messages.append({"role": "assistant", "content": msg.content})
                else:
                    # Another persona spoke — prefix it so the model knows
                    messages.append(
                        {
                            "role": "assistant",
                            "content": f"[{msg.persona}]: {msg.content}",
                        }
                    )

        return messages

    def get_history_dicts(self) -> List[dict]:
        """Return history as plain dicts for JSON serialization."""
        return [m.model_dump() for m in self._history]


# Singleton instance — single-user app, one session to rule them all.
session = SessionManager()
