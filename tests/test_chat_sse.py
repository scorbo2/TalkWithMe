"""API tests for app/routers/chat.py — the SSE streaming endpoint.

The LLM layer (stream_chat / stream_chat_with_tools / chat_completion) is
replaced with stubs; the session, persistence, and persona-selection logic
are exercised for real.
"""

import uuid
from pathlib import Path

import app.config as app_config
import app.routers.chat as chat_router
from app.config import ChatRoom, ChatRoomsConfig, GeneralConfig, Persona, PersonasConfig
from app.services import builtin, llm
from tests.factories import (
    make_chatrooms,
    make_personas,
    make_settings,
    parse_sse_events,
    sse_events_by_type,
)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _stub_stream(monkeypatch, tokens):
    """Replace the plain streaming path with a canned token sequence."""

    async def fake_stream(messages):
        for token in tokens:
            yield token

    monkeypatch.setattr(chat_router, "stream_chat", fake_stream)


def _stub_stream_error_after(monkeypatch, tokens_before_error):
    async def fake_stream(messages):
        for token in tokens_before_error:
            yield token
        raise RuntimeError("boom")

    monkeypatch.setattr(chat_router, "stream_chat", fake_stream)


def _stub_tools(monkeypatch, events):
    """Replace the agentic path with canned tool-loop events."""

    async def fake_tools(messages, tools, persona):
        for event in events:
            yield event

    monkeypatch.setattr(chat_router, "stream_chat_with_tools", fake_tools)


def _capturing_tools(monkeypatch, seen: dict, events=None):
    """Stub the agentic path and record what the router passed to it.

    `seen` ends up holding {"messages", "tools", "persona"} from the call,
    for assertions on tool lists and system-prompt injection.
    """
    canned = list(events or [])

    async def fake_tools(messages, tools, persona):
        seen.update(messages=list(messages), tools=list(tools), persona=persona)
        for event in canned:
            yield event

    monkeypatch.setattr(chat_router, "stream_chat_with_tools", fake_tools)


def _tool_persona_dir(tmp_path: Path, *, name="ToolUser", memory_size=8192) -> Persona:
    """A tool-capable persona backed by a real directory (so built-in tool
    availability can be tested end-to-end)."""
    persona_dir = tmp_path / name
    persona_dir.mkdir(parents=True)
    return Persona(
        name=name,
        system_prompt="You use tools.",
        router_hints="tools",
        allow_tool_calls=True,
        memory_size=memory_size,
        persona_dir=persona_dir,
    )


def _stub_completion(monkeypatch, result: str):
    async def fake_completion(prompt, max_tokens=16):
        return result

    monkeypatch.setattr(chat_router, "chat_completion", fake_completion)


def _stub_completion_error(monkeypatch):
    async def fake_completion(prompt, max_tokens=16):
        raise RuntimeError("llm down")

    monkeypatch.setattr(chat_router, "chat_completion", fake_completion)


def _tool_call_event(**overrides):
    event = {
        "type": "tool_call",
        "tool_name": "get_time",
        "arguments": {"zone": "utc"},
        "result": "It is noon.",
        "failed": False,
    }
    event.update(overrides)
    return event


def _patch_chatrooms(monkeypatch, extra_rooms):
    """Extend the fixture chatrooms cache with the given rooms."""
    config = make_chatrooms()
    config.chat_rooms.extend(extra_rooms)
    monkeypatch.setattr(app_config, "_chatrooms_cache", config)
    return config


def _patch_personas(monkeypatch, personas_config: PersonasConfig):
    monkeypatch.setattr(app_config, "_personas_cache", personas_config)


def _patch_general(monkeypatch, **overrides):
    settings = make_settings()
    settings.general = GeneralConfig(**overrides)
    monkeypatch.setattr(app_config, "_settings_cache", settings)
    return settings


def _chat(client, monkeypatch=None, **overrides) -> list:
    """POST /api/chat with sensible defaults and return the parsed SSE events."""
    payload = {"message": "hello there", "who_answers": "Alex", "chat_room": "default"}
    payload.update(overrides)
    resp = client.post("/api/chat", json=payload)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse_events(resp.text)


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

class TestRequestValidation:
    def test_traversal_chat_room_returns_422_and_writes_nothing(
        self, client, monkeypatch, persistence_root
    ):
        # chat_room flows straight into on-disk paths (history.json + audio
        # files are created under the persistence root); an unvalidated
        # "../evil" would let the persistence layer mkdir() and write
        # outside of it. Validation must reject it before the LLM is even
        # involved and before anything is persisted:
        llm_calls = []

        async def fake_stream(messages):
            llm_calls.append(messages)
            yield "nope"

        monkeypatch.setattr(chat_router, "stream_chat", fake_stream)

        resp = client.post("/api/chat", json={
            "message": "hi", "who_answers": "Alex", "chat_room": "../evil",
        })

        assert resp.status_code == 422
        assert "Room name may only contain" in str(resp.json()["detail"])
        assert llm_calls == []
        assert not (persistence_root.parent / "evil").exists()
        assert not (persistence_root / "default" / "history.json").exists()


# ---------------------------------------------------------------------------
# Basic single-reply flow
# ---------------------------------------------------------------------------

