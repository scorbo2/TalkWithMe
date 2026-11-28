"""The single chokepoint for reading and writing our YAML files on disk.

Framework-agnostic and stdlib-only (same shape as ``app/services/mime.py``).
Shared by ``app/config.py`` (``settings.yaml`` + ``chatrooms.yaml``) and
``app/services/persona_store.py`` (the legacy ``personas.yaml`` migration).

Why a chokepoint instead of a local ``open()`` in every loader: every one of
these files holds user text — persona names, system prompts, room names — and
every one of them is written with ``allow_unicode=True``, i.e. non-ASCII is
stored raw rather than escaped. A bare ``open()`` then silently inherits the
interpreter's *locale* default encoding, which is only UTF-8 by luck on a
Linux/macOS box. On Windows it is the ANSI codepage (cp1252 on a Western
install), and both failure modes are bad:

  * reading a UTF-8 file under cp1252 either raises ``UnicodeDecodeError``
    (any byte cp1252 leaves undefined) or — worse, because nothing complains —
    decodes ``Björk`` as ``BjÃ¶rk`` and feeds that to the UI, the router and
    the LLM;
  * writing under cp1252 raises ``UnicodeEncodeError`` the moment a value
    leaves the codepage (emoji, CJK), which used to happen *after* ``open(...,
    "w")`` had already truncated the file — leaving a half-written
    ``settings.yaml`` behind.

Pinning the encoding here makes UTF-8 the only option rather than six
independent call sites a contributor can forget. (Interpreters with UTF-8 as
the default text encoding — PEP 686, Python 3.15+ — are still not enough: the
supported floor here is 3.10, and files written correctly must be readable
regardless of how the reader's locale is configured.)
"""

import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE_ENCODING = "utf-8"
"""The one encoding every YAML config file is read and written with."""


def load_yaml_file(path: Path) -> Any:
    """Parse a UTF-8 YAML file and return the raw document.

    Returns ``None`` for an empty document — callers decide whether that means
    "defaults" (``settings.yaml``) or "no rooms" (``chatrooms.yaml``).

    Raises on a missing file or malformed YAML; the callers already have
    deliberately different policies for those two cases (a missing
    ``settings.yaml`` is normal, a malformed legacy ``personas.yaml`` is a
    fatal migration error), so the policy stays with them, not here.
    """
    with open(path, encoding=CONFIG_FILE_ENCODING) as f:
        return yaml.safe_load(f)


def save_yaml_file(path: Path, data: Any) -> None:
    """Serialize ``data`` to ``path`` as UTF-8 YAML, atomically.

    Writes to a sibling temp file and swaps it in with ``os.replace()``, so a
    serialization error (or a full disk) can never leave a truncated config
    file where a valid one used to be. Same contract as the room history file
    (see ``app/persistence.py::_write_history_file``).

    Uses ``safe_dump``, not ``dump``: everything here comes out of
    ``model_dump()``, so anything non-plain is a bug upstream — and ``dump``
    would write it as a ``!!python/object`` tag our own ``safe_load`` refuses
    to read, i.e. a config file the next startup cannot open.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding=CONFIG_FILE_ENCODING) as f:
            yaml.safe_dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        os.replace(tmp_path, path)
    except BaseException:
        # Never leave the half-written temp lying around for the next run.
        tmp_path.unlink(missing_ok=True)
        raise
