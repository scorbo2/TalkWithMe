"""Chat router — the core SSE streaming endpoint.

Receives a user message, decides which persona should respond (router, random,
or explicit selection), streams tokens back via SSE, and appends the full
response to session history. Messages are persisted to disk per chat room.
"""

import json
import logging
import random
import uuid
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.config import get_chatrooms, get_personas, get_settings
from app.models import ChatRequest
from app.session import session
from app.services import builtin, persona_store
from app.services.llm import chat_completion, stream_chat, stream_chat_with_tools
from app.services.tool_registry import get_all_tools

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Persona pool resolution — derive eligible personas from chat room config
# ---------------------------------------------------------------------------

def _resolve_room_personas(chat_room: str) -> list[str]:
    """Return the list of persona names eligible for the given chat room.

    "default" room (or any room not found in config) includes all personas.
    Named rooms are limited to their assigned persona_names.
    This is the authoritative source of truth for persona eligibility —
    no longer dependent on the frontend-maintained session.active_personas.
    """
    all_names = [p.name for p in get_personas().personas]

    if chat_room.lower() == "default":
        return all_names

    chatrooms_config = get_chatrooms()
    room = next(
        (r for r in chatrooms_config.chat_rooms if r.name.lower() == chat_room.lower()),
        None,
    )
    if room is not None:
        # Only include personas that actually exist in the config (empty list means "no one is here")
        return [n for n in room.persona_names if n in all_names]

    # Unknown room — fall back to all personas rather than blocking the chat
    return all_names


# ---------------------------------------------------------------------------
# Persona router — asks the LLM to pick the best responder
# ---------------------------------------------------------------------------

def _build_router_prompt(user_message: str, chat_room: str) -> list[dict]:
    """Build a minimal prompt that asks the LLM to pick a persona by name."""
    personas_config = get_personas().personas
    eligible = _resolve_room_personas(chat_room)
    active_personas = [p for p in personas_config if p.name in eligible]
    persona_choices = ", ".join(p.name for p in active_personas)

    # Build router hints block — only for personas actually eligible in this room
    hints = "\n".join(
        f"- {p.name}: {p.router_hints}" for p in active_personas
    )

    # Include last N conversation turns for context
    max_context = get_settings().general.max_turns_for_context
    recent = session.history[-max_context:]
    context_lines = []
    for msg in recent:
        if msg.role == "user":
            context_lines.append(f"User: {msg.content}")
        else:
            context_lines.append(f"{msg.persona}: {msg.content}")
    context = "\n".join(context_lines)

    system = (
        "You are a conversation router. Your ONLY job is to pick the best "
        "persona to respond to the user's latest message.\n\n"
        f"Available personas:\n{hints}\n\n"
        f"Recent conversation:\n{context}\n\n"
        f"User's latest message: {user_message}\n\n"
        "Respond with ONLY the name of the best persona. Choose from: "
        f"{persona_choices}. Do not add any explanation."
    )

    return [{"role": "system", "content": system}, {"role": "user", "content": "Pick one persona."}]


async def _pick_persona(who_answers: str, user_message: str, chat_room: str) -> str:
    """Determine which persona should respond.

    - "router": ask the LLM to decide
    - "random": pick randomly from eligible room personas
    - explicit name: use that persona directly
    - anything else: fall back to random
    """
    eligible = _resolve_room_personas(chat_room)

    if not eligible:
        raise ValueError(f"No eligible personas for room '{chat_room}'")

    if who_answers == "random":
        return random.choice(eligible)

    if who_answers == "router":
        try:
            prompt = _build_router_prompt(user_message, chat_room)
            result = await chat_completion(prompt, max_tokens=16)
            chosen = result.strip().strip("\"'")
            # Validate the LLM actually returned an eligible name
            if chosen in eligible:
                return chosen
            logger.info("Router returned unknown name '%s', falling back to random", chosen)
        except Exception as exc:
            logger.warning("Router call failed (%s), falling back to random", exc)
        return random.choice(eligible)

    # Explicit persona name — validate it's in this room
    if who_answers in eligible:
        return who_answers

    # Unknown value — fall back to random
    logger.info("Unrecognized who_answers='%s', falling back to random", who_answers)
    return random.choice(eligible)


# ---------------------------------------------------------------------------
# Memory injection (docs/feature_persona_memory.md)
# ---------------------------------------------------------------------------

def _system_prompt_with_memories(persona, settings) -> str:
    """The persona's system prompt, with saved memories appended if eligible.

    Qualifying conditions: the global enable_persona_memories flag, a
    non-zero memory_size, and a memories.txt that exists and is not
    blank. Note that allow_tool_calls is deliberately NOT part of this
    gate: a persona that may not call tools can still benefit from
    memories it saved earlier (injection and adding are independent).

    The memory budget is enforced on the read path as well as the write
    path: the file may have been edited by an external process (the
    README explicitly encourages it), so an over-limit file is purged
    oldest-first to the persona's memory_size before injection, rather
    than being handed to the LLM verbatim.
    """
    if not (settings.general.enable_persona_memories and persona.memory_size > 0):
        return persona.system_prompt
    if persona.persona_dir is None:
        return persona.system_prompt
    # Cheap no-op when the file is already within budget; repairs the
    # on-disk file as a side effect when it isn't (e.g. an external
    # writer ignored the persona's budget).
    persona_store.purge_memories_to_limit(persona.persona_dir, persona.memory_size)
    memories = persona_store.read_memories(persona.persona_dir)
    if not memories.strip():
        return persona.system_prompt
    return (
        persona.system_prompt
        + "\n\nYou have the following memories related to the user:\n"
        + memories
    )


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

