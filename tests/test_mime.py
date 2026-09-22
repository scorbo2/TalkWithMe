"""Tests for app/services/mime.py — the shared deterministic MIME -> extension helper.

This module is the single source of truth for turning a MIME type into a
file extension. It is used by app/persistence.py (on-disk audio filenames)
and app/services/stt_client.py (the STT multipart part name).

These tests pin the rule table (the "unit" level). The per-caller "dot"
formatting and the end-to-end filename assertions live with their
integrations: tests/test_persistence.py (dotted, ".bin" fallback) and
tests/test_tts_stt_clients.py (bare, "bin" fallback).
"""

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