class TestSingleReply:
    def test_event_sequence_and_payloads(self, client, monkeypatch):
        _stub_stream(monkeypatch, ["Hel", "lo"])
        events = _chat(client, message_id="my-user-uuid")

        types = [e["type"] for e in events]
        assert types == ["start", "token", "token", "done", "complete"]

        start = events[0]
        assert start["persona"] == "Alex"
        assert start["user_message_id"] == "my-user-uuid"
        assert uuid.UUID(start["message_id"])  # server-generated assistant id

        tokens = sse_events_by_type(events, "token")
        assert [t["token"] for t in tokens] == ["Hel", "lo"]
        assert all(t["persona"] == "Alex" for t in tokens)

        done = sse_events_by_type(events, "done")[0]
        assert done["text"] == "Hello"
        assert done["message_id"] == start["message_id"]  # stable across the reply

    def test_session_history_updated(self, client, monkeypatch):
        _stub_stream(monkeypatch, ["hi"])
        events = _chat(client)

        history = client.get("/api/session").json()["history"]
        # The in-memory history carries the persisted message IDs (selective
        # deletion relies on them) — the same IDs the SSE events issued.
        start = events[0]
        assert history == [
            {"role": "user", "content": "hello there", "persona": None,
             "id": start["user_message_id"]},
            {"role": "assistant", "content": "hi", "persona": "Alex",
             "id": start["message_id"]},
        ]
        assert uuid.UUID(history[0]["id"])
        assert uuid.UUID(history[1]["id"])

    def test_generated_user_message_id_when_absent(self, client, monkeypatch):
        _stub_stream(monkeypatch, ["hi"])
        events = _chat(client)  # no message_id in the request

        start = events[0]
        assert uuid.UUID(start["user_message_id"])  # valid UUID, server-generated

    def test_user_message_persisted_to_requested_room(self, client, monkeypatch, persistence_root):
        _stub_stream(monkeypatch, ["hi"])
        _chat(client, chat_room="TNG")

        from app.persistence import load_history

        messages = load_history("TNG")
        assert [m["sender"] for m in messages] == ["USER", "Alex"]
        assert messages[0]["text"] == "hello there"
        # Session room tracker follows the request.
        assert client.get("/api/session").json()["current_room"] == "TNG"


# ---------------------------------------------------------------------------
# Persona selection
# ---------------------------------------------------------------------------

class TestPersonaSelection:
    def test_explicit_persona_used_directly(self, client, monkeypatch):
        _stub_stream(monkeypatch, ["hi"])
        events = _chat(client, who_answers="Luna")
        assert events[0]["persona"] == "Luna"

    def test_random_mode_picks_from_room(self, client, monkeypatch):
        _patch_chatrooms(monkeypatch, [ChatRoom(name="Solo", persona_names=["Luna"])])
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="random", chat_room="Solo")

        # "Solo" only contains Luna, so "random" is deterministic here.
        assert events[0]["persona"] == "Luna"

    def test_explicit_persona_not_in_room_falls_back_to_random(self, client, monkeypatch):
        _patch_chatrooms(monkeypatch, [ChatRoom(name="Solo", persona_names=["Luna"])])
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="Alex", chat_room="Solo")

        assert events[0]["persona"] == "Luna"

    def test_unknown_room_falls_back_to_all_personas(self, client, monkeypatch):
        _stub_stream(monkeypatch, ["hi"])
        events = _chat(client, chat_room="Nowhere")  # not in chatrooms.yaml
        assert events[0]["persona"] == "Alex"  # explicit name still honored

    def test_room_with_no_personas_emits_error_not_reply(self, client, monkeypatch):
        _patch_chatrooms(monkeypatch, [ChatRoom(name="Empty", persona_names=[])])

        def fail(*a, **kw):
            raise AssertionError("LLM must not be called when no persona is eligible")

        monkeypatch.setattr(chat_router, "stream_chat", fail)

        events = _chat(client, chat_room="Empty")

        types = [e["type"] for e in events]
        assert types == ["error", "complete"]
        assert "No eligible personas" in events[0]["message"]
        # The user message must not have been recorded either.
        assert client.get("/api/session").json()["history"] == []

    # -- router mode ---------------------------------------------------------

    def test_router_mode_uses_llm_choice(self, client, monkeypatch):
        _stub_completion(monkeypatch, "Luna")
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="router")

        assert events[0]["persona"] == "Luna"

    def test_router_mode_strips_quotes_and_whitespace(self, client, monkeypatch):
        _stub_completion(monkeypatch, '  "Alex"  ')
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="router")

        assert events[0]["persona"] == "Alex"

    def test_router_mode_invalid_choice_falls_back_to_random(self, client, monkeypatch):
        _patch_chatrooms(monkeypatch, [ChatRoom(name="Solo", persona_names=["Luna"])])
        _stub_completion(monkeypatch, "Q")  # not an eligible persona
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="router", chat_room="Solo")

        assert events[0]["persona"] == "Luna"

    def test_router_mode_llm_failure_falls_back_to_random(self, client, monkeypatch):
        _patch_chatrooms(monkeypatch, [ChatRoom(name="Solo", persona_names=["Luna"])])
        _stub_completion_error(monkeypatch)
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="router", chat_room="Solo")

        assert events[0]["persona"] == "Luna"


# ---------------------------------------------------------------------------
# Responder planning (_plan_responders — pure function, no endpoint)
# ---------------------------------------------------------------------------

