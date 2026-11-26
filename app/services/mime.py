"""MIME type -> file extension helpers.

Framework-agnostic and stdlib-only. This is the single source of truth for
turning a MIME type into a file extension, shared by the persistence layer
(dotted audio filenames) and the STT client (multipart part filenames).

Deliberately deterministic: this never consults the host OS's mime database
(`mimetypes.guess_extension`). That function's answer depends on the
operating system — macOS maps ``audio/webm`` to ``.weba``, most Linux boxes
to nothing at all — so basing a filename on it would make the output
platform-dependent. The MIME subtype is already the container name the client
recorded into, and the part's ``Content-Type`` header carries the
authoritative type either way.
"""

import re
from typing import FrozenSet, Optional

# Subtypes whose raw name is not a usable file extension, mapped to the
# conventional one. Kept to the smallest set that is actually needed; this is
# where a future override belongs rather than a new branch in the function.
_SUBTYPE_ALIASES = {
    "mpeg": "mp3",  # "audio/mpeg" records as .mp3 in practice
}

# The extension alphabet this module is ABLE to emit — the one definition of
# it. A derived extension is kept to [a-z0-9_-] because the result is
# interpolated into on-disk audio filenames and into the STT multipart part
# name, and the input (a client-advertised MIME subtype) is untrusted.
#
# Note what is deliberately NOT here: any requirement that an extension start
# with a letter. "audio/_foo" sanitizes to "_foo", so anything validating a
# filename this module named must accept a leading "_" or "-". The clip-name
# allowlist in app/persistence.py originally hand-copied a stricter guess
# ("extensions start alphanumeric") and the two drifted — the writer saved a
# clip that the playback endpoint 404s forever. EXTENSION_SAFE_RE_CLASS below
# exists so that copy never has to be made a third time.
EXTENSION_SAFE_CHARS: FrozenSet[str] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")

# Regex character-class form of the set above, for consumers that match
# filenames. Derived from EXTENSION_SAFE_CHARS rather than written out, so an
# allowlist built from it cannot disagree with the sanitizer that produced the
# name it is checking.
EXTENSION_SAFE_RE_CLASS = "[" + "".join(re.escape(c) for c in sorted(EXTENSION_SAFE_CHARS)) + "]"


def mime_to_extension(mime_type: Optional[str]) -> str:
    """Return the bare (dot-less) file extension for a MIME type.

    Falls back to ``'bin'`` when no sensible extension can be derived.

    Rules, applied in order:
      1. Strip MIME parameters (``audio/ogg;rate=44100`` -> ``audio/ogg``).
      2. Empty input or no ``/`` -> ``'bin'``.
      3. Take the subtype (the part after the *last* ``/``, so a malformed
         multi-slash string can never leak a path separator into the result).
      4. Drop a ``+suffix`` structured-syntax tail (``application/problem+json``
         -> ``problem``).
      5. Drop a vendor ``x-`` prefix (``audio/x-wav`` -> ``wav``).
      6. Apply a small alias table (e.g. ``mpeg`` -> ``mp3``).
      7. Drop characters outside ``EXTENSION_SAFE_CHARS``.
      8. Empty result -> ``'bin'``.
    """
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    if "/" not in base:
        return "bin"

    subtype = base.rsplit("/", 1)[-1]
    subtype = subtype.split("+", 1)[0]
    if subtype.startswith("x-"):
        subtype = subtype[2:]
    subtype = _SUBTYPE_ALIASES.get(subtype, subtype)

    # MIME types are untrusted input (they arrive from client requests); ensure the
    # extension is safe to interpolate into filenames across platforms.
    safe = "".join(ch for ch in subtype if ch in EXTENSION_SAFE_CHARS)
    return safe or "bin"
