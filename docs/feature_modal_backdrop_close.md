# Modal backdrop close (and the drag that closed the modal)

A bug report that sounds like nonsense, and the one-line rule that kills it.

## The symptom

Reported against the **New Persona** form (Persona Editor modal): click an input
field, drag the mouse pointer to the left so it leaves the modal box, release —
and the entire modal disappears, taking the half-typed persona with it.

Verified in Firefox on Linux. The detail that makes it sound made up: **it never
happens on a text field.** Only the checkboxes, the colour picker, the sliders,
the buttons and the file inputs trigger it.

## Why it happens (it is not "just a browser quirk")

Every modal in this app used the obvious close-on-outside-click handler:

```js
personaEditorOverlay.addEventListener("click", (e) => {
    if (e.target === personaEditorOverlay) closePersonaEditor();
});
```

The markup is a full-viewport backdrop with the dialog centred inside it:

```html
<div id="persona-editor-overlay" class="modal-overlay hidden">   <!-- fixed; inset: 0 -->
    <div class="modal-box"> ... the form ... </div>
</div>
```

so "click the dark area outside the dialog" *looks* unambiguous. It isn't.

A browser does **not** fire `click` on the element under the cursor. It fires it
on the **nearest common ancestor of the mousedown target and the mouseup target**
(that is the UI Events definition of what a "click" on a drag is). So:

| pointer | element |
|---------|---------|
| `mousedown` | the checkbox, inside `.modal-box` |
| `mouseup` | `.modal-overlay` (the backdrop, left of the dialog) |
| → `click` dispatched on | **`.modal-overlay`**, because it is the common ancestor |

`e.target === personaEditorOverlay` is therefore `true`, and the handler closes
the modal. Nothing in the frontend logic is wrong; the *test* it relies on is
answering a different question than the one it asks.

Text fields escape this because a drag that begins in a text field is a *text
selection* gesture, and the browser never synthesises a click for it at all. Same
mouse movement, different control, opposite outcome — hence the report that made
nobody believe the user.

The mirror case was also broken and nobody had noticed: press on the backdrop,
drag right until the pointer is over the dialog, release → `click` is dispatched
on the backdrop again → close.

## The rule

`installBackdropClose(overlay, closeFn)` in `static/utils.js`. Decide from the
**press pair**, never from the synthesised click: close only when a **main
button (left)** `mousedown` **and** `mouseup` both land on the overlay itself.

```js
function installBackdropClose(overlay, closeFn) {
    const MAIN_BUTTON = 0;
    let pressStartedOnBackdrop = false;

    overlay.addEventListener("mousedown", (e) => {
        pressStartedOnBackdrop = e.button === MAIN_BUTTON && e.target === overlay;
    });

    overlay.addEventListener("mouseup", (e) => {
        const clickCompletedOnBackdrop =
            pressStartedOnBackdrop && e.button === MAIN_BUTTON && e.target === overlay;
        pressStartedOnBackdrop = false;
        if (clickCompletedOnBackdrop) closeFn();
    });
}
```

`mousedown`/`mouseup` fire on the element actually under the pointer (only `click`
gets retargeted), which is exactly what makes this correct. Presses that start
*inside* the modal still bubble to the overlay listener — the overlay is an
ancestor of the modal box — so the flag is re-armed on every press, and a press
released outside the browser window (where no `mouseup` is delivered at all) can
never leave a stale "close me" behind.

### …and the press must be the LEFT button (issue #172)

`mousedown` and `mouseup` fire **once per mouse button**, with `MouseEvent.button`
`0` / `1` / `2` = main / auxiliary (middle) / secondary (right). A press pair
that only compares `target` therefore also accepts a **middle-click** (button 1,
which on X11 additionally pastes the primary selection) and a **right-click**
(button 2, which pops the native context menu over the page) on the backdrop as
"a genuine backdrop click" — and the half-filled persona form disappears while
the user is still looking for a context menu.

The uncomfortable part: **the old `click`-on-overlay handler never had this
bug.** A browser synthesises `click` for the main button only (middle is
`auxclick`, right is `contextmenu`), so rewriting the close from `click` to the
press pair is what opened the hole. Lesson: when you replace an event with the
lower-level events underneath it, check which of the higher event's guarantees
you just gave away.