class TestPlanResponders:
    def test_single_reply_plans_only_the_first_persona(self):
        plan = chat_router._plan_responders(["Alex", "Luna"], "Alex", 1)

        assert plan == ["Alex"]

    def test_cap_larger_than_pool_plans_every_eligible_exactly_once(self):
        plan = chat_router._plan_responders(["Alex", "Luna"], "Luna", 12)

        assert plan[0] == "Luna"  # the configured pick keeps slot 0
        assert sorted(plan) == ["Alex", "Luna"]

    def test_planned_personas_are_always_distinct_and_eligible(self):
        # Invariant sweep over pools/counts: the plan must only ever
        # contain eligible, non-repeating names, with `first` in slot 0
        # and its length capped at min(count, pool size).
        pool = ["Alex", "Luna", "Cmdr", "Bella"]
        for first in pool:
            for count in range(1, 10):
                plan = chat_router._plan_responders(pool, first, count)

                assert plan[0] == first
                assert len(plan) == min(count, len(pool))
                assert len(set(plan)) == len(plan)  # no repeats
                assert set(plan) <= set(pool)       # never an outsider

    def test_zero_or_negative_count_plans_nothing(self):
        assert chat_router._plan_responders(["Alex"], "Alex", 0) == []
        assert chat_router._plan_responders(["Alex"], "Alex", -3) == []

    def test_first_outside_pool_stays_slot_zero_and_well_formed(self):
        # Defensive: _pick_persona() guarantees `first` is eligible, but
        # the plan must stay well-formed if a future caller breaks that
        # contract (no crash, no duplicate of `first`).
        plan = chat_router._plan_responders(["Alex", "Luna"], "Nobody", 2)

        assert plan[0] == "Nobody"
        assert plan[1] in ("Alex", "Luna")


# ---------------------------------------------------------------------------
# Multi-persona replies
# ---------------------------------------------------------------------------

class TestMultiPersonaReplies:
    def test_two_replies_from_two_personas(self, client, monkeypatch):
        _patch_general(monkeypatch, max_persona_replies=2)
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="Alex", chat_room="TNG")

        starts = sse_events_by_type(events, "start")
        dones = sse_events_by_type(events, "done")
        assert [e["persona"] for e in starts] == ["Alex", "Luna"]
        assert [e["persona"] for e in dones] == ["Alex", "Luna"]
        assert [e["type"] for e in events][-1] == "complete"
        # Each reply gets its own assistant message id.
        assert len({e["message_id"] for e in starts}) == 2

    def test_replies_capped_at_eligible_count(self, client, monkeypatch):
        _patch_general(monkeypatch, max_persona_replies=12)
        _patch_chatrooms(monkeypatch, [ChatRoom(name="Solo", persona_names=["Luna"])])
        _stub_stream(monkeypatch, ["hi"])

        events = _chat(client, who_answers="random", chat_room="Solo")

        starts = sse_events_by_type(events, "start")
        assert [e["persona"] for e in starts] == ["Luna"]  # only one persona available

    def test_second_reply_sees_first_reply_in_history(self, client, monkeypatch):
        _patch_general(monkeypatch, max_persona_replies=2)
        seen_contexts = []

        async def capturing_stream(messages):
            seen_contexts.append(
                [(m["role"], m["content"]) for m in messages if m["role"] != "system"])
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)

        _chat(client, who_answers="Alex", chat_room="TNG")

        assert len(seen_contexts) == 2
        # The first reply only saw the user's message...
        assert seen_contexts[0] == [("user", "hello there")]
        # ...the second also saw Alex's answer, reformatted as a prefixed
        # "user" turn (another persona's words must not look like its own).
        assert seen_contexts[1] == [("user", "hello there"), ("user", "[Alex]: hi")]


# ---------------------------------------------------------------------------
# Echo chamber
# ---------------------------------------------------------------------------

