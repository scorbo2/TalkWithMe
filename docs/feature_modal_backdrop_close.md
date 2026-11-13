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
**press pair**, never from the synthesised click: close only when `mousedown`
**and** `mouseup` both land on the overlay itself.

```js
function installBackdropClose(overlay, closeFn) {
    let pressStartedOnBackdrop = false;

    overlay.addEventListener("mousedown", (e) => {
        pressStartedOnBackdrop = e.target === overlay;
    });

    overlay.addEventListener("mouseup", (e) => {
        const clickCompletedOnBackdrop = pressStartedOnBackdrop && e.target === overlay;
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

All six backdrop-closable modals share it:

| Modal | Module |
|-------|--------|
| `persona-editor-overlay`, `pe-confirm-overlay` | `static/persona.js` |
| `chatrooms-overlay`, `cr-confirm-overlay`, `persona-picker-overlay` | `static/chatrooms.js` |
| `settings-overlay` | `static/settings.js` |
| `gen-settings-overlay` | `static/gen-settings.js` |

**Do not** write a per-modal `click`-on-overlay handler; the grep guard in the
test suite fails the build if one comes back.

## Tests

`tests/test_modal_backdrop.js` (plain Node, deliberately outside pytest):

```bash
node tests/test_modal_backdrop.js
```

It drives the real listeners of all four modal modules through four press
shapes per modal — backdrop/backdrop (closes), control→backdrop (the reported
bug), backdrop→control, and backdrop→off-window — and asserts the flag re-arms
so a skipped `mouseup` never sticks.

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
