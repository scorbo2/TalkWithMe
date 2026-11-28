"""Tests for app/services/yaml_io.py — the single chokepoint for YAML on disk.

Two things are locked here (issue #166, "Several Windows-specific issues"):

1. The encoding invariant. settings.yaml / chatrooms.yaml / personas.yaml hold
   user text (persona names, system prompts, room names) and are written with
   allow_unicode=True, so non-ASCII is stored raw. Reading or writing them
   with the interpreter's locale default is correct only on a UTF-8 box: on a
   Windows install the default is the ANSI codepage, which mangles values
   silently or raises. The `ansi_codepage_io_default` fixture reproduces that
   default here, so a regression fails on Linux too.

2. The atomic-write invariant. The old `open(target, "w")` truncated the file
   before serializing, so an encoding failure mid-dump left a half-written
   settings.yaml where a valid one used to be.

Plus the project-wide poison guard at the bottom: no text-mode file I/O
anywhere in app/ may omit an explicit encoding.
"""

import ast
from pathlib import Path

import pytest

from app.services import yaml_io

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# A deliberately hostile mixture: an accented Latin letter (present in cp1252,
# so a wrong default silently corrupts it), CJK and an emoji (absent from
# cp1252, so a wrong default raises on either side of the round-trip).
UNICODE_TEXT = "Bj\u00f6rk \u4f50\u85e4 \U0001f643 caf\u00e9"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class TestLoadYamlFile:
    def test_load_yaml_file_with_utf8_non_ascii_document_returns_values_verbatim(self, tmp_path):
        # GIVEN a UTF-8 file holding non-ASCII text (what any correct writer
        # — including this module — produces):
        path = tmp_path / "settings.yaml"
        path.write_text(f"general:\n  global_system_prompt: '{UNICODE_TEXT}'\n", encoding="utf-8")

        # WHEN it is parsed,
        raw = yaml_io.load_yaml_file(path)

        # THEN the text survives byte-for-byte, with no locale dependence:
        assert raw["general"]["global_system_prompt"] == UNICODE_TEXT

    def test_load_yaml_file_with_ansi_codepage_default_still_reads_utf8(self, tmp_path, ansi_codepage_io_default):
        # GIVEN the interpreter default is cp1252 (a Western Windows install)
        # and the file on disk is UTF-8:
        path = tmp_path / "chatrooms.yaml"
        path.write_text(f"chat_rooms:\n- name: Lounge\n  persona_names:\n  - {UNICODE_TEXT}\n", encoding="utf-8")

        # WHEN it is read through the shared helper,
        raw = yaml_io.load_yaml_file(path)

        # THEN we get the real names, not the 'BjÃ¶rk' mojibake a bare open()
        # would have produced (the pre-fix bug):
        assert raw["chat_rooms"][0]["persona_names"] == [UNICODE_TEXT]

    def test_load_yaml_file_with_empty_document_returns_none(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        assert yaml_io.load_yaml_file(path) is None

    def test_load_yaml_file_with_missing_file_raises_file_not_found(self, tmp_path):
        # Callers own the missing-file policy (a missing settings.yaml is
        # normal, a missing personas.yaml mid-migration is fatal), so the
        # helper must not swallow it.
        with pytest.raises(FileNotFoundError):
            yaml_io.load_yaml_file(tmp_path / "nope.yaml")

    def test_load_yaml_file_with_malformed_yaml_raises(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text("personas: [unclosed", encoding="utf-8")
        with pytest.raises(Exception):
            yaml_io.load_yaml_file(path)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

class TestSaveYamlFile:
    def test_save_yaml_file_with_non_ascii_values_writes_raw_utf8_not_escapes(self, tmp_path):
        # WHEN data with non-ASCII is saved,
        path = tmp_path / "chatrooms.yaml"
        yaml_io.save_yaml_file(path, {"room": UNICODE_TEXT})

        # THEN the bytes on disk are the raw UTF-8 sequences (allow_unicode),
        # not \u escapes and not a codepage translation:
        raw = path.read_bytes()
        assert "Björk".encode("utf-8") in raw
        assert "佐藤".encode("utf-8") in raw
        assert b"\\u" not in raw

    def test_save_yaml_file_with_ansi_codepage_default_still_writes_utf8(self, tmp_path, ansi_codepage_io_default):
        # GIVEN the interpreter default is cp1252:
        # WHEN an emoji/CJK value is saved (the pre-fix path raised
        # UnicodeEncodeError here, AFTER the file had been truncated),
        path = tmp_path / "settings.yaml"
        yaml_io.save_yaml_file(path, {"general": {"global_system_prompt": UNICODE_TEXT}})

        # THEN the file is valid UTF-8 and reads back identically:
        assert yaml_io.load_yaml_file(path)["general"]["global_system_prompt"] == UNICODE_TEXT

    def test_save_yaml_file_round_trips_what_load_settings_writes(self, tmp_path, ansi_codepage_io_default):
        # GIVEN a file written by this helper,
        path = tmp_path / "settings.yaml"
        yaml_io.save_yaml_file(path, {"general": {"global_system_prompt": "Bj\u00f6rk"}})

        # WHEN/THEN reading it back — even under a cp1252 interpreter default —
        # is lossless, i.e. a file written on one platform is readable on
        # another regardless of the reader's locale:
        assert yaml_io.load_yaml_file(path)["general"]["global_system_prompt"] == "Bj\u00f6rk"

    def test_save_yaml_file_keeps_key_order_and_block_style(self, tmp_path):
        # The UI and the docs assume a readable, stable layout: sort_keys=False
        # and block style, never flow style.
        path = tmp_path / "settings.yaml"
        yaml_io.save_yaml_file(path, {"zulu": 1, "alpha": {"nested": [1, 2]}})

        text = path.read_text(encoding="utf-8")
        assert text.index("zulu") < text.index("alpha")
        assert "{ " not in text and "[1, 2]" not in text


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

class TestSaveYamlFileAtomicity:
    def test_save_yaml_file_when_serialization_fails_keeps_previous_file_intact(self, tmp_path, caplog):
        # GIVEN a valid config on disk,
        path = tmp_path / "settings.yaml"
        yaml_io.save_yaml_file(path, {"general": {"global_system_prompt": "keep me"}})
        before = path.read_bytes()

        # WHEN a save fails part-way through serializing (an unrepresentable
        # value stands in for any mid-dump error — the pre-fix Windows
        # UnicodeEncodeError was exactly this shape),
        class Unserializable:
            pass

        with pytest.raises(Exception):
            yaml_io.save_yaml_file(path, {"broken": Unserializable()})

        # THEN the old file is still there, whole — never a truncated one:
        assert path.read_bytes() == before
        assert yaml_io.load_yaml_file(path)["general"]["global_system_prompt"] == "keep me"

    def test_save_yaml_file_when_serialization_fails_leaves_no_temp_file(self, tmp_path):
        # GIVEN a target that does not exist yet,
        path = tmp_path / "settings.yaml"

        class Unserializable:
            pass

        # WHEN the save fails,
        with pytest.raises(Exception):
            yaml_io.save_yaml_file(path, {"broken": Unserializable()})

        # THEN nothing half-written is left behind for the next startup:
        assert list(tmp_path.glob("*")) == []


# ---------------------------------------------------------------------------
# The Windows emulator fixture must actually emulate something
# ---------------------------------------------------------------------------

class TestAnsiCodepageFixtureIsReal:
    """Meta-guard for `ansi_codepage_io_default` itself.

    Every encoding test in the suite depends on that fixture really making a
    bare text write come out as cp1252. If the emulation ever goes soft — a
    CPython upgrade resolving the default encoding somewhere the patch cannot
    reach, or a well-meant refactor back to `setdefault` — each of those tests
    keeps passing while proving nothing at all. So the emulation is pinned
    here, entry point by entry point.
    """

    def test_fixture_makes_bare_open_write_the_ansi_codepage(self, tmp_path, ansi_codepage_io_default):
        # WHEN a text file is written with no encoding argument,
        path = tmp_path / "bare.txt"
        with open(path, "w") as f:
            f.write("caf\u00e9")

        # THEN the bytes are cp1252 (\xe9), not the runner's UTF-8 (\xc3\xa9):
        assert path.read_bytes() == b"caf\xe9"

    def test_fixture_makes_bare_open_read_the_ansi_codepage(self, tmp_path, ansi_codepage_io_default):
        # GIVEN a cp1252-encoded file (what a Windows box wrote before the fix),
        path = tmp_path / "bare.txt"
        path.write_bytes(b"caf\xe9")

        # WHEN it is read with no encoding argument,
        with open(path) as f:
            text = f.read()

        # THEN the codepage is what gets used:
        assert text == "caf\u00e9"

    def test_fixture_makes_path_write_text_without_encoding_use_the_ansi_codepage(self, tmp_path, ansi_codepage_io_default):
        # The pathlib entry point: Path.write_text() resolves the interpreter
        # default via io.text_encoding() BEFORE Path.open is called, which is
        # how the fixture silently stopped covering it once (PR #167 review).
        path = tmp_path / "pathtext.txt"
        path.write_text("caf\u00e9")
        assert path.read_bytes() == b"caf\xe9"

    def test_fixture_makes_path_read_text_without_encoding_use_the_ansi_codepage(self, tmp_path, ansi_codepage_io_default):
        path = tmp_path / "pathtext.txt"
        path.write_bytes(b"caf\xe9")
        assert path.read_text() == "caf\u00e9"

    def test_fixture_makes_path_open_without_encoding_use_the_ansi_codepage(self, tmp_path, ansi_codepage_io_default):
        path = tmp_path / "pathopen.txt"
        with path.open("w") as f:
            f.write("caf\u00e9")
        assert path.read_bytes() == b"caf\xe9"

    def test_fixture_leaves_an_explicit_utf8_call_alone(self, tmp_path, ansi_codepage_io_default):
        # The fixture overrides the DEFAULT, never a pinned encoding — the app
        # pins UTF-8 everywhere, and that must stay UTF-8 under the emulation
        # (otherwise the encoding tests would be asserting nothing).
        path = tmp_path / "pinned.txt"
        path.write_text("caf\u00e9", encoding="utf-8")
        assert path.read_bytes() == b"caf\xc3\xa9"
        assert path.read_text(encoding="utf-8") == "caf\u00e9"

    def test_fixture_leaves_binary_modes_alone(self, tmp_path, ansi_codepage_io_default):
        # Binary I/O (audio, avatars) has no text encoding; injecting one would
        # raise TypeError, so those calls must pass through untouched.
        path = tmp_path / "binary.bin"
        with open(path, "wb") as f:
            f.write("caf\u00e9".encode("utf-8"))
        assert path.read_bytes() == b"caf\xc3\xa9"


# ---------------------------------------------------------------------------
# Poison guard: no text-mode file I/O in app/ may omit an explicit encoding
# ---------------------------------------------------------------------------

def _all_app_source_files():
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _call_needs_explicit_encoding(node: ast.Call) -> bool:
    """True when this call opens a text file and does not pin the encoding."""
    func = node.func
    if isinstance(func, ast.Name):
        if func.id != "open":
            return False
    elif isinstance(func, ast.Attribute):
        # Path.open / Path.read_text / Path.write_text / io.open.
        if func.attr not in {"open", "read_text", "write_text", "open_code"}:
            return False
        if func.attr == "open_code":  # always binary
            return False
    else:
        return False

    if any(kw.arg == "encoding" for kw in node.keywords):
        return False

    # Binary modes legitimately have no text encoding. Anything whose mode is
    # not a literal is treated as text, so an indirect call still has to pin it.
    mode_literal = None
    if len(node.args) > 1:
        mode_literal = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode_literal = kw.value
    if isinstance(mode_literal, ast.Constant) and isinstance(mode_literal.value, str):
        return "b" not in mode_literal.value
    return True


def test_no_module_in_app_opens_a_text_file_without_an_explicit_encoding():
    """Every text-mode open() in app/ must pass encoding= explicitly.

    The default is the interpreter's locale encoding, which is UTF-8 only by
    luck on Linux/macOS — see app/services/yaml_io.py for the Windows story.
    A new call site that forgets it fails here rather than on a user's
    machine, exactly like the mimetypes poison guard in tests/test_mime.py.
    """
    offenders = []
    for source_path in _all_app_source_files():
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_needs_explicit_encoding(node):
                offenders.append(f"{source_path.relative_to(APP_ROOT.parent)}:{node.lineno}")

    assert offenders == [], (
        "text-mode file I/O without encoding='utf-8' (locale-dependent — "
        f"cp1252 on Windows): {offenders}"
    )


def test_yaml_file_paths_are_read_only_through_yaml_io():
    """Nothing in app/ may open a *.yaml path itself; it must call yaml_io.

    The four config loaders used to open their YAML files directly (that is
    how issue #166 happened). Centralizing them is only durable if a new
    direct open() cannot sneak back in, so grep the syntax trees for a string
    literal ending in '.yaml' next to an open()/read_text()/write_text() call.
    """
    offenders = []
    for source_path in _all_app_source_files():
        if source_path.name == "yaml_io.py":
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                continue
            name = (
                node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            )
            if name not in {"open", "read_text", "write_text"}:
                continue
            for literal in ast.walk(node):
                if (
                    isinstance(literal, ast.Constant)
                    and isinstance(literal.value, str)
                    and literal.value.endswith(".yaml")
                ):
                    offenders.append(
                        f"{source_path.relative_to(APP_ROOT.parent)}:{node.lineno} -> {literal.value}"
                    )

    assert offenders == [], f"YAML opened outside app/services/yaml_io.py: {offenders}"


def test_yaml_io_helper_names_are_stable_for_the_guards():
    # The guards above talk about yaml_io by name; if the helpers are ever
    # renamed, the coverage map in AGENTS.md must be updated with them.
    assert callable(yaml_io.load_yaml_file)
    assert callable(yaml_io.save_yaml_file)
    assert yaml_io.CONFIG_FILE_ENCODING == "utf-8"