class TestEchoChamber:
    def test_echoes_user_message_verbatim_without_llm(self, client, monkeypatch):
        _patch_chatrooms(monkeypatch,
                          [ChatRoom(name="Echo", persona_names=["Alex"], echo_chamber=True)])
        _patch_general(monkeypatch, max_persona_replies=4)

        def fail(*a, **kw):
            raise AssertionError("echo chamber must bypass the LLM entirely")

        monkeypatch.setattr(chat_router, "stream_chat", fail)

        events = _chat(client, who_answers="Alex", chat_room="Echo")

        tokens = sse_events_by_type(events, "token")
        assert [t["token"] for t in tokens] == ["hello there"]
        done = sse_events_by_type(events, "done")[0]
        assert done["text"] == "hello there"
        # Only one echo because the room has only one persona — the count
        # is capped at the eligible count, not at 1 (see the tests below
        # for multi-persona echo rooms).
        assert [e["persona"] for e in sse_events_by_type(events, "start")] == ["Alex"]

    def test_echo_enabled_on_default_room_bypasses_llm(self, client, monkeypatch):
        # The default room has no record in chat_rooms; its flag lives in
        # the config (default_echo_chamber). The chat flow must read it
        # from there, not only from per-room records.
        config = make_chatrooms()
        config.default_echo_chamber = True
        monkeypatch.setattr(app_config, "_chatrooms_cache", config)
        # max_persona_replies=1 keeps this test focused on the flag — the
        # reply-COUNT behavior of the echo chamber has its own tests below.
        _patch_general(monkeypatch, max_persona_replies=1)

        def fail(*a, **kw):
            raise AssertionError("echo chamber must bypass the LLM entirely")

        monkeypatch.setattr(chat_router, "stream_chat", fail)

        events = _chat(client, who_answers="Alex", chat_room="default")

        tokens = sse_events_by_type(events, "token")
        assert [t["token"] for t in tokens] == ["hello there"]
        done = sse_events_by_type(events, "done")[0]
        assert done["text"] == "hello there"
        assert [e["persona"] for e in sse_events_by_type(events, "start")] == ["Alex"]

    def test_echo_respects_max_persona_replies(self, client, monkeypatch):
        # The echo chamber no longer caps replies at 1: with two personas
        # in the room and max_persona_replies=2, BOTH echo the message
        # verbatim — the explicit pick first, then the remaining pool.
        _patch_chatrooms(monkeypatch,
                          [ChatRoom(name="Echo", persona_names=["Alex", "Luna"],
                                    echo_chamber=True)])
        _patch_general(monkeypatch, max_persona_replies=2)

        def fail(*a, **kw):
            raise AssertionError("echo chamber must bypass the LLM entirely")

        monkeypatch.setattr(chat_router, "stream_chat", fail)

        events = _chat(client, who_answers="Alex", chat_room="Echo")

        starts = sse_events_by_type(events, "start")
        assert [e["persona"] for e in starts] == ["Alex", "Luna"]
        # Every reply is the user's message, verbatim, with no LLM tokens.
        assert [t["token"] for t in sse_events_by_type(events, "token")] == \
            ["hello there", "hello there"]
        assert [e["text"] for e in sse_events_by_type(events, "done")] == \
            ["hello there", "hello there"]
        # Each echo gets its own assistant message id.
        assert len({e["message_id"] for e in starts}) == 2

    def test_echo_capped_at_eligible_count_without_repeats(self, client, monkeypatch):
        # max_persona_replies above the room's persona count must not
        # duplicate personas: three personas in the room, max=12, and each
        # persona echoes exactly once.
        personas_config = make_personas()
        personas_config.personas.append(
            Persona(name="Cmdr", system_prompt="You are Cmdr.",
                    router_hints="commands"))
        _patch_personas(monkeypatch, personas_config)
        _patch_chatrooms(monkeypatch,
                          [ChatRoom(name="Echo",
                                    persona_names=["Alex", "Luna", "Cmdr"],
                                    echo_chamber=True)])
        _patch_general(monkeypatch, max_persona_replies=12)

        def fail(*a, **kw):
            raise AssertionError("echo chamber must bypass the LLM entirely")

        monkeypatch.setattr(chat_router, "stream_chat", fail)

        events = _chat(client, who_answers="Alex", chat_room="Echo")

        starts = sse_events_by_type(events, "start")
        # Alex first (explicit pick); the other two come from the remaining
        # pool in random order — each exactly once.
        assert starts[0]["persona"] == "Alex"
        assert sorted(e["persona"] for e in starts) == ["Alex", "Cmdr", "Luna"]
        assert [t["token"] for t in sse_events_by_type(events, "token")] == \
            ["hello there"] * 3
        assert [e["text"] for e in sse_events_by_type(events, "done")] == \
            ["hello there"] * 3

    def test_default_room_with_flag_off_streams_from_llm(self, client, monkeypatch):
        # Regression guard: a config with the flag explicitly False (how
        # pre-flag files load) must NOT echo — the normal LLM path runs.
        config = make_chatrooms()
        config.default_echo_chamber = False
        monkeypatch.setattr(app_config, "_chatrooms_cache", config)
        _stub_stream(monkeypatch, ["normal reply"])

        events = _chat(client, who_answers="Alex", chat_room="default")

        tokens = sse_events_by_type(events, "token")
        assert [t["token"] for t in tokens] == ["normal reply"]


# ---------------------------------------------------------------------------
# Tool calls (agentic persona)
# ---------------------------------------------------------------------------

class TestToolCalls:
    def _tool_persona_cache(self, monkeypatch):
        config = make_personas()
        config.personas.append(
            Persona(name="ToolUser", system_prompt="You use tools.",
                    router_hints="tools", allow_tool_calls=True))
        _patch_personas(monkeypatch, config)

    def test_tool_call_event_emitted_when_enabled(self, client, monkeypatch):
        self._tool_persona_cache(monkeypatch)
        _stub_tools(monkeypatch, [
            _tool_call_event(),
            {"type": "token", "token": "It is "},
            {"type": "token", "token": "noon."},
        ])

        events = _chat(client, who_answers="ToolUser")

        tool_events = sse_events_by_type(events, "tool_call")
        assert len(tool_events) == 1
        tool_event = tool_events[0]
        assert tool_event["persona"] == "ToolUser"
        assert tool_event["tool_name"] == "get_time"
        assert tool_event["arguments"] == {"zone": "utc"}
        assert tool_event["result"] == "It is noon."
        assert tool_event["failed"] is False

        done = sse_events_by_type(events, "done")[0]
        assert done["text"] == "It is noon."

    def test_failed_tool_call_flag_survives_to_sse(self, client, monkeypatch):
        self._tool_persona_cache(monkeypatch)
        _stub_tools(monkeypatch, [
            _tool_call_event(result="Error: connection refused", failed=True),
            {"type": "token", "token": "sorry"},
        ])

        events = _chat(client, who_answers="ToolUser")

        tool_event = sse_events_by_type(events, "tool_call")[0]
        assert tool_event["failed"] is True
        assert tool_event["result"] == "Error: connection refused"

    def test_tool_events_suppressed_when_show_tool_calls_false(self, client, monkeypatch):
        self._tool_persona_cache(monkeypatch)
        _patch_general(monkeypatch, show_tool_calls=False)
        _stub_tools(monkeypatch, [
            _tool_call_event(),
            {"type": "token", "token": "noon"},
        ])

        events = _chat(client, who_answers="ToolUser")

        assert sse_events_by_type(events, "tool_call") == []
        # The reply itself still streams and completes.
        assert sse_events_by_type(events, "done")[0]["text"] == "noon"


