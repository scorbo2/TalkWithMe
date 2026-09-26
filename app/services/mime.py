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

from typing import Optional

# Subtypes whose raw name is not a usable file extension, mapped to the
# conventional one. Kept to the smallest set that is actually needed; this is
# where a future override belongs rather than a new branch in the function.
_SUBTYPE_ALIASES = {
    "mpeg": "mp3",  # "audio/mpeg" records as .mp3 in practice
}


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
      7. Empty result -> ``'bin'``.
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
    safe = "".join(ch for ch in subtype if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in "_-" )
    return safe or "bin"
