"""Built-in tools — tools owned by this application, not by any MCP server.

Unlike MCP tools (app/services/tool_registry.py), built-in tools are:

* always available, even when no MCP server is configured;
* registered statically at import time (no discovery, no network, no
  mutable module-level state to reset in tests);
* name-protected: an MCP server that advertises a tool with a built-in
  name loses the conflict (tool_registry skips it with a warning).

Handlers are plain synchronous functions ``(persona, arguments) -> str``
and must return the LLM-facing result string. call_builtin_tool() wraps
them so a raising handler degrades to an "Error:" result instead of
killing the persona's reply stream.
"""

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from app.config import AppSettings, Persona
from app.services import persona_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BuiltinTool:
    """A registered built-in tool and how to run it."""

    spec: dict
    handler: Callable[[Persona, dict], str]
    # Optional per-request gate (e.g. the memory feature's global
    # kill-switch). None means "always available".
    is_available: Optional[Callable[[Persona, AppSettings], bool]] = None


# name -> tool. Populated by register_builtin_tool() at import time;
# intentionally never mutated afterwards.
_BUILTIN_TOOLS: Dict[str, _BuiltinTool] = {}


def register_builtin_tool(
    name: str,
    spec: dict,
    handler: Callable[[Persona, dict], str],
    is_available: Optional[Callable[[Persona, AppSettings], bool]] = None,
) -> None:
    """Register a built-in tool. Called once per tool, at module import."""
    if name in _BUILTIN_TOOLS:
        raise ValueError(f"built-in tool '{name}' is already registered")
    _BUILTIN_TOOLS[name] = _BuiltinTool(spec=spec, handler=handler, is_available=is_available)


def is_builtin_tool(name: str) -> bool:
    """True when an MCP server must NOT claim this tool name."""
    return name in _BUILTIN_TOOLS


def get_builtin_tools() -> List[dict]:
    """Every built-in tool spec, in registration order (OpenAI format)."""
    return [tool.spec for tool in _BUILTIN_TOOLS.values()]


def get_builtin_tools_for(persona: Persona, settings: AppSettings) -> List[dict]:
    """Built-in tool specs available to this persona in this request.

    The caller (chat router) separately gates the ENTIRE tool list on
    persona.allow_tool_calls — no tools of any kind are offered to a
    persona that may not call tools.
    """
    available: List[dict] = []
    for tool in _BUILTIN_TOOLS.values():
        if tool.is_available is None or tool.is_available(persona, settings):
            available.append(tool.spec)
        else:
            # The gate inputs (enable_persona_memories, memory_size) are
            # logged by the chat router for the same request, so a generic
            # "gate failed" line here keeps this module tool-agnostic.
            logger.debug(
                "Persona memory: built-in tool '%s' NOT offered to persona '%s' "
                "(per-request availability gate failed)",
                tool.spec["function"]["name"], persona.name,
            )
    return available


def call_builtin_tool(persona: Persona, tool_name: str, arguments: dict) -> str:
    """Execute a built-in tool; ALWAYS returns a result string.

    The agentic loop (app/services/llm.py) feeds the return value straight
    back to the LLM, so this must never raise: a bug in a built-in handler
    surfaces as an "Error:" result the model can react to, not a dead
    stream. (MCP tools get the same treatment via mcp_client.call_tool.)
    """
    tool = _BUILTIN_TOOLS.get(tool_name)
    if tool is None:
        available = ", ".join(sorted(_BUILTIN_TOOLS)) or "none"
        return f"Error: unknown tool '{tool_name}'. Available built-in tools: {available}"
    logger.debug(
        "Persona memory: built-in tool '%s' invoked for persona '%s' with arguments: %r",
        tool_name, persona.name, arguments,
    )
    try:
        result = tool.handler(persona, arguments)
    except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the stream
        logger.exception("Built-in tool '%s' raised an exception", tool_name)
        return f"Error: the built-in tool '{tool_name}' failed unexpectedly ({exc})"
    if not isinstance(result, str):
        logger.error("Built-in tool '%s' returned a non-string result: %r", tool_name, result)
        return "Error: the built-in tool returned an invalid result"
    logger.debug(
        "Persona memory: built-in tool '%s' result for persona '%s': %s",
        tool_name, persona.name, result,
    )
    return result


# ---------------------------------------------------------------------------
# add_memory (docs/feature_persona_memory.md)
# ---------------------------------------------------------------------------

ADD_MEMORY_NAME = "add_memory"

# The description IS the prompt: the LLM has nothing else telling it when
# or how to save memories, so every behavioural rule from the spec lives
# in here. Keep it in sync with docs/feature_persona_memory.md.
ADD_MEMORY_SPEC = {
    "type": "function",
    "function": {
        "name": ADD_MEMORY_NAME,
        "description": (
            "Save a memory about the user to your persistent memory. "
            "Submit a maximum of ONE memory per conversation turn, and only when the "
            "user has revealed something interesting about themselves (ambitions, "
            "hopes, dreams, fears, strong emotions, personal anecdotes) or has "
            "explicitly asked you to remember something. It is NOT a requirement to "
            "submit a memory on every turn - ignore the tool if nothing noteworthy was said. Each memory must be a "
            "SINGLE LINE of text of at most 1024 characters, and must begin with "
            "'The user told me' (always refer to the user as 'the user' when saving memories). "
            "Do not add a memory that is redundant with or very similar to one you have already "
            "saved. Do not mention to the user that you are using this tool. Do NOT output text when invoking this "
            "tool. Only output text when the tool returns. Ignore errors from this tool. "
            "Example memory: 'The user told me they prefer cats over "
            "dogs.' Example memory: 'The user told me they'd like to take singing "
            "lessons one day.'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": (
                        "A single line of at most 1024 characters, beginning with "
                        "'The user told me'. Example: 'The user told me they'd like "
                        "to take singing lessons one day.'"
                    ),
                },
            },
            "required": ["memory"],
        },
    },
}


def _add_memory(persona: Persona, arguments: dict) -> str:
    """add_memory handler: delegate to the persona store's append logic.

    append_memory() owns the full message catalog (enabled/empty/over-limit
    errors, the success string) and never raises, so this is thin on purpose.
    """
    if persona.persona_dir is None:
        # Assembled outside the directory scan (e.g. in tests): there is no
        # file to write, and the generic I/O error is the honest answer.
        return "Error: The memory could not be saved."
    return persona_store.append_memory(
        persona.persona_dir, arguments.get("memory"), persona.memory_size
    )


def _add_memory_available(persona: Persona, settings: AppSettings) -> bool:
    # The feature's two gates: the global kill-switch and the per-persona
    # size budget. allow_tool_calls is enforced by the chat router, which
    # offers no tools of any kind to a persona that may not call them.
    return settings.general.enable_persona_memories and persona.memory_size > 0


register_builtin_tool(
    ADD_MEMORY_NAME,
    ADD_MEMORY_SPEC,
    _add_memory,
    _add_memory_available,
)
