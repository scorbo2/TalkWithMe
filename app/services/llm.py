"""Streaming client for a locally running llama.cpp server.

The llama.cpp server exposes an OpenAI-compatible API at /v1/chat/completions.
We stream tokens via SSE for the main chat flow, and do a quick non-streaming
call for the persona router.

Tool calling: stream_chat_with_tools() runs a fully agentic loop — when the
LLM answers with tool_calls, we invoke each tool (built-in tools from
app/services/builtin.py first, everything else on its owning MCP server
via app/services/tool_registry.py), feed the results back, and repeat until
the LLM produces a plain text answer.
"""

import json
import logging
import uuid
from typing import AsyncGenerator, Dict, List, Optional

import httpx

from app.config import Persona, get_settings
from app.services import builtin, mcp_client
from app.services.tool_registry import get_server_for_tool

logger = logging.getLogger(__name__)


def _base_payload(messages: List[dict]) -> dict:
    """Common /v1/chat/completions payload fields (model, sampling, streaming)."""
    settings = get_settings()
    return {
        "model": settings.llm.model,
        "messages": messages,
        "max_tokens": settings.llm.max_tokens,
        "temperature": settings.llm.temperature,
        "stream": True,
    }


async def _iter_completion_chunks(payload: dict) -> AsyncGenerator[dict, None]:
    """Yield one `choices[0]` dict per SSE data line from the LLM.

    Each dict carries the "delta" and, on the final line, "finish_reason".
    Malformed lines are logged and skipped rather than aborting the stream.
    """
    settings = get_settings()
    url = f"{settings.llm.base_url}/v1/chat/completions"

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    yield chunk["choices"][0]
                except (json.JSONDecodeError, KeyError, IndexError) as exc:
                    logger.warning("Malformed SSE chunk from LLM: %s", exc)
                    continue


async def stream_chat(
    messages: List[Dict[str, str]],
) -> AsyncGenerator[str, None]:
    """Stream tokens from the LLM's /v1/chat/completions endpoint.

    Yields individual token strings as they arrive.
    """
    payload = _base_payload(messages)
    async for choice in _iter_completion_chunks(payload):
        token = (choice.get("delta") or {}).get("content") or ""
        if token:
            yield token


