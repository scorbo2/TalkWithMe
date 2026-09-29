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

from app.config import get_chatrooms, get_personas, get_settings, room_echo_enabled
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

def _recent_context() -> str:
    """The last max_turns_for_context turns as "User: ..." / "<Persona>: ..." lines."""
    max_context = get_settings().general.max_turns_for_context
    lines = []
    for msg in session.history[-max_context:]:
        if msg.role == "user":
            lines.append(f"User: {msg.content}")
        else:
            lines.append(f"{msg.persona}: {msg.content}")
    return "\n".join(lines)


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

    context = _recent_context()

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
# Dynamic replies — after each reply, the router decides who (if anyone) is next
# ---------------------------------------------------------------------------

_NO_NEXT_SPEAKER = "NONE"


def _build_next_speaker_prompt(chat_room: str, last_speaker: str, replies_so_far: int,
                               max_replies: int) -> list[dict]:
    """Prompt asking the LLM who reacts to the last reply, or NONE to end the round."""
    personas_config = get_personas().personas
    eligible = _resolve_room_personas(chat_room)
    candidates = [p for p in personas_config if p.name in eligible and p.name != last_speaker]
    hints = "\n".join(f"- {p.name}: {p.router_hints}" for p in candidates)
    choices = ", ".join([p.name for p in candidates] + [_NO_NEXT_SPEAKER])

    system = (
        "You are directing a group conversation between a user and several personas. "
        "Decide who, if anyone, speaks next.\n\n"
        f"Personas who could speak next:\n{hints}\n\n"
        f"Recent conversation:\n{_recent_context()}\n\n"
        f"{last_speaker} just spoke. The personas have replied {replies_so_far} time(s) "
        f"since the user's last message (at most {max_replies}).\n\n"
        "Pick the persona who would naturally react to what was just said: someone who "
        "was addressed, contradicted, blamed or mentioned, or who would clearly have "
        f"something to add. Answer {_NO_NEXT_SPEAKER} when the exchange has reached a natural "
        "pause, when a question to the user is waiting for an answer, or when nobody "
        "has a strong reason to speak. Real conversations rarely go on for long without "
        f"the user: the more replies so far, the more likely {_NO_NEXT_SPEAKER} is right.\n\n"
        f"Respond with ONLY one of: {choices}. Do not add any explanation."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": "Who speaks next?"}]


def _round_continues(replies_so_far: int, max_replies: int) -> bool:
    """Taper: should the router even be asked for another reply?

    Personas written to react to each other (GLaDOS and Wheatley blame each
    other in every line) always give the router a reason to continue, so a
    round would always run to the cap. The chance to go on falls linearly
    with each reply: the first follow-up is always offered, the last
    possible one only with 1/(max-1) — for a cap of 4: 100 %, 67 %, 33 %.
    """
    if max_replies <= 1 or replies_so_far >= max_replies:
        return False
    chance = 1.0 - (replies_so_far - 1) / (max_replies - 1)
    return random.random() < chance


async def _pick_next_speaker(chat_room: str, last_speaker: str, replies_so_far: int,
                             max_replies: int) -> str | None:
    """The next dynamic responder, or None to end the round.

    Never the persona that just spoke (no talking to oneself); anyone else in
    the room may come back, so GLaDOS -> Wheatley -> GLaDOS is possible. An
    unknown name, NONE, or an LLM failure ends the round: in doubt, the user
    gets the floor back rather than a random persona.
    """
    candidates = [n for n in _resolve_room_personas(chat_room) if n != last_speaker]
    if not candidates:
        return None
    try:
        prompt = _build_next_speaker_prompt(chat_room, last_speaker, replies_so_far, max_replies)
        result = await chat_completion(prompt, max_tokens=16)
    except Exception as exc:
        logger.warning("Next-speaker call failed (%s), ending the round", exc)
        return None
    chosen = result.strip().strip("\"'.")
    if chosen in candidates:
        logger.debug("Dynamic replies: %s speaks after %s", chosen, last_speaker)
        return chosen
    if chosen.upper() != _NO_NEXT_SPEAKER:
        logger.info("Next-speaker call returned unknown name '%s', ending the round", chosen)
    return None


# ---------------------------------------------------------------------------
# Responder planning — how many (and which) personas answer one message
# ---------------------------------------------------------------------------

def _plan_responders(eligible: list[str], first: str, count: int) -> list[str]:
    """Plan the ordered responder sequence for one user message.

    Slot 0 is always `first` (picked per the configured selection
    strategy); each subsequent slot is a randomly ordered, non-repeating
    pick from the remaining eligible personas. The plan stops at `count`
    entries or when the room runs out of personas, whichever comes first
    (`count <= 0` yields an empty plan). Pure apart from the RNG, so the
    selection strategy is unit-testable without the SSE endpoint.
    """
    if count <= 0:
        return []
    plan = [first]
    remaining = [name for name in eligible if name not in plan]
    plan.extend(random.sample(remaining, min(count - 1, len(remaining))))
    return plan


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
        logger.debug(
            "Persona memory: NOT injecting saved memories for '%s' "
            "(enable_persona_memories=%s, memory_size=%d)",
            persona.name, settings.general.enable_persona_memories, persona.memory_size,
        )
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
    memory_lines = [line for line in memories.splitlines() if line.strip()]
    logger.debug(
        "Persona memory: injecting %d saved memory line(s) into the system prompt of '%s'",
        len(memory_lines), persona.name,
    )
    return (
        persona.system_prompt
        + "\n\nYou have the following memories related to the user:\n"
        + memories
    )


def _with_global_system_prompt(system_prompt: str, settings) -> str:
    """Append general.global_system_prompt to a persona's final system prompt.

    Appended AFTER the persona prompt and any injected memories, so global
    rules sit at the very end of the prompt — the spot the LLM is most
    likely to weigh when persona-specific instructions disagree (e.g. a
    persona that likes markdown vs. a global "plain text only for TTS").
    A blank line separates the two sections. Empty/whitespace-only values
    leave the prompt untouched; surrounding whitespace is stripped so a
    stray trailing newline from the settings textarea never lands in the
    prompt.
    """
    global_prompt = (settings.general.global_system_prompt or "").strip()
    if not global_prompt:
        return system_prompt
    # rstrip the base so the separator is EXACTLY one blank line: the
    # memories block (and hand-edited persona prompts) may already end with
    # their own trailing newline, which would otherwise double it up.
    return system_prompt.rstrip() + "\n\n" + global_prompt


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
    # Echo chamber keeps the fixed plan: it exists to hear every voice speak
    # the same line, which a "who reacts?" decision would defeat.
    dynamic = settings.general.dynamic_replies and not room_echo_enabled(get_chatrooms(), req.chat_room)
    if dynamic:
        # A persona may speak again after someone else, so the cap is not
        # limited to the room size here; a one-persona room still ends after
        # one reply (nobody else to hand over to).
        max_replies = settings.general.max_persona_replies

    # Pick the first persona using the configured strategy
    first_persona_name = await _pick_persona(req.who_answers, req.message, req.chat_room)

    # Use frontend-provided message ID or generate one
    user_message_id = req.message_id or str(uuid.uuid4())

    # Add user message to history (persisted automatically)
    session.add_user_message(req.message, user_message_id)

    # Check if echo chamber is enabled for this room (case-insensitive
    # lookup; the "default" room's flag lives in the config, not in a
    # room record — room_echo_enabled() knows about that).
    echo_enabled = room_echo_enabled(get_chatrooms(), req.chat_room)

    # Echo chamber: the LLM is bypassed for EVERY planned echo, so no
    # tool calls (add_memory included) can happen. The echo count follows
    # max_persona_replies (see _plan_responders) — this is what lets a
    # voice-tester hear every persona in the room speak the same line.
    if echo_enabled:
        logger.debug(
            "Echo chamber: room '%s' — the LLM is bypassed entirely for all "
            "%d echo(es), so NO tool calls (add_memory included) can happen",
            req.chat_room, max_replies,
        )

    # Plan the full responder sequence up front (first persona from the
    # configured selection strategy, then random non-repeating picks from
    # the remaining eligible personas until the cap or the room runs out).
    # Dynamic replies start with the first persona only and grow one at a
    # time, after each reply (see _pick_next_speaker).
    if dynamic:
        responders = [first_persona_name]
    else:
        responders = _plan_responders(eligible, first_persona_name, max_replies)

    index = 0
    while index < len(responders):
        persona_name = responders[index]
        index += 1
        persona = next((p for p in config.personas if p.name == persona_name), None)
        if not persona:
            yield f'data: {json.dumps({"type": "error", "message": f"Persona {persona_name} not found"})}\n\n'
            return

        # Diagnostic trail (DEBUG): the three inputs the add_memory feature
        # gates on, exactly as the runtime sees them (post-cache, post-parse).
        logger.debug(
            "Persona memory: persona '%s' decision inputs: allow_tool_calls=%s, "
            "memory_size=%d, enable_persona_memories=%s, persona_dir=%s",
            persona_name, persona.allow_tool_calls, persona.memory_size,
            settings.general.enable_persona_memories, persona.persona_dir,
        )

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
                system_prompt=_with_global_system_prompt(
                    _system_prompt_with_memories(persona, settings), settings),
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
                    # MCP tools are filtered to this persona's grants
                    # (issue #138); built-ins are unaffected by the lists.
                    tools = get_all_tools(persona_name) + builtin.get_builtin_tools_for(persona, settings)
                    logger.debug(
                        "Persona memory: persona '%s' — agentic path, %d tool(s) supplied to LLM: %s",
                        persona_name, len(tools),
                        [t["function"]["name"] for t in tools],
                    )
                    async for event in stream_chat_with_tools(messages, tools, persona):
                        if event["type"] == "token":
                            full_text += event["token"]
                            yield f'data: {json.dumps({"type": "token", "persona": persona_name, "token": event["token"]})}\n\n'
                        elif event["type"] == "tool_call" and settings.general.show_tool_calls:
                            yield f'data: {json.dumps({"type": "tool_call", "persona": persona_name, "tool_name": event["tool_name"], "arguments": event["arguments"], "result": event["result"], "failed": event["failed"]})}\n\n'
                else:
                    logger.debug(
                        "Persona memory: persona '%s' — allow_tool_calls is False, "
                        "plain streaming path taken; NO tools of any kind supplied to LLM",
                        persona_name,
                    )
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

        if dynamic and _round_continues(len(responders), max_replies):
            next_name = await _pick_next_speaker(
                req.chat_room, persona_name, len(responders), max_replies)
            if next_name:
                responders.append(next_name)

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
