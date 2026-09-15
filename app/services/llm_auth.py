"""LLM API key resolution (docs/feature_api_key.md).

The key is deliberately NOT part of AppSettings: GET/PUT /api/settings
round-trip the settings models — a key in the model would leak to disk
and onto the API surface. It is resolved once per process, from (in priority order):

1. the ``TALKWITHME_LLM_API_KEY`` environment variable (used verbatim,
   apart from surrounding whitespace — real keys never contain any, so
   a padded export is a typo worth forgiving);
2. the ``llm_api_key`` file in the project root (``llm_api_key = <key>``
   format — see llm_api_key.example).

If neither source is present (or yields a blank value), no key is used —
a local llama.cpp server needs none. The key is never logged, never
returned by any endpoint, and never shown in the UI. It is resolved at
startup only: changing it requires a restart (no runtime reload, by
design).
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ENV_VAR_NAME = "TALKWITHME_LLM_API_KEY"
KEY_FILENAME = "llm_api_key"

# Anchored to the package location, not the CWD: the app can be started
# from any directory, the same way settings.yaml is resolved.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_api_key: Optional[str] = None
_loaded = False


def _key_from_env() -> Optional[str]:
    raw = os.environ.get(ENV_VAR_NAME, "").strip()
    return raw or None


def _key_from_file() -> Optional[str]:
    """Parse the project-root key file, or None when it is absent/blank.

    The format is one ``llm_api_key = <value>`` line, surrounded by any
    number of comments and blank lines (the committed example file). Only
    that exact key name is honored — a bare key on a line of its own is
    NOT picked up, because guessing would misread hand-edited files.
    """
    path = _PROJECT_ROOT / KEY_FILENAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Unreadable (permissions, disk, ...): degrade to no key with a loud
        # warning instead of breaking startup — the app still works against
        # a keyless local LLM.
        logger.warning("Could not read %s: %s; no LLM API key will be used", path, exc)
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == KEY_FILENAME:
            value = value.strip()
            return value or None
    logger.warning(
        "the %s file contains no '%s = <value>' line; no LLM API key will be used",
        KEY_FILENAME, KEY_FILENAME,
    )
    return None


def load_llm_api_key() -> Optional[str]:
    """Resolve the API key for this process and cache it.

    The environment variable wins over the file; a blank variable is
    treated as unset so the file still gets a chance. Safe to call more
    than once — each call re-resolves (the lifespan calls it once at
    startup; get_llm_api_key() resolves lazily on first use).
    """
    global _api_key, _loaded
    env_key = _key_from_env()
    if env_key is not None:
        _api_key = env_key
        _loaded = True
        # Name the source, never the value: the key must not reach the log.
        logger.info("LLM API key loaded from environment variable %s", ENV_VAR_NAME)
        return _api_key
    file_key = _key_from_file()
    _api_key = file_key
    _loaded = True
    if file_key is not None:
        logger.info("LLM API key loaded from %s", KEY_FILENAME)
    return _api_key


def get_llm_api_key() -> Optional[str]:
    """The API key for this process, resolved on first access.

    Lazy (not startup-only) on purpose: request paths that run before or
    outside the lifespan — most notably the test suite, whose TestClient
    skips the startup lifespan — must still see a configured key.
    """
    if not _loaded:
        load_llm_api_key()
    return _api_key


def invalidate_llm_api_key() -> None:
    """Forget the cached key.

    For tests. The app itself resolves once per process: the key is not
    reloadable at runtime, by design (changing it requires a restart).
    """
    global _api_key, _loaded
    _api_key = None
    _loaded = False