async def _chat_stream(req: ChatRequest) -> AsyncIterator[str]:
    """Generator that yields SSE-formatted JSON lines."""
    # Switch to the requested chat room for persistence
    session.set_current_room(req.chat_room)

    # Resolve eligible personas from the chat room config — the authoritative source
    config = get_personas()
    eligible = _resolve_room_personas(req.chat_room)

    if not eligible:
        yield f'data: {json.dumps({"type": "error", "message": "No eligible personas for this room"})}\n\n'
        yield f'data: {json.dumps({"type": "complete"})}\n\n'
        return

    settings = get_settings()
    max_replies = min(settings.general.max_persona_replies, len(eligible))

    # Pick the first persona using the configured strategy
    first_persona_name = await _pick_persona(req.who_answers, req.message, req.chat_room)

    # Use frontend-provided message ID or generate one
    user_message_id = req.message_id or str(uuid.uuid4())

    # Add user message to history (persisted automatically)
    session.add_user_message(req.message, user_message_id)

    # Check if echo chamber is enabled for this room (case-insensitive lookup)
    chatrooms_config = get_chatrooms()
    room = next(
        (r for r in chatrooms_config.chat_rooms if r.name.lower() == req.chat_room.lower()),
        None,
    )
    echo_enabled = room.echo_chamber if room else False

    # Echo chamber overrides max_replies — only one persona echoes the user.
    # Multiple identical echoes from different personas would be pointless noise.
    if echo_enabled:
        max_replies = 1

    replied_personas: list[str] = []

    for reply_idx in range(max_replies):
        if reply_idx == 0:
            persona_name = first_persona_name
        else:
            remaining = [n for n in eligible if n not in replied_personas]
            if not remaining:
                break
            persona_name = random.choice(remaining)

        persona = next((p for p in config.personas if p.name == persona_name), None)
        if not persona:
            yield f'data: {json.dumps({"type": "error", "message": f"Persona {persona_name} not found"})}\n\n'
            return

        replied_personas.append(persona_name)

        # Generate the assistant message ID BEFORE emitting "start". The
        # frontend stamps it onto every TTS item enqueued during this
        # response, so audio is associated with the correct message no
        # matter when each fetch resolves. Generating it after the stream
        # (and backfilling later) is how audio got misattributed across turns.
        assistant_message_id = str(uuid.uuid4())

        # Emit start event — include the user's message_id so frontend can track it,
        # and this response's message_id so streaming TTS audio can be associated
        # with the correct message from the first token onward.
        yield f'data: {json.dumps({"type": "start", "persona": persona_name, "user_message_id": user_message_id, "message_id": assistant_message_id})}\n\n'

        if echo_enabled:
            # Echo chamber: bypass the LLM entirely and return the user's message verbatim.
            full_text = req.message
            yield f'data: {json.dumps({"type": "token", "persona": persona_name, "token": full_text})}\n\n'
        else:
            # Normal path: stream LLM response (history already includes prior personas' replies)
            messages = session.build_llm_messages(
                system_prompt=_system_prompt_with_memories(persona, settings),
                responding_persona=persona_name,
                max_turns_for_context=settings.general.max_turns_for_context,
            )
            full_text = ""
            try:
                if persona.allow_tool_calls:
                    # Agentic path: the LLM may invoke MCP tools AND the
                    # built-in tools (add_memory) mid-reply. The loop runs
                    # regardless of show_tool_calls; that flag only controls
                    # whether tool_call SSE events are emitted.
                    tools = get_all_tools() + builtin.get_builtin_tools_for(persona, settings)
                    async for event in stream_chat_with_tools(messages, tools, persona):
                        if event["type"] == "token":
                            full_text += event["token"]
                            yield f'data: {json.dumps({"type": "token", "persona": persona_name, "token": event["token"]})}\n\n'
                        elif event["type"] == "tool_call" and settings.general.show_tool_calls:
                            yield f'data: {json.dumps({"type": "tool_call", "persona": persona_name, "tool_name": event["tool_name"], "arguments": event["arguments"], "result": event["result"], "failed": event["failed"]})}\n\n'
                else:
                    async for token in stream_chat(messages):
                        full_text += token
                        yield f'data: {json.dumps({"type": "token", "persona": persona_name, "token": token})}\n\n'
            except Exception as exc:
                logger.error("Streaming error: %s", exc)
                yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
                return

        # Persist — subsequent personas will see this in history
        session.add_assistant_message(full_text, persona_name, assistant_message_id)

        yield f'data: {json.dumps({"type": "done", "persona": persona_name, "text": full_text, "message_id": assistant_message_id})}\n\n'

    yield f'data: {json.dumps({"type": "complete"})}\n\n'


@router.post("")
async def chat(req: ChatRequest):
    """Accept a user message and return an SSE stream of the AI response."""
    return StreamingResponse(
        _chat_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