# ---------------------------------------------------------------------------
# Per-server persona access control (issue #138)
# ---------------------------------------------------------------------------

class TestAllowedPersonasFiltering:
    """The agentic loop must receive only the tools from servers that
    allow the responding persona. The tool registry is seeded directly
    (no discovery); the router's filtering path is exercised for real."""

    @staticmethod
    def _openai_tool(name: str) -> dict:
        return {
            "type": "function",
            "function": {"name": name, "description": f"desc {name}",
                         "parameters": {"type": "object", "properties": {}}},
        }

    @staticmethod
    def _seed_registry(allowed_for: list):
        """An open server ('open_tool') plus a restricted server
        ('restricted_tool') whose allow-list is ``allowed_for``."""
        from app.services import tool_registry
        from tests.factories import make_mcp_server

        open_server = make_mcp_server("open", "http://open.local")
        restricted_server = make_mcp_server(
            "restricted", "http://restricted.local", allowed_personas=allowed_for,
        )
        open_tool = TestAllowedPersonasFiltering._openai_tool("open_tool")
        restricted_tool = TestAllowedPersonasFiltering._openai_tool("restricted_tool")
        tool_registry._tool_cache.update({
            "open": [open_tool],
            "restricted": [restricted_tool],
        })
        tool_registry._server_map.update({
            "open_tool": open_server,
            "restricted_tool": restricted_server,
        })

    @staticmethod
    def _tool_user_cache(monkeypatch, tmp_path):
        config = make_personas()
        config.personas.append(_tool_persona_dir(tmp_path))
        _patch_personas(monkeypatch, config)

    def test_unlisted_persona_receives_only_open_tools(self, client, monkeypatch, tmp_path):
        self._tool_user_cache(monkeypatch, tmp_path)
        self._seed_registry(allowed_for=["Luna"])  # ToolUser is NOT in the list

        seen = {}
        _capturing_tools(monkeypatch, seen, events=[{"type": "token", "token": "hi"}])

        _chat(client, who_answers="ToolUser")

        names = {t["function"]["name"] for t in seen["tools"]}
        assert names == {"open_tool", builtin.ADD_MEMORY_NAME}

    def test_listed_persona_receives_open_and_restricted_tools(self, client, monkeypatch, tmp_path):
        self._tool_user_cache(monkeypatch, tmp_path)
        self._seed_registry(allowed_for=["ToolUser"])

        seen = {}
        _capturing_tools(monkeypatch, seen, events=[{"type": "token", "token": "hi"}])

        _chat(client, who_answers="ToolUser")

        names = {t["function"]["name"] for t in seen["tools"]}
        assert names == {"open_tool", "restricted_tool", builtin.ADD_MEMORY_NAME}


# ---------------------------------------------------------------------------
# Persona memories (docs/feature_persona_memory.md)
# ---------------------------------------------------------------------------

