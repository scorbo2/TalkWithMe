"""Expressive speech: per-sentence voice direction and inline vocal events.

When `general.expressive_speech` is on and the connected TTS engine takes a
voice instruction together with the reference clip (Breeze TTS: "voice
direction"), the LLM is told about two kinds of markup, and this module turns
that markup into what the engine understands:

- **Voice direction** — `{coldly, slowly}` at the start of a sentence. The
  frontend strips it from the chat bubble; for TTS it becomes the request's
  `instruction` (replacing the static one from `tts.parameters`) for that
  sentence and the following ones, until the next tag.
- **Vocal events** — `(laugh)`, `(sigh)`, ... inside the text. Breeze
  performs them; the text keeps them. Only the words in VOCAL_EVENTS pass
  (inflections too: laughing -> laugh): a bracket naming several sounds,
  "(cough, nervous laugh)", becomes "(cough) (laugh)", and any other
  parenthesis is unwrapped to plain text (an aside the engine would
  otherwise mangle). `*laughs*`-style actions are handled the same way.

With the feature on but an engine that cannot take an instruction (e.g.
faster-qwen3-tts or OmniVoice), the markup is stripped instead: directions dropped, events
removed, so the voice never reads them aloud. With the feature off nothing
here runs and TTS text is untouched.

Framework-agnostic: no FastAPI, no settings access — callers pass what they
know.
"""

import re
from typing import Optional

# The first four are the documented Breeze TTS events; the rest rendered in a
# listening test with four cloned voices (more or less audibly, depending on
# the voice). Parentheses are plain text to the engine, so any word can be
# tried; these are the ones that did something.
VOCAL_EVENTS = (
    "laugh", "cough", "clears throat", "sigh",
    "laughs", "chuckle", "giggle", "snicker", "scoff", "snort",
    "gasp", "groan", "grunt", "sniff", "sob", "cry", "scream", "yawn", "sneeze", "hiccup",
    "hum", "hmm", "uh", "um", "tsk",
    "inhale", "exhale", "deep breath", "breathes heavily", "pause",
    "whisper", "whispers", "shouts", "mumbles",
)
# Bounded lengths: a stray "{" or "(" must not swallow a whole reply.
_DIRECTION_TAG = re.compile(r"\{([^{}]{0,200})\}")
_PARENTHESIS = re.compile(r"\(([^()]{0,80})\)")
_ASTERISK_ACTION = re.compile(r"\*([^*\n]{1,40})\*")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")


# Several sounds in one bracket: "(cough, nervous laugh)", "{sigh and groan}".
_LIST_SEPARATOR = re.compile(r"\s*(?:,|;|/|\band\b|\bthen\b)\s*")


def _inflections(event: str) -> list[str]:
    """laugh -> laughs, laughing, laughed; giggle -> giggling, giggled; ..."""
    forms = [event, event + "s", event + "es", event + "ing", event + "ed"]
    if event.endswith("e"):
        forms += [event[:-1] + "ing", event[:-1] + "ed"]
    return forms


# Longest first, so "deep breath" wins over a shorter overlapping event; an
# event's own name wins over another event's inflection ("laughs" is an event
# of its own, not laugh + s).
_EVENT_FORMS = sorted(
    ((form, event) for event in VOCAL_EVENTS for form in _inflections(event)),
    key=lambda pair: (-len(pair[0]), pair[0] != pair[1]),
)
_EXACT_FORMS = {}
for _form, _event in _EVENT_FORMS:
    _EXACT_FORMS.setdefault(_form, _event)


def _event_in(part: str, *, exact: bool) -> Optional[str]:
    """The event a bracket part names: the whole part ("sighing"), or with
    exact=False any event word inside it ("nervous laugh" -> laugh)."""
    if exact:
        return _EXACT_FORMS.get(part)
    for form, event in _EVENT_FORMS:
        if re.search(rf"\b{re.escape(form)}\b", part):
            return event
    return None


def _bracket_events(inner: str, *, exact: bool) -> Optional[list[str]]:
    """Events named in a bracket, or None when it is not a sound bracket.

    exact=True (curly braces): every part must be an event, otherwise the
    braces are a direction — "{pause, then coldly}" stays a direction.
    exact=False (round brackets): one event is enough, the other words are
    dropped — "(cough, nervous laugh)" -> (cough) (laugh), never read aloud.
    """
    parts = [p for p in _LIST_SEPARATOR.split(inner.strip().lower()) if p]
    events = [e for e in (_event_in(p, exact=exact) for p in parts) if e]
    if not events or (exact and len(events) != len(parts)):
        return None
    return events


def _render_events(events: list[str], allowed: bool) -> str:
    return " " + " ".join(f"({e})" for e in events) + " " if allowed else " "


def _events_out_of_braces(text: str) -> str:
    """{sigh} -> (sigh): LLMs put sounds in braces too; a sound is never a direction."""

    def convert(match: re.Match) -> str:
        events = _bracket_events(match.group(1), exact=True)
        return _render_events(events, True) if events else match.group(0)

    return _DIRECTION_TAG.sub(convert, text)


def split_direction(text: str) -> tuple[Optional[str], str]:
    """(first direction in the text or None, text without any direction tag).

    A tag holding a known vocal event ({sigh}) is turned into the event
    instead, so it is neither a direction nor lost.
    """
    text = _events_out_of_braces(text)
    direction = None
    for match in _DIRECTION_TAG.finditer(text):
        candidate = match.group(1).strip()
        if candidate:
            direction = candidate
            break
    return direction, _tidy(_DIRECTION_TAG.sub(" ", text))


def prepare_tts_text(text: str, events_allowed: bool) -> str:
    """TTS text: no direction tags, known events kept (or removed), other brackets unwrapped."""

    def bracket(match: re.Match) -> str:
        events = _bracket_events(match.group(1), exact=False)
        if events:
            return _render_events(events, events_allowed)
        return f" {match.group(1).strip()} "

    text = _DIRECTION_TAG.sub(" ", _events_out_of_braces(text))
    text = _ASTERISK_ACTION.sub(bracket, text)
    text = _PARENTHESIS.sub(bracket, text)
    return _tidy(text)


def delivery_prompt() -> str:
    """System-prompt section that teaches the LLM the markup."""
    events = ", ".join(f"({e})" for e in VOCAL_EVENTS)
    return (
        "Delivery markup: your replies are spoken aloud by a voice that performs this markup.\n"
        "- Voice direction: you may begin a sentence with a short direction in curly braces, "
        "for example {coldly, slowly} or {whispering, nervous} or {shouting, furious}. "
        "It sets how that sentence and the following ones are spoken, until the next direction. "
        "It is heard, never shown. Change it when the mood changes, not on every sentence. "
        "No full stops inside the braces.\n"
        f"- Vocal events: you may place one of these sounds in round brackets inside or between "
        f"sentences: {events}. Use only these exact words, sparingly, where the character "
        "would really make that sound. Sounds always go in round brackets, never in curly "
        "braces: write {nervous} Well (laugh), no, not {laugh}.\n"
        "- Do not use round brackets, asterisks or curly braces for anything else, and do not "
        "describe actions or gestures."
    )


def _tidy(text: str) -> str:
    text = _SPACES.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return text.strip()
