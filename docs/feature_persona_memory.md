# Persona memory

This document describes an addition to the TalkWithMe app.
The goal is to give each persona a persistent memory file, to store interesting
things that the user has told that persona.

## Current state

Each chatroom has a persistence file (`history.json` in the chatroom's subdirectory).
This persistence file stores all chat messages (both user and persona messages)
that have occurred in that chatroom. When the user selects "New Chat" in that
chatroom in the UI, the history for that chatroom is cleared.

## Proposed state

Each LLM chat call (not router calls) should include a new tool called `add_memory`, 
if the persona in question has `allow_tool_calls` enabled, with instructions
to call this new tool if the user has mentioned anything "interesting" - the exact
definition of "interesting" is discussed in the "Proposed LLM prompts" section.

Selecting "New Chat" in the UI still clears the chatroom's `history.json` as before,
but explicitly does NOT remove persona memories - those persist.

### Configuration changes

For each persona: new field `memory_size` (integer). Value of 0 disables
saving of memories for that persona. Negative values and non-integer values
are rejected on load (log warning and assume default value).
Maximum allowable size is 16384. Default value is 8192. This `memory_size`
property describes the file size (in bytes) of the persona's memories file (UTF-8).

In general settings: new global flag `enable_persona_memories` (boolean).
Toggling this value does NOT change the `memory_size` for any persona, nor
does it delete any saved memories when disabled. It simply disables the
`add_memory` tool, so that no new memories can be saved. This allows the user
to "pause" all new memory activity globally, without changing persona-specific settings.
The memory feature can be globally reactivated later by toggling this back to true.
The default value is true. Invalid values in `settings.yaml` are rejected
on load (log warning and assume default value). Note that toggling `enable_persona_memories`
to off not only disables adding new memories, but also prevents the LLM from being
supplied with existing memories. The memory feature is disabled application-wide.

The existing persona property `allow_tool_calls` is used in conjunction with this
feature. In order for the LLM to submit a new memory for a persona, the following
conditions must be met:
- `enable_persona_memories` is enabled globally
- `memory_size` for the persona must be nonzero
- `allow_tool_calls` must be enabled for the persona

### The `add_memory` tool, and saving memories

Each persona directory will have a new optional file `memories.txt`.
The `add_memory` tool will take a memory supplied by the LLM
and append it to this file, creating the file if it does not yet exist.
The LLM will be instructed to formulate each memory as a single
line of text, so the structure of this file is one memory per line,
with older memories at the top of the file and newer memories towards the bottom.

If `add_memory` is invoked when the file already meets or exceeds the persona's
configured memory size, existing memories are purged from oldest to newest until the
file size is beneath the configured limit, or until only the newly-added memory remains. 
If `add_memory` is invoked when the  configured limit is 0, the file is deleted if it
exists, and the new memory is ignored (error returned from tool). The purge should be done
via a temp file so that an atomic rename can protect against problems during the purge.

If `add_memory` is invoked with a memory that exceeds the current configured
memory size, the memory is NOT stored (return error from tool call).

If `add_memory` is invoked with an empty, null, or blank (only whitespace)
memory, the memory is not stored (return error from tool call).

Each new memory is expected as one line of text with a length limit of 1024 characters.
Do NOT truncate if the message exceeds this length! Reject it with an explicit error.
Guard against garbage input from the LLM by stripping out leading/trailing whitespace,
and removing all newline characters within the given memory. If the result is empty,
reject it with an explicit error.

The `add_memory` tool returns success/failure to the LLM. The LLM is instructed
not to mention the use of this tool in their response. (But the tool chip will
be visible to the user if `show_tool_calls` is enabled).

The `memory_size` value for each persona is stored in the YAML frontmatter of their
`prompt.md` file. If the property is missing, the default value is assumed.
(The value will be missing for all existing personas on upgrade from previous
versions, so silently assuming the default value is a safe choice). Note that the
new feature is therefore automatically enabled for all existing personas on upgrade.
This is deliberate. The user can disable it globally with `enable_persona_memories`,
or individually for each persona by setting `memory_size` to 0.

When the LLM invokes `add_memory`, a tool call chip should be displayed, following
the same rules as MCP tool call chips. It should honor the `show_tool_calls` flag
which already exists.

Possible tool response messages:
- On success: "The memory was saved successfully."
- `memory_size` is set to 0: "Error: Memory is not enabled for this persona."
- Memory exceeds `memory_size`: "Error: The memory was too large to save."
- Memory exceeds 1024 character limit: "Error: The memory was too large to save."
- Empty/null/blank memory provided: "Error: The memory was not saved because it had no content."
- Other errors (I/O problems or such): "Error: The memory could not be saved."

Note: the "Error:" prefix in error messages is required. The current tool chip logic
detects failures by looking for that exact prefix.

### New tool type: built-in tools

Currently, all tools are MCP tools, collected from all registered MCP servers.
The `add_memory` tool is a *new type* of tool, one that is built into this application.
It is available even if no MCP tools are registered. It wins any naming conflict
with any tool supplied by any MCP server. There may be additional built-in tools
added in future releases, so it would be good to build the code in a generic and
extensible way. Perhaps a new `app/services/builtin.py` to handle built-in tools.

The existing `stream_chat_with_tools()` must take built-in tools into consideration.

The existing `stream_chat()` should be unaffected by this feature. (That code
path cannot execute tools at all).

### "Echo Chamber"

The Echo Chamber feature bypasses the LLM entirely, so there are no changes
to consider for Echo Chamber. This feature does not affect it.

### UI changes

The Persona Editor modal needs a new field to allow the user to set `memory_size` for
each persona. Confirming the modal needs to add the new value for `memory_size` to
the YAML frontmatter section of their `prompt.md` file, overwriting any previous value,
or adding the property if it was not already there. Setting `memory_size` to 0 and confirming
this modal should delete the memories file for that persona, if it existed.

The General Settings modal needs a new checkbox for `enable_persona_memories`.

### Cloning a persona

Cloning a persona currently copies the persona's directory contents. With this new
feature, that means that the source persona's memories file will also be copied
to the clone. This is fine.

### Chat flow changes

The router flow is unchanged.

If the memories file for a persona is non-empty when that persona is called upon,
AND if `enable_persona_memories` is enabled globally, AND if `memory_size` is nonzero
for the persona in question, then the contents of the persona's memories file should 
be appended to their system prompt with a brief note to put it in context. For example:

```
<Alex's usual system prompt goes here>

You have the following memories related to the user:
<Contents of Alex's memories file goes here>
```

The LLM can therefore consider all saved memories when formulating its response.
It may decide to reference one or more of these memories in the context of the
current conversation.

Note that a persona might have `allow_tool_calls` disabled, yet still meet the
qualifying conditions above for memory injection. This is fine. The LLM will have
access to any existing memories for this persona, but will be unable to add new ones.

### API changes

`PersonaDetailResponse` must gain `memory_size` and `GET /api/personas/{name}/detail` must
return it, otherwise the editor can't pre-fill the field. Create/update need it as a form field too.

## Proposed LLM prompts:

Description of the `add_memory` tool should include the following points:
- Submit a maximum of ONE memory per conversation turn.
- Each memory must be submitted as a SINGLE LINE of text, length limit 1024 characters.
- Do not mention that you are invoking this tool. The UI allows the user to see tool calls.
- Errors from this tool are not fatal - this is an optional feature, it's fine to proceed if the memory was not saved.
- It is not a requirement to submit a memory on every conversation turn.
- Begin each memory with "The user told me" (always refer to the user as "the user" when saving memories)
- Only submit a memory if the user has revealed something interesting about themselves OR if the user explicitly asks for something to be remembered.
  - examples: ambitions, hopes, dreams, fears, strong emotions, personal anecdotes
  - specific requests: "Call me Tom from now on" -> "The user told me to address them as Tom."
- Example memory: "The user told me they prefer cats over dogs."
- Example memory: "The user told me they'd like to take singing lessons one day."
- Do NOT store mundanities: "The user told me hello" - this is not interesting!
- Do NOT add memories that are redundant with or very similar to memories that you have already stored.

## Testing

Memory storage, retrieval, purge handling, and configuration properties are all good candidates for unit testing.

Include a `test_chat_sse.py` test for the `add_memory` tool event + prompt injection.

Update `tests/test_persona_form.js` for the new persona form field.

Add a test to ensure that saving from the Servers dialog (which sends no `general` section) does not
clobber `enable_persona_memories`.

If the "built-in" implementation introduces any module-level state, it goes in the `conftest.py` autouse fixture.

## Concurrency

Two browser tabs could near-simultaneously invoke `add_memory` for the same persona. This is a single-user local
app, so the risk is acceptable. Last-write-wins is fine here.