class TestPersonaMemory:
    """The memory feature at the chat boundary: saved memories are
    injected into the system prompt, and tool-capable personas get the
    built-in add_memory tool offered."""

    # -- _system_prompt_with_memories (unit) ---------------------------------

    @staticmethod
    def _persona_with_memories(tmp_path, **persona_kwargs) -> Persona:
        persona_dir = tmp_path / "Alex"
        persona_dir.mkdir(parents=True)
        (persona_dir / "memories.txt").write_text("The user likes tea.\n")
        return Persona(name="Alex", system_prompt="You are Alex.",
                       persona_dir=persona_dir, **persona_kwargs)

    def test_memories_appended_to_system_prompt(self, tmp_path):
        result = chat_router._system_prompt_with_memories(
            self._persona_with_memories(tmp_path), make_settings(),
        )
        assert result == (
            "You are Alex.\n\nYou have the following memories related to the user:\n"
            "The user likes tea.\n"
        )

    def test_no_injection_when_global_flag_off(self, tmp_path):
        settings = make_settings(general=GeneralConfig(enable_persona_memories=False))
        result = chat_router._system_prompt_with_memories(
            self._persona_with_memories(tmp_path), settings,
        )
        assert result == "You are Alex."

    def test_no_injection_when_memory_size_zero(self, tmp_path):
        result = chat_router._system_prompt_with_memories(
            self._persona_with_memories(tmp_path, memory_size=0), make_settings(),
        )
        assert result == "You are Alex."

    def test_no_injection_when_persona_has_no_directory(self, tmp_path):
        result = chat_router._system_prompt_with_memories(
            Persona(name="Alex", system_prompt="You are Alex."), make_settings(),
        )
        assert result == "You are Alex."

    def test_no_injection_when_memories_file_absent(self, tmp_path):
        persona_dir = tmp_path / "Alex"
        persona_dir.mkdir(parents=True)
        result = chat_router._system_prompt_with_memories(
            Persona(name="Alex", system_prompt="You are Alex.",
                    persona_dir=persona_dir), make_settings(),
        )
        assert result == "You are Alex."

    def test_no_injection_when_memories_file_blank(self, tmp_path):
        persona_dir = tmp_path / "Alex"
        persona_dir.mkdir(parents=True)
        (persona_dir / "memories.txt").write_text("  \n")
        result = chat_router._system_prompt_with_memories(
            Persona(name="Alex", system_prompt="You are Alex.",
                    persona_dir=persona_dir), make_settings(),
        )
        assert result == "You are Alex."

    # -- budget enforcement on the read path ----------------------------------

    @staticmethod
    def _persona_with_budget(tmp_path, memory_size: int) -> Persona:
        """A persona with a real directory and a (small) memory budget,
        no memories file yet — the tests write that themselves."""
        persona_dir = tmp_path / "Alex"
        persona_dir.mkdir(parents=True)
        return Persona(name="Alex", system_prompt="You are Alex.",
                       persona_dir=persona_dir, memory_size=memory_size)

    def test_over_limit_memories_purged_oldest_first_on_read(self, tmp_path):
        persona = self._persona_with_budget(tmp_path, memory_size=10)
        memories_file = persona.persona_dir / "memories.txt"
        # 15 bytes against a 10-byte budget: the oldest-first purge leaves
        # only the newest memory — both in the injected prompt and on disk.
        memories_file.write_text("aaaa\nbbbb\ncccc\n")

        result = chat_router._system_prompt_with_memories(persona, make_settings())

        assert result == (
            "You are Alex.\n\nYou have the following memories related to the user:\n"
            "cccc\n"
        )
        assert memories_file.read_text() == "cccc\n"

    def test_within_budget_memories_left_untouched_on_read(self, tmp_path):
        persona = self._persona_with_budget(tmp_path, memory_size=10)
        memories_file = persona.persona_dir / "memories.txt"
        # Exactly at the limit: the read path must not rewrite the file.
        memories_file.write_text("aaaa\nbbbb\n")

        result = chat_router._system_prompt_with_memories(persona, make_settings())

        assert result == (
            "You are Alex.\n\nYou have the following memories related to the user:\n"
            "aaaa\nbbbb\n"
        )
        assert memories_file.read_text() == "aaaa\nbbbb\n"

    def test_single_memory_exceeding_budget_deletes_file_on_read(self, tmp_path):
        persona = self._persona_with_budget(tmp_path, memory_size=10)
        memories_file = persona.persona_dir / "memories.txt"
        # One 11-byte memory against a 10-byte budget: nothing can survive,
        # so the file is deleted — same semantics as the write path.
        memories_file.write_text("aaaaaaaaaa\n")

        result = chat_router._system_prompt_with_memories(persona, make_settings())

        assert result == "You are Alex."
        assert not memories_file.exists()

    # -- integration: injection reaches the LLM -------------------------------

    def test_injected_memories_reach_the_llm_payload(self, client, monkeypatch, tmp_path):
        alex_dir = tmp_path / "Alex"
        alex_dir.mkdir(parents=True)
        (alex_dir / "memories.txt").write_text("The user likes tea.\n")
        config = make_personas()
        config.personas[0] = Persona(
            name="Alex",
            description="A friendly assistant",
            system_prompt="You are Alex, a friendly assistant.",
            router_hints="general questions",
            persona_dir=alex_dir,
        )
        _patch_personas(monkeypatch, config)

        seen = []

        async def capturing_stream(messages):
            seen.append(list(messages))
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)

        _chat(client, who_answers="Alex")

        assert len(seen) == 1
        system_message = seen[0][0]
        assert system_message["role"] == "system"
        assert system_message["content"].endswith(
            "You have the following memories related to the user:\n"
            "The user likes tea.\n"
        )

    def test_external_over_limit_memories_purged_before_llm_payload(self, client, monkeypatch, tmp_path):
        # The scenario the read-path enforcement exists for: an external
        # process inflates memories.txt past the persona's budget while the
        # app runs; the next chat must purge it, not inject it verbatim.
        alex_dir = tmp_path / "Alex"
        alex_dir.mkdir(parents=True)
        memories_file = alex_dir / "memories.txt"
        memories_file.write_text("aaaa\nbbbb\ncccc\n")  # 15 bytes, budget is 10
        config = make_personas()
        config.personas[0] = Persona(
            name="Alex",
            description="A friendly assistant",
            system_prompt="You are Alex, a friendly assistant.",
            router_hints="general questions",
            persona_dir=alex_dir,
            memory_size=10,
        )
        _patch_personas(monkeypatch, config)

        seen = []

        async def capturing_stream(messages):
            seen.append(list(messages))
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)

        _chat(client, who_answers="Alex")

        assert len(seen) == 1
        system_message = seen[0][0]
        assert system_message["role"] == "system"
        assert system_message["content"].endswith(
            "You have the following memories related to the user:\ncccc\n"
        )
        # The on-disk file is repaired too, so subsequent reads stay clean.
        assert memories_file.read_text() == "cccc\n"

    # -- integration: add_memory is offered to the LLM ------------------------

    def _chat_with_captured_tools(self, client, monkeypatch, tmp_path, **general):
        config = make_personas()
        config.personas.append(_tool_persona_dir(tmp_path))
        _patch_personas(monkeypatch, config)
        if general:
            _patch_general(monkeypatch, **general)
        seen = {}
        _capturing_tools(monkeypatch, seen, events=[{"type": "token", "token": "hi"}])
        _chat(client, who_answers="ToolUser")
        return seen

    def test_add_memory_offered_to_tool_persona_by_default(self, client, monkeypatch, tmp_path):
        seen = self._chat_with_captured_tools(client, monkeypatch, tmp_path)
        tool_names = [t["function"]["name"] for t in seen["tools"]]
        assert builtin.ADD_MEMORY_NAME in tool_names
        # The persona is forwarded so built-ins can run against its directory.
        assert seen["persona"].name == "ToolUser"

    def test_add_memory_not_offered_when_global_flag_off(self, client, monkeypatch, tmp_path):
        seen = self._chat_with_captured_tools(
            client, monkeypatch, tmp_path, enable_persona_memories=False,
        )
        tool_names = [t["function"]["name"] for t in seen["tools"]]
        assert builtin.ADD_MEMORY_NAME not in tool_names

    def test_add_memory_not_offered_when_memory_size_zero(self, client, monkeypatch, tmp_path):
        config = make_personas()
        config.personas.append(_tool_persona_dir(tmp_path, memory_size=0))
        _patch_personas(monkeypatch, config)
        seen = {}
        _capturing_tools(monkeypatch, seen, events=[{"type": "token", "token": "hi"}])
        _chat(client, who_answers="ToolUser")
        tool_names = [t["function"]["name"] for t in seen["tools"]]
        assert builtin.ADD_MEMORY_NAME not in tool_names

    def test_non_tool_persona_gets_no_builtins(self, client, monkeypatch, tmp_path):
        # A non-tool persona never reaches stream_chat_with_tools, so the
        # plain stream path must not be offered anything tool-shaped.
        seen = []

        async def capturing_stream(messages):
            seen.append(list(messages))
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)

        def fail(*a, **kw):
            raise AssertionError("a non-tool persona must not use the agentic path")

        monkeypatch.setattr(chat_router, "stream_chat_with_tools", fail)

        _chat(client, who_answers="Alex")

        # Exactly one stream call, via the plain (non-agentic) path.
        assert len(seen) == 1
        assert seen[0][0]["role"] == "system"