Both ends of the pair are gated:

- the **`mousedown`** gate is the fix itself, and it assigns the flag for every
  press rather than only inside a left-button branch — a non-main press must
  *disarm* as well as fail to arm, or an earlier off-window left press could
  come back to close the modal later;
- the **`mouseup`** gate costs one condition and refuses the ambiguous residue:
  middle button down on the backdrop, left button down there too, middle
  released first. A release-side gate can only ever *suppress* a close, never
  arm one, so the worst it does is drop a press nobody means.

The native context menu is deliberately **not** suppressed (`contextmenu` →
`preventDefault`): blocking it app-wide to paper over a close bug would be worse
than the bug.

Every backdrop-closable modal in the app shares it. The authoritative list is
**derived from `templates/index.html`** by the test suite (every `id` on a
`class="modal-overlay"` element), not hand-maintained — the table below is a
snapshot for readers, and the tests are what complains if it stops matching:

| Modal | Module |
|-------|--------|
| `persona-editor-overlay`, `pe-confirm-overlay` | `static/persona.js` |
| `chatrooms-overlay`, `cr-confirm-overlay`, `persona-picker-overlay` | `static/chatrooms.js` |
| `settings-overlay` | `static/settings.js` |
| `gen-settings-overlay` | `static/gen-settings.js` |

**Do not** write a per-modal `click`-on-overlay handler; the grep guard in the
test suite scans **every** module in `static/` and fails the build if one comes
back, and a second, behavioural check asserts that each overlay in the template
ended up with the press-pair listeners and no `click` listener at all.

## Tests

`tests/test_modal_backdrop.js` (plain Node, deliberately outside pytest):

```bash
node tests/test_modal_backdrop.js
```

It drives the real listeners of every modal in the app through five press
shapes each — backdrop/backdrop (closes), control→backdrop (the reported
bug), backdrop→control, backdrop→off-window, and **middle- and right-button
backdrop presses** (issue #172, must not close — followed by a left click that
must still close) — and asserts the flag re-arms so a skipped `mouseup` never
sticks.

`press()` models the button: `{ down, up, button = 0 }`, and the activation
event it dispatches afterwards depends on it (`click` / `auxclick` /
`contextmenu`). A companion self-check installs the **pre-#172** button-blind
press-pair handler and asserts it is fooled twice, so a harness that quietly
stopped delivering `button` cannot make those tests vacuous.

The set of modals those loops run over is derived from the markup rather than
written out (this file, `AGENTS.md` and the test all used to carry a hand-typed
count, which is exactly how "six modals" survived the arrival of a seventh).
Each id's `owner` is resolved from the `installBackdropClose()` call that wires
it, so:

- adding a modal to `templates/index.html` gets it every backdrop test
  automatically — nothing else has to be edited;
- adding one **without** wiring it, or wiring it in a module the harness does
  not load, fails the suite with that element id in the message (a commented-out
  `installBackdropClose()` does not count as wiring — that was verified);
- the scraper that builds the list has its own self-check test, because a
  scraper that quietly returned nothing would leave the per-modal loops
  registering zero tests: green, and covering nothing.

The harness part matters more than the assertions: a naive stub that only
dispatches the events you name would make every drag test pass against the
broken code, because the broken code only ever listened to `click`. So the
fake elements carry **parent chains**, and `press()` finishes by dispatching
`click` on `nearestCommonAncestor(mousedownTarget, mouseupTarget)` — the real
rule. A self-check test installs the old naive handler and asserts that it
*is* fooled, so the harness can never silently stop modelling it.

## Gotchas

- The close now happens on **release** (`mouseup`), not on the later synthesized
  `click`. Visually identical; both fire in the same task in practice.
- Touch works through the browser's compat mouse events; a tap on the backdrop
  still closes the modal.
- Native pickers (the colour dialog, the file chooser) open on the press, so the
  modal must not be closed by whatever the popup does to the pointer afterwards.
  Keying on where the press *started* is what makes that safe.
- Escape-key close is **not** implemented — it would have to be per-modal and
  would also have to respect nested dialogs (`pe-confirm-overlay` sits on top of
  `persona-editor-overlay`). Out of scope for this fix.
