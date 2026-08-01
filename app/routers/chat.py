"""Chat router — the core SSE streaming endpoint.

Receives a user message, decides which persona should respond (router, random,
or explicit selection), streams tokens back via SSE, and appends the full
response to session history.
"""

import json
import logging
import random
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.config import get_personas
from app.models import ChatRequest
from app.session import session
from app.services.llm import chat_completion, stream_chat

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Persona router — asks the LLM to pick the best responder
# ---------------------------------------------------------------------------

def _build_router_prompt(user_message: str) -> list[dict]:
    """Build a minimal prompt that asks the LLM to pick a persona by name."""
    personas_config = get_personas().personas
    active = session.active_personas or [p.name for p in personas_config]
    active_personas = [p for p in personas_config if p.name in active]
    persona_choices = ", ".join(p.name for p in active_personas)

    # Build router hints block
    hints = "\n".join(
        f"- {p.name}: {p.router_hints}" for p in personas_config
    )

    # Include last N conversation turns for context
    recent = session.history[-6:] if len(session.history) >= 6 else session.history
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


async def _pick_persona(who_answers: str, user_message: str) -> str:
    """Determine which persona should respond.

    - "router": ask the LLM to decide
    - "random": pick randomly from active personas
    - explicit name: use that persona directly
    - anything else: fall back to random
    """
    config = get_personas()
    all_names = [p.name for p in config.personas]
    active = session.active_personas or all_names

    if who_answers == "random":
        return random.choice(active)

    if who_answers == "router":
        try:
            prompt = _build_router_prompt(user_message)
            result = await chat_completion(prompt, max_tokens=16)
            chosen = result.strip().strip("\"'")
            # Validate the LLM actually returned a known name
            if chosen in active:
                return chosen
            logger.info("Router returned unknown name '%s', falling back to random", chosen)
        except Exception as exc:
            logger.warning("Router call failed (%s), falling back to random", exc)
        return random.choice(active)

    # Explicit persona name — validate it exists
    if who_answers in active:
        return who_answers

    # Unknown value — fall back to random
    logger.info("Unrecognized who_answers='%s', falling back to random", who_answers)
    return random.choice(active)


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

async def _chat_stream(req: ChatRequest) -> AsyncIterator[str]:
    """Generator that yields SSE-formatted JSON lines."""
    persona_name = await _pick_persona(req.who_answers, req.message)
    config = get_personas()
    persona = next((p for p in config.personas if p.name == persona_name), None)
    if not persona:
        yield f'data: {json.dumps({"type": "error", "message": f"Persona {persona_name} not found"})}\n\n'
        return

    # Add user message to history
    session.add_user_message(req.message)

    # Build LLM messages with the responding persona's system prompt
    messages = session.build_llm_messages(
        system_prompt=persona.system_prompt,
        responding_persona=persona_name,
    )

    # Append the user's message to the messages list (it's the latest)
    # Actually, build_llm_messages already includes all history including the
    # user message we just added. So we're good.

    # Emit start event
    yield f'data: {json.dumps({"type": "start", "persona": persona_name})}\n\n'

    # Stream tokens
    full_text = ""
    try:
        async for token in stream_chat(messages):
            full_text += token
            yield f'data: {json.dumps({"type": "token", "persona": persona_name, "token": token})}\n\n'
    except Exception as exc:
        logger.error("Streaming error: %s", exc)
        yield f'data: {json.dumps({"type": "error", "message": str(exc)})}\n\n'
        return

    # Append full response to session history
    session.add_assistant_message(full_text, persona_name)

    # Emit done and complete events
    yield f'data: {json.dumps({"type": "done", "persona": persona_name, "text": full_text})}\n\n'
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