# ---------------------------------------------------------------------------
# Global system prompt (settings.general.global_system_prompt)
# ---------------------------------------------------------------------------

class TestGlobalSystemPrompt:
    """general.global_system_prompt is appended to the END of every
    persona's system prompt — the one place for rules that used to be
    copy-pasted into each persona (e.g. "no markdown for TTS")."""

    # -- _with_global_system_prompt (unit) -----------------------------------

    def test_no_append_when_prompt_is_empty(self):
        result = chat_router._with_global_system_prompt(
            "You are Alex.", make_settings(),
        )
        assert result == "You are Alex."

    def test_no_append_when_prompt_is_whitespace_only(self):
        settings = make_settings(general=GeneralConfig(global_system_prompt="   \n  "))
        result = chat_router._with_global_system_prompt("You are Alex.", settings)
        assert result == "You are Alex."

    def test_prompt_appended_after_blank_line(self):
        settings = make_settings(general=GeneralConfig(
            global_system_prompt="Do not use markdown."))
        result = chat_router._with_global_system_prompt("You are Alex.", settings)
        assert result == "You are Alex.\n\nDo not use markdown."

    def test_surrounding_whitespace_is_stripped_from_appended_prompt(self):
        # The settings textarea can pick up stray leading/trailing
        # whitespace; the prompt must not (a trailing newline in the
        # system prompt is exactly the kind of thing that makes the
        # next section look like a continuation of the prompt body).
        settings = make_settings(general=GeneralConfig(
            global_system_prompt="  Do not use markdown.  \n"))
        result = chat_router._with_global_system_prompt("You are Alex.", settings)
        assert result == "You are Alex.\n\nDo not use markdown."

    def test_multiline_global_prompt_preserves_inner_newlines(self):
        settings = make_settings(general=GeneralConfig(
            global_system_prompt="Line one.\nLine two."))
        result = chat_router._with_global_system_prompt("You are Alex.", settings)
        assert result == "You are Alex.\n\nLine one.\nLine two."

    def test_base_prompt_trailing_newline_does_not_double_the_separator(self):
        # A persona prompt ending in a newline (or the memories block's own
        # trailing newline) must yield EXACTLY one blank line, not two.
        settings = make_settings(general=GeneralConfig(
            global_system_prompt="Do not use markdown."))
        result = chat_router._with_global_system_prompt("You are Alex.\n", settings)
        assert result == "You are Alex.\n\nDo not use markdown."

    # -- ordering: memories first, global prompt last -------------------------

    def test_global_prompt_appended_after_injected_memories(self, tmp_path):
        # The global rules must sit at the very end of the prompt — after
        # the persona prompt AND any injected memories — so they win when
        # a persona-level instruction disagrees (a persona that likes
        # markdown vs. a global "plain text only").
        persona_dir = tmp_path / "Alex"
        persona_dir.mkdir(parents=True)
        (persona_dir / "memories.txt").write_text("The user likes tea.\n")
        persona = Persona(name="Alex", system_prompt="You are Alex.",
                          persona_dir=persona_dir)
        settings = make_settings(general=GeneralConfig(
            global_system_prompt="Plain text only."))

        result = chat_router._with_global_system_prompt(
            chat_router._system_prompt_with_memories(persona, settings), settings,
        )
        assert result == (
            "You are Alex.\n\nYou have the following memories related to the user:\n"
            "The user likes tea.\n\nPlain text only."
        )

    # -- integration: the append reaches the LLM payload ----------------------

    def test_global_prompt_reaches_llm_payload(self, client, monkeypatch):
        _patch_general(monkeypatch, global_system_prompt="No markdown, plain text only.")

        seen = []

        async def capturing_stream(messages):
            seen.append(list(messages))
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)

        _chat(client, who_answers="Alex")

        assert len(seen) == 1
        system_message = seen[0][0]
        assert system_message["role"] == "system"
        assert system_message["content"].endswith("\n\nNo markdown, plain text only.")

    def test_blank_global_prompt_leaves_llm_payload_untouched(self, client, monkeypatch):
        _patch_general(monkeypatch, global_system_prompt="   ")

        seen = []

        async def capturing_stream(messages):
            seen.append(list(messages))
            yield "hi"

        monkeypatch.setattr(chat_router, "stream_chat", capturing_stream)

        _chat(client, who_answers="Alex")

        system_message = seen[0][0]
        assert system_message["content"] == "You are Alex, a friendly assistant."

    def test_global_prompt_reaches_agentic_path_payload(self, client, monkeypatch, tmp_path):
        # Tool-capable personas go through stream_chat_with_tools, not
        # stream_chat — the append must apply to that path too.
        config = make_personas()
        config.personas.append(_tool_persona_dir(tmp_path))
        _patch_personas(monkeypatch, config)
        _patch_general(monkeypatch, global_system_prompt="Plain text only.")

        seen = {}
        _capturing_tools(monkeypatch, seen, events=[{"type": "token", "token": "hi"}])

        _chat(client, who_answers="ToolUser")

        system_message = seen["messages"][0]
        assert system_message["role"] == "system"
        assert system_message["content"].endswith("\n\nPlain text only.")


