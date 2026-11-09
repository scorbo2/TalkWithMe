# Copy-to-clipboard button on every chat bubble

Issue #158. The goal is small: let the user copy the text of one chat bubble
without first selecting it by hand. The button goes **in the same row as the
existing "delete message" button** and is hidden until the bubble is hovered,
exactly like the delete button.

## Current state

Every message row already carries one per-message action button — the 🗑
delete button (`addDeleteButtonToRow()` in
`static/chat.js`, `.message-delete-btn` in `static/style.css`; see
`docs/feature_delete_message.md`). It is created
by four different render paths (live user bubble, live/streaming assistant
row, and the two persisted-history renderers), appended as the last child of
the row's *content container*:

- `.user-message-content` for user rows,
- `.bubble-content` for persona rows.

Both of those containers are `display: flex; flex-direction: column`, which is
what makes audio stack under the bubble. That detail decides the shape of this
feature: two buttons appended as siblings of that container **stack
vertically**, not side by side.

There is no clipboard support anywhere in the frontend today.

## Desired state

Every message row that has a delete button also has a copy button, to its
left, in the same row:

```
.message-row.user      .bubble
                       .message-audio        (optional, replay buttons)
                       .message-actions      [📋] [🗑]     <- one flex row

.message-row.assistant .bubble-name
                       .bubble
                       .message-audio        (optional)
                       .message-actions      [📋] [🗑]     <- one flex row
```

- Both buttons live in one new wrapper, `.message-actions`, which is the last
  child of the content container. The wrapper is *required* by the column-flex
  layout above — it is not decoration.
- Reveal rules are the delete button's rules: hidden by default, visible on
  `.message-row:hover` / `:focus-within`, always visible on touch devices
  (`@media (hover: none)`). No new styling vocabulary.
- The copy button is enabled from the moment the row exists, including on a
  streaming assistant row. Copying half a reply is legal and not destructive,
  so it does not wait for the `done` event the way the delete button does.
- Clicking it copies the bubble's visible text and briefly shows ✅ (or ❌).
- Error bubbles (`appendErrorBubble`) get neither button: they have no content
  container and are never persisted.

## UI changes

### One factory, not two

`addDeleteButtonToRow(row, messageId, disabled)` becomes
**`addMessageActionButtons(row, messageId, disabled)`** and creates the
wrapper plus both buttons in one call. Creating them together is deliberate:
four render paths build rows, and one call site per button is one forgotten
call away from a half-rendered action row. The name has to change with the
behaviour — a function called "add delete button" that also adds a copy
button is a trap for the next person who greps for "delete".

### Copy source: `bubble.textContent`, verbatim

`copyableTextOf(row)` reads the row's `.bubble`. Everything the user does not
want in the clipboard is already outside that element — the persona name
(`.bubble-name`), the tool chips (`.tool-chips`) and the replay buttons
(`.message-audio`) — because streaming tokens are appended to
`bubble.textContent` itself, which is why nothing else was ever allowed inside
the bubble. Two consequences, both intended and both pinned by tests:

- The `[Error: ...]` trailer that the SSE `error` handler appends to the
  bubble is copied too. It is on screen; the copy should agree with the screen.
- An assistant row's loading placeholder **is** a `.bubble`
  (`class="bubble loading-bubble"`), so "the row has a bubble" does not mean
  "the row has text". A blank bubble is treated as *nothing to copy*: the
  clipboard is left untouched (writing `""` would wipe whatever the user had
  copied) and the button says "Nothing to copy yet".

### Feedback: the Clipboard API confirms nothing

`flashCopyButton()` swaps the glyph to ✅ (or ❌) for 1.5 s, then restores 📋,
updating `title` **and** `aria-label` (the button's accessible name comes from
`aria-label`, so a swapped emoji alone would leave a screen reader announcing
"Copy text" after a failure). The pending revert timer is kept on the button
and `clearTimeout()`ed first, otherwise a second click inside the window has
the older timer wipe the newer confirmation.

### Clipboard access goes through one helper

`copyTextToClipboard(text)` in `static/utils.js` is the only frontend code
allowed to touch `navigator.clipboard`:

```js
const ok = await copyTextToClipboard("some text");   // resolves true/false
```

