"""The two copies of the four tts-serve capabilities snapshots must stay in
lockstep.

pytest uses the copies embedded in tests/factories.py
(``_CAPABILITIES_SNAPSHOTS``); the plain-Node suite (tests/test_tts_settings.js)
uses the JSON files in tests/fixtures/. Both were copied verbatim from
tts-serve/impl/tests/snapshots/, so if one changes (an upstream snapshot
bump) the other must change in the same commit — otherwise the two suites
silently test two different protocols. This test fails loudly on drift.
"""

import json
from pathlib import Path

from tests.factories import _CAPABILITIES_SNAPSHOTS

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_ENGINE_TO_FIXTURE_FILE = {
    "chatterbox": "chatterbox_capabilities.json",
    "dots.tts": "dots_capabilities.json",
    "omnivoice": "omnivoice_capabilities.json",
    "qwen3-tts": "qwen3_capabilities.json",
}


def test_fixture_json_files_match_factory_snapshots():
    # GIVEN the four factory snapshots and their JSON fixture twins:
    # WHEN each fixture file is parsed:
    # THEN it is exactly the factory's copy (dict equality — order-independent
    # but value-complete, in both directions):
    for engine, filename in _ENGINE_TO_FIXTURE_FILE.items():
        fixture_doc = json.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
        assert fixture_doc == _CAPABILITIES_SNAPSHOTS[engine], (
            f"tests/fixtures/{filename} has drifted from tests/factories.py's "
            f"'{engine}' snapshot — re-copy both verbatim from "
            "tts-serve/impl/tests/snapshots/ in the same commit"
        )


def test_factory_snapshot_engines_are_exactly_the_fixture_set():
    # GIVEN the engine keys in both sources:
    # WHEN the sets are compared:
    # THEN they match exactly — a fifth engine added to only one side (or a
    # key renamed) must not pass silently:
    assert set(_CAPABILITIES_SNAPSHOTS) == set(_ENGINE_TO_FIXTURE_FILE)