# ---------------------------------------------------------------------------
# LLM failures mid-stream
# ---------------------------------------------------------------------------

class TestStreamErrors:
    def test_error_after_partial_tokens_terminates_stream(self, client, monkeypatch):
        _stub_stream_error_after(monkeypatch, ["par"])

        events = _chat(client)

        types = [e["type"] for e in events]
        assert types == ["start", "token", "error"]
        assert events[-1]["message"] == "boom"
        # No done/complete after a mid-stream failure.
        assert "done" not in types
        assert "complete" not in types

    def test_partial_reply_is_not_persisted(self, client, monkeypatch):
        _stub_stream_error_after(monkeypatch, ["par"])
        _chat(client)

        from app.persistence import load_history

        messages = load_history("default")
        # Only the user message landed; the assistant row never did.
        assert [m["sender"] for m in messages] == ["USER"]


# ---------------------------------------------------------------------------
# Double aborts (issue #128)
# ---------------------------------------------------------------------------

class TestDoubleAbort:
    """A stream the server aborts twice in a row (in-band error object,
    plus its single retry) raises LLMStreamAborted from the LLM layer.
    The router must surface it as a visible error event and skip
    persistence — the pre-fix failure mode was a SILENTLY PERSISTED
    empty assistant row: a blank bubble for the user, and an empty
    "[Name]: " prefix poisoning every subsequent persona's context."""

    @staticmethod
    def _tool_persona_cache(monkeypatch):
        config = make_personas()
        config.personas.append(
            Persona(name="ToolUser", system_prompt="You use tools.",
                    router_hints="tools", allow_tool_calls=True))
        _patch_personas(monkeypatch, config)

    @staticmethod
    def _aborting_tools(monkeypatch, events_before_raise):
        async def fake_tools(messages, tools, persona):
            for event in events_before_raise:
                yield event
            raise llm.LLMStreamAborted("aborted twice in a row")

        monkeypatch.setattr(chat_router, "stream_chat_with_tools", fake_tools)

    @staticmethod
    def _aborting_plain(monkeypatch):
        async def fake_stream(messages):
            # The empty yield loop makes this an async generator (the
            # router consumes it via `async for`); the raise fires on the
            # first __anext__, before any token.
            for token in ():
                yield token
            raise llm.LLMStreamAborted("aborted twice in a row")

        monkeypatch.setattr(chat_router, "stream_chat", fake_stream)

    def test_tool_loop_abort_emits_error_not_done(self, client, monkeypatch):
        self._tool_persona_cache(monkeypatch)
        self._aborting_tools(monkeypatch, [])

        events = _chat(client, who_answers="ToolUser")

        types = [e["type"] for e in events]
        assert types == ["start", "error"]
        assert "aborted twice" in events[-1]["message"]
        # No done/complete after an abort — same contract as any
        # mid-stream error (see TestStreamErrors).
        assert "done" not in types
        assert "complete" not in types

    def test_tool_loop_abort_after_tool_chip_still_emits_the_chip(self, client, monkeypatch):
        # The tool call (and its side effects) already happened before
        # the final round aborted: the chip is real and must be shown,
        # then the error.
        self._tool_persona_cache(monkeypatch)
        self._aborting_tools(monkeypatch, [_tool_call_event()])

        events = _chat(client, who_answers="ToolUser")

        types = [e["type"] for e in events]
        assert types == ["start", "tool_call", "error"]

    def test_tool_loop_abort_persists_no_empty_reply(self, client, monkeypatch):
        self._tool_persona_cache(monkeypatch)
        self._aborting_tools(monkeypatch, [])
        _chat(client, who_answers="ToolUser")

        from app.persistence import load_history

        messages = load_history("default")
        # Only the user message landed; no empty assistant row.
        assert [m["sender"] for m in messages] == ["USER"]

    def test_plain_path_abort_emits_error_not_done(self, client, monkeypatch):
        self._aborting_plain(monkeypatch)

        events = _chat(client)

        types = [e["type"] for e in events]
        assert types == ["start", "error"]
        assert "done" not in types
        assert "complete" not in types

    def test_plain_path_abort_persists_no_empty_reply(self, client, monkeypatch):
        self._aborting_plain(monkeypatch)
        _chat(client)

        from app.persistence import load_history

        messages = load_history("default")
        # Only the user message landed; no empty assistant row.
        assert [m["sender"] for m in messages] == ["USER"]