async def chat_completion(messages: List[Dict[str, str]], max_tokens: int = 64) -> str:
    """Non-streaming LLM call. Used for the persona router.

    Returns the full response text, or empty string on failure.
    """
    settings = get_settings()
    url = f"{settings.llm.base_url}/v1/chat/completions"

    payload = {
        "model": settings.llm.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,  # Low temperature for deterministic routing
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
            return body["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("LLM non-streaming call failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Agentic tool-call loop
# ---------------------------------------------------------------------------

def _merge_tool_call_delta(pending: Dict[int, dict], delta: dict) -> None:
    """Accumulate a streamed tool-call delta into the per-index accumulator.

    Tool calls arrive in fragments: the first chunk carries id/type/name,
    later chunks append to function.arguments (and occasionally name).
    Backends that re-send the full name in later deltas are handled by
    resyncing to the latest copy rather than concatenating it.
    """
    index = delta.get("index", 0)
    entry = pending.setdefault(
        index, {"type": "function", "function": {"name": "", "arguments": ""}}
    )
    if delta.get("id"):
        entry["id"] = delta["id"]
    if delta.get("type"):
        entry["type"] = delta["type"]
    fn = delta.get("function") or {}
    if fn.get("name"):
        current_name = entry["function"]["name"]
        if current_name and current_name in fn["name"]:
            # The incoming name CONTAINS what we already have — this is the
            # signature of a backend re-sending the name (in full) in a later
            # delta; concatenating would yield "get_timeget_time". The check
            # is directional on purpose: a genuine name fragment is almost
            # never a superstring of the accumulated name, so normal
            # fragment streaming is unaffected.
            entry["function"]["name"] = fn["name"]
        else:
            entry["function"]["name"] = current_name + fn["name"]
    if fn.get("arguments"):
        entry["function"]["arguments"] += fn["arguments"]


def _normalize_tool_call(tc: dict) -> dict:
    """Fill in the fields a tool call needs before it is sent anywhere."""
    normalized = dict(tc)
    # Some backends omit the id; the follow-up "tool" message must reference
    # it, so synthesize one when missing.
    normalized["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
    normalized.setdefault("type", "function")
    function = dict(normalized.get("function") or {})
    function["name"] = function.get("name") or ""
    function["arguments"] = function.get("arguments") or ""
    normalized["function"] = function
    return normalized


def _try_parse_arguments(raw: str) -> Optional[dict]:
    """Parse the JSON argument string the LLM produced.

    Returns None when the string is not valid JSON — typically a call cut
    off mid-stream at max_tokens. Callers must refuse to execute such a
    call rather than falling back to guessed/empty arguments.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON tool arguments: %.200s", raw)
        return None
    return parsed if isinstance(parsed, dict) else {"value": parsed}


async def stream_chat_with_tools(
    messages: List[dict],
    tools: List[dict],
    persona: Persona,
) -> AsyncGenerator[dict, None]:
    """Stream a persona reply, running an agentic tool-call loop.

    Yields event dicts (not SSE strings; the chat router formats them):
      {"type": "token", "token": str}
      {"type": "tool_call", "tool_name": str, "arguments": dict,
       "result": str, "failed": bool}

    ``persona`` is required (not defaulted): built-in tools execute
    against it (e.g. add_memory writes to the persona's directory), so a
    caller that omits it is a bug, not a "no built-ins" case.

    The loop continues while the LLM answers with tool_calls, up to
    mcp.max_tool_iterations tool rounds. The FINAL round is sent without
    the tools list so the LLM is forced to produce text — this guarantees
    a non-empty response even when the iteration cap is exhausted.

    Tool calls whose arguments do not parse as JSON (usually a call
    truncated at max_tokens, i.e. finish_reason "length") are NOT
    executed; the LLM receives an "Error:" result explaining why, and
    can retry with a smaller request or answer without the tool.
    """
    settings = get_settings()
    max_iterations = settings.mcp.max_tool_iterations
    tool_list = tools or []

    conversation = list(messages)
    for round_num in range(max_iterations + 1):
        is_final_round = round_num == max_iterations
        payload = _base_payload(conversation)
        if tool_list and not is_final_round:
            payload["tools"] = tool_list

        content_parts: List[str] = []
        pending_tool_calls: Dict[int, dict] = {}
        finish_reason: Optional[str] = None
        async for choice in _iter_completion_chunks(payload):
            delta = choice.get("delta") or {}
            token = delta.get("content") or ""
            if token:
                content_parts.append(token)
                yield {"type": "token", "token": token}
            for tc_delta in delta.get("tool_calls") or []:
                _merge_tool_call_delta(pending_tool_calls, tc_delta)
            # Intermediate chunks carry null; keep the last non-null value.
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

        if not pending_tool_calls:
            return  # Plain text response — the loop is done.

        # Pathological case: the model emitted tool calls even though no
        # tools were offered on the final round. Executing hallucinated
        # calls would be worse than stopping, so we don't.
        if is_final_round:
            logger.warning(
                "LLM still emitted tool calls on the final (tool-less) round; stopping"
            )
            return

        tool_calls = [
            _normalize_tool_call(pending_tool_calls[i]) for i in sorted(pending_tool_calls)
        ]
        logger.info(
            "Tool call round %d/%d: %s",
            round_num + 1, max_iterations,
            [tc["function"]["name"] for tc in tool_calls],
        )

        # Append the assistant's tool-call message, then each tool result,
        # in the exact pairing the OpenAI-compatible API expects.
        conversation.append({
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            arguments = _try_parse_arguments(tc["function"]["arguments"])
            if arguments is None:
                # Unparseable arguments — almost always a call truncated by
                # max_tokens (finish_reason "length"). Executing it with
                # guessed/empty args would be worse than refusing: the LLM
                # sees the refusal and can retry smaller or answer directly.
                logger.warning(
                    "Not executing tool '%s': arguments not valid JSON (finish_reason=%s)",
                    tool_name, finish_reason,
                )
                result = (
                    f"Error: the call to '{tool_name}' was not executed because "
                    "its arguments were not valid JSON"
                    + (
                        " (the response hit max_tokens mid-call). Retry with a "
                        "smaller request or answer without the tool."
                        if finish_reason == "length"
                        else ". Retry with valid arguments or answer without the tool."
                    )
                )
                arguments = {}
            else:
                if builtin.is_builtin_tool(tool_name):
                    # Built-ins win name collisions (tool_registry never
                    # registers an MCP tool under a built-in name), and
                    # they run locally — no server lookup, no network.
                    result = builtin.call_builtin_tool(persona, tool_name, arguments)
                else:
                    server = get_server_for_tool(tool_name)
                    if server is None:
                        available = [t["function"]["name"] for t in tool_list]
                        result = (f"Error: unknown tool '{tool_name}'. "
                                  f"Available tools: {available}")
                    else:
                        result = await mcp_client.call_tool(server, tool_name, arguments)
            conversation.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            yield {
                "type": "tool_call",
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
                # Explicit failure flag for the frontend — the client should
                # not re-derive failure by sniffing the "Error:" prefix.
                "failed": result.startswith(mcp_client.ERROR_PREFIX),
            }
