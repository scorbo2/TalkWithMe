"""Tests for app/services/mime.py — the shared deterministic MIME -> extension helper.

This module is the single source of truth for turning a MIME type into a
file extension. It is used by app/persistence.py (on-disk audio filenames)
and app/services/stt_client.py (the STT multipart part name).

These tests pin the rule table (the "unit" level). The per-caller "dot"
formatting and the end-to-end filename assertions live with their
integrations: tests/test_persistence.py (dotted, ".bin" fallback) and
tests/test_tts_stt_clients.py (bare, "bin" fallback).
"""

import re

import pytest

from app.services import mime


class TestMimeToExtension:
    # Note: the parameter is named mime_type on purpose — a parameter named
    # "mime" would shadow the imported app.services.mime module itself.
    @pytest.mark.parametrize("mime_type,expected", [
        # The extension must derive from the MIME subtype deterministically —
        # never from the host OS's mime database (mimetypes.guess_extension
        # answers per platform: .weba for audio/webm on macOS, nothing on
        # most Linux boxes, .oga for audio/ogg on some).
        ("audio/webm", "webm"),
        ("audio/we\\bm", "webm"),  # malformed subtype: strip Windows path separator
        ("audio/ogg", "ogg"),
        ("audio/wav", "wav"),
        ("audio/x-wav", "wav"),       # vendor "x-" prefix is not an extension
        ("audio/mpeg", "mp3"),        # raw subtype would be a bad file hint
        ("audio/utterly-unknown", "utterly-unknown"),  # subtype fallback
        ("application/problem+json", "problem"),  # "+suffix" is a syntax marker, not an extension
        ("audio/ogg;rate=44100", "ogg"),  # parameters stripped
        # Garbage input falls back to "bin" and must never yield a path
        # separator — the result is interpolated into a filename.
        ("garbage-no-slash", "bin"),
        ("", "bin"),
        ("audio/", "bin"),
        ("audio/x-", "bin"),
        ("application/+json", "bin"),
        ("a/b/c", "c"),               # multi-slash: last component only
    ])
    def test_mime_to_extension(self, mime_type, expected):
        assert mime.mime_to_extension(mime_type) == expected

    def test_mime_to_extension_none_input_returns_bin(self):
        # None is a legitimate input (persistence's mime may be None). The
        # fallback is the bare "bin"; each caller adds its own leading dot.
        assert mime.mime_to_extension(None) == "bin"

    def test_never_consults_host_mimetypes_database(self, monkeypatch):
        # Regression guard for the macOS '.weba' bug report: if a future
        # change reintroduces mimetypes.guess_extension() (or any other
        # host-mime-database lookup) into the derivation, this poison makes
        # it fail loudly on every platform — not just the ones whose
        # database happens to disagree with the deterministic rule.
        def poison(*args, **kwargs):
            raise AssertionError("must not consult the host OS's mime database")

        monkeypatch.setattr("mimetypes.guess_extension", poison)

        assert mime.mime_to_extension("audio/webm") == "webm"


# The extension alphabet is exported (EXTENSION_SAFE_CHARS /
# EXTENSION_SAFE_RE_CLASS) so that consumers which VALIDATE a filename this
# module named — app/persistence.py's clip-name allowlist — can import it
# instead of re-guessing it. These tests are what make that export trustworthy:
# the sanitizer's real output set, the exported set, and the exported regex
# class must stay one fact in three clothes.
class TestExtensionSafeCharsetExport:
    @pytest.mark.parametrize("mime_type,expected", [
        # There is NO "an extension starts with a letter" rule here, and there
        # must never be one implicitly: callers that assume it 404 real files.
        ("audio/_foo", "_foo"),
        ("audio/-foo", "-foo"),
        ("audio/_", "_"),
        ("audio/-", "-"),
        ("audio/x-_weird", "_weird"),  # vendor prefix stripped, leading "_" kept
        ("audio/__proto__", "__proto__"),
    ])
    def test_mime_to_extension_can_emit_an_extension_starting_with_underscore_or_hyphen(
        self, mime_type, expected
    ):
        # GIVEN a MIME subtype whose sanitizer-surviving form starts with "_"/"-",
        # THEN the extension keeps that character: the sanitizer filters the
        # character SET, never positions.
        assert mime.mime_to_extension(mime_type) == expected

    @pytest.mark.parametrize("mime_type", [
        "audio/wav", "audio/webm;codecs=opus", "audio/x-m4a", "audio/3gpp",
        "audio/vnd.wave", "audio/_foo", "audio/-", "audio/|weird;chars|",
        "a/b/c", "audio/_\x00", "garbage", None,
    ])
    def test_every_extension_the_sanitizer_can_emit_matches_the_exported_class(self, mime_type):
        # GIVEN any input at all: whatever mime_to_extension() returns MUST be
        # matched by the class it exports. A gap here is the exact shape of the
        # persistence allowlist bug — a name a writer produces that a validator
        # refuses.
        extension = mime.mime_to_extension(mime_type)
        assert re.fullmatch(mime.EXTENSION_SAFE_RE_CLASS + "+", extension) is not None, extension

    def test_exported_regex_class_accepts_the_exported_chars_and_nothing_else(self):
        # GIVEN the two exported views of the alphabet,
        # THEN they agree character by character over the whole printable ASCII
        # range — the regex class is derived from the set, so a hand-edit of
        # either one that makes them disagree fails here naming the character.
        for codepoint in range(32, 127):
            char = chr(codepoint)
            in_set = char in mime.EXTENSION_SAFE_CHARS
            # Single character, so the bare class (no quantifier) is the probe.
            in_class = re.fullmatch(mime.EXTENSION_SAFE_RE_CLASS, char) is not None
            assert in_set == in_class, f"{char!r}: set={in_set} class={in_class}"

    def test_exported_set_is_the_documented_alphabet(self):
        # GIVEN the alphabet itself,
        # THEN it is exactly lowercase alphanumerics plus "_"/"-". Widening it
        # is allowed (the allowlist consumers follow automatically) but must be
        # a deliberate, named decision rather than a silent drift.
        assert mime.EXTENSION_SAFE_CHARS == frozenset(
            "abcdefghijklmnopqrstuvwxyz0123456789_-"
        )