`navigator.clipboard` exists only in a **secure context** (HTTPS, or the
browser's `localhost` exception). It is `undefined` when the app is opened
over plain HTTP by IP or hostname — the exact LAN scenario this app is built
for, and the same one that already broke the send button (#145) and forced
`generateMessageId()` to avoid `crypto.randomUUID()`. The helper therefore:

1. uses `navigator.clipboard.writeText()` when the browser offers it;
2. on rejection **or** absence, falls back to a hidden `<textarea>` +
   `document.execCommand("copy")`, which works in insecure contexts;
3. resolves `false` when neither path worked — it never rejects, because the
   caller's only option is to tell the user.

The scratch textarea must be *off-screen* (`position: fixed; top: -1000px`),
not `display: none` / `hidden`: an element that is not rendered cannot be
selected, and the whole fallback is "select, then copy".

`tests/test_copy_button.js` includes a grep guard, in the style of
`test_message_id.js`'s "no file calls `crypto.randomUUID()` directly": no file
in `static/` may read `navigator.clipboard` except the one sanctioned line
inside `copyTextToClipboard()`.

## The landmine: audio replay buttons and `insertBefore`

This is the part that bites. `addAudioButtonToAssistantMessage()` and
`addAudioButtonToUserMessage()` create the row's `.message-audio` container
lazily and insert it *before the action buttons*, so the buttons stay last:

```js
const deleteBtn = content.querySelector(".message-delete-btn");   // descendant search
content.insertBefore(audioContainer, deleteBtn);                  // needs a DIRECT child
```

`querySelector` finds descendants; `insertBefore` only accepts a **direct
child**. The moment the delete button moves inside `.message-actions`, that
call throws `NotFoundError` — and both call sites live in unawaited fetch
chains whose `catch` logs "Failed to persist TTS/STT audio". So the failure
looks like a persistence problem and *behaves* like missing replay buttons on
live rows (mic input and TTS replies stop getting their 🔁 button). History
rendering is unaffected, which makes it look intermittent.

Fix: `getAudioContainer(container)` — one helper for both call sites — anchors
on the `.message-actions` wrapper (falling back to a bare delete button for
rows without one):

```js
const anchor = container.querySelector(".message-actions")
    || container.querySelector(".message-delete-btn");
if (anchor) container.insertBefore(audioContainer, anchor);
else container.appendChild(audioContainer);
```

Note for anyone extending the plain-Node harnesses: the stubs in
`test_speaking_highlight.js` / `test_stop_button.js` implement
`insertBefore` as "if the reference node is not found, just append" — which
silently tolerates exactly this bug. `tests/test_copy_button.js` throws
`NotFoundError` like a real browser does, and that strictness is what makes
the regression testable at all.

## What this feature does NOT do

- No "copy the whole conversation" (that is the existing history file on disk).
- No copy of persona-internal context (memories, system prompt) — the button
  copies what the bubble shows.
- No rich-text/markdown variants: plain text only, which is what a TTS-driven
  app wants on the clipboard.
- No toast/notification system: the confirmation lives on the button itself.

## Implementation notes

| File | Change |
|------|--------|
| `static/utils.js` | `copyTextToClipboard()` + `legacyCopyToClipboard()` — the Clipboard API with an insecure-context fallback (~50 LOC). |
| `static/chat.js` | `addDeleteButtonToRow()` → `addMessageActionButtons()` (wrapper + copy + delete), `copyableTextOf()`, `copyMessageText()`, `flashCopyButton()`, `setCopyButtonLabel()`, `getAudioContainer()` shared by both audio-injection paths; four call sites renamed (~90 LOC changed/added). |
| `static/style.css` | `.message-actions` flex row + `align-self` rules moved from the buttons; copy/delete share one rule set; copy button gets `min-width` so the ✅ swap does not twitch the row (~30 LOC). |
| `tests/test_copy_button.js` | New plain-Node suite (18 tests): the action-row layout, all four render paths, copied payload, feedback + revert, insecure-origin fallback, the `navigator.clipboard` grep guard, and the audio-container insertion contract. |
| `README.md` | Feature bullet, a "Copying a message" section, release-history line. |
| `AGENTS.md` | Test-coverage entry + "changes to the message action buttons require `node tests/test_copy_button.js`" rule; `chat.js`/`utils.js` row updates. |

## Development plan (as executed)

1. `copyTextToClipboard()` in `utils.js` — the fallback is the risky half, so
   it goes first.
2. `chat.js`: one action-buttons factory, both buttons in a wrapper; rename
   the four call sites.
3. `chat.js`: `getAudioContainer()` — re-anchor the audio insertion **in the
   same change** as the wrapper. Splitting them is how the regression happens.
4. `style.css`: shared button rules + `.message-actions`.
5. `tests/test_copy_button.js`, including the strict `insertBefore`.
6. Docs: this file, README, AGENTS.md.
7. Verification: `python -m pytest` plus every Node suite
   (`test_copy_button.js`, `test_speaking_highlight.js`, `test_stop_button.js`,
   `test_sentence_split.js`, `test_message_id.js`, `test_persona_form.js`,
   `test_persona_picker.js`, `test_tts_settings.js`) all green.

## Gotchas

- **Reveal rules must be duplicated per class.** `.message-copy-btn` needs its
  own `:hover` / `:focus-within` / `@media (hover: none)` selectors alongside
  `.message-delete-btn`, otherwise one of the two stays invisible on touch
  devices.
- **No `.message-copy-btn:disabled` rules.** The hidden-while-disabled
  behaviour exists only because a mid-stream delete button has nothing to
  delete yet; a copy button is never disabled, so style rules for that state
  are dead code.
- **The copy button is intentionally clickable before `done`.** Its row may
  still be a loading placeholder, which is precisely why `copyableTextOf()`
  returns "" for it rather than letting the click do nothing.
- **Emoji are not monospaced.** 📋 → ✅ changes the button's width; the
  `min-width` on `.message-copy-btn` reserves the box so the action row does
  not jump.
