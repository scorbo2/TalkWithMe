"""Tests for app/services/builtin.py — the built-in tool registry and the
add_memory tool (docs/feature_persona_memory.md).

Unit tests: personas are built directly (no config cache, no app state)
and memory files live under pytest's tmp_path. No network involved. The
module registers its tools at import time and keeps no mutable state of
its own, so there is nothing to reset between tests — except where a test
registers a throwaway tool, in which case it patches the registry dict so
the throwaway never leaks into the other tests.
"""

import pytest

from app.config import AppSettings, GeneralConfig, Persona
from app.services import builtin
from app.services.builtin import (
    ADD_MEMORY_NAME,
    ADD_MEMORY_SPEC,
    call_builtin_tool,
    get_builtin_tools,
    get_builtin_tools_for,
    is_builtin_tool,
    register_builtin_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persona(tmp_path, *, memory_size=8192, name="Mindy") -> Persona:
    """A persona backed by a real (empty) directory under tmp_path."""
    persona_dir = tmp_path / name
    persona_dir.mkdir(parents=True)
    return Persona(
        name=name,
        system_prompt="You are Mindy.",
        persona_dir=persona_dir,
        memory_size=memory_size,
    )


def _dirless_persona() -> Persona:
    """A persona assembled outside the directory scan (persona_dir is None)."""
    return Persona(name="Mindy", system_prompt="You are Mindy.")


def _settings(**general) -> AppSettings:
    return AppSettings(general=GeneralConfig(**general))


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_add_memory_is_registered(self):
        assert is_builtin_tool(ADD_MEMORY_NAME)
        assert [t["function"]["name"] for t in get_builtin_tools()] == [ADD_MEMORY_NAME]

    def test_spec_is_openai_function_shape(self):
        assert ADD_MEMORY_SPEC["type"] == "function"
        function = ADD_MEMORY_SPEC["function"]
        assert function["name"] == ADD_MEMORY_NAME
        assert function["parameters"]["type"] == "object"
        assert function["parameters"]["required"] == ["memory"]
        assert function["parameters"]["properties"]["memory"]["type"] == "string"

    def test_registering_a_duplicate_name_raises(self, monkeypatch):
        # Patch the dict first so the attempt below can't poison the real
        # registry (registration refuses, but be safe).
        monkeypatch.setattr(builtin, "_BUILTIN_TOOLS", dict(builtin._BUILTIN_TOOLS))
        with pytest.raises(ValueError, match="already registered"):
            register_builtin_tool(
                ADD_MEMORY_NAME, {"type": "function"}, lambda p, a: "x",
            )

    def test_call_unknown_tool_lists_available_builtins(self):
        result = call_builtin_tool(_dirless_persona(), "nope", {})
        assert result.startswith("Error: unknown tool 'nope'")
        assert "add_memory" in result  # the model is told what it CAN call

    def test_raising_handler_degrades_to_error_string(self, monkeypatch):
        """A handler bug must surface as an 'Error:' result, not an
        exception: call_builtin_tool() feeds the return value straight
        back to the LLM, and an exception would kill the reply stream."""
        monkeypatch.setattr(builtin, "_BUILTIN_TOOLS", dict(builtin._BUILTIN_TOOLS))

        def boom(persona, arguments):
            raise RuntimeError("kaboom")

        register_builtin_tool("boom", {"type": "function"}, boom)
        result = call_builtin_tool(_dirless_persona(), "boom", {})
        assert result.startswith("Error: the built-in tool 'boom' failed unexpectedly")
        assert "kaboom" in result

    def test_non_string_result_degrades_to_error_string(self, monkeypatch):
        monkeypatch.setattr(builtin, "_BUILTIN_TOOLS", dict(builtin._BUILTIN_TOOLS))
        register_builtin_tool("nasty", {"type": "function"}, lambda p, a: 42)
        result = call_builtin_tool(_dirless_persona(), "nasty", {})
        assert result == "Error: the built-in tool returned an invalid result"


# ---------------------------------------------------------------------------
# add_memory: availability gating
# ---------------------------------------------------------------------------

class TestAddMemoryAvailability:
    def test_available_when_feature_on_and_budget_positive(self, tmp_path):
        tools = get_builtin_tools_for(_persona(tmp_path), _settings())
        assert [t["function"]["name"] for t in tools] == [ADD_MEMORY_NAME]

    def test_unavailable_when_global_flag_off(self, tmp_path):
        tools = get_builtin_tools_for(
            _persona(tmp_path), _settings(enable_persona_memories=False),
        )
        assert tools == []

    def test_unavailable_when_budget_zero(self, tmp_path):
        tools = get_builtin_tools_for(_persona(tmp_path, memory_size=0), _settings())
        assert tools == []

    def test_global_flag_off_also_blocks_when_budget_positive(self, tmp_path):
        # Both gates are ANDed; one off is enough.
        tools = get_builtin_tools_for(
            _persona(tmp_path, memory_size=0),
            _settings(enable_persona_memories=False),
        )
        assert tools == []


# ---------------------------------------------------------------------------
# add_memory: end-to-end through call_builtin_tool
# ---------------------------------------------------------------------------

class TestAddMemoryTool:
    def test_saves_memory_and_reports_success(self, tmp_path):
        persona = _persona(tmp_path)

        result = call_builtin_tool(
            persona, ADD_MEMORY_NAME, {"memory": "The user told me they like tea."},
        )

        assert result == "The memory was saved successfully."
        assert (persona.persona_dir / "memories.txt").read_text() == (
            "The user told me they like tea.\n"
        )

    def test_persona_without_directory_reports_generic_io_error(self, tmp_path):
        # No persona_dir -> no file to write; the honest answer is the
        # generic save-failure, not a crash or a fabricated success.
        result = call_builtin_tool(
            _dirless_persona(), ADD_MEMORY_NAME, {"memory": "The user told me x."},
        )
        assert result == "Error: The memory could not be saved."
        assert not (tmp_path / "memories.txt").exists()

    def test_disabled_budget_deletes_stale_file_and_reports_error(self, tmp_path):
        persona = _persona(tmp_path, memory_size=0)
        (persona.persona_dir / "memories.txt").write_text("stale\n")

        result = call_builtin_tool(
            persona, ADD_MEMORY_NAME, {"memory": "The user told me new."},
        )

        assert result == "Error: Memory is not enabled for this persona."
        assert not (persona.persona_dir / "memories.txt").exists()

    def test_missing_memory_argument_reports_no_content(self, tmp_path):
        persona = _persona(tmp_path)
        result = call_builtin_tool(persona, ADD_MEMORY_NAME, {})
        assert result == "Error: The memory was not saved because it had no content."
        assert not (persona.persona_dir / "memories.txt").exists()

    def test_empty_memory_argument_reports_no_content(self, tmp_path):
        persona = _persona(tmp_path)
        result = call_builtin_tool(persona, ADD_MEMORY_NAME, {"memory": "   "})
        assert result == "Error: The memory was not saved because it had no content."
