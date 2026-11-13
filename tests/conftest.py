"""Shared fixtures: keep tests hermetic.

The app leans on module-level globals (config caches, a session singleton,
a fixed persistence root, an MCP tool cache). This autouse fixture points
all of them at throwaway state under pytest's tmp_path so no test ever
reads or writes the real settings.yaml / personas.yaml / chatrooms.yaml /
chatrooms/ data.
"""

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of pytest's invocation cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.config as app_config
import app.persistence as persistence
import app.routers.persistence as persistence_router
import app.services.llm as llm_module
import app.services.llm_auth as llm_auth
import app.services.tool_registry as tool_registry
import app.services.tts_client as tts_client
from app.session import session as global_session

from tests.factories import make_chatrooms, make_personas, make_settings


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch):
    """Point every global at throwaway state; restore everything after."""
    # All YAML writes (save_settings/save_chatrooms) land here; persona
    # writes go to the Personas directory resolved from this root.
    monkeypatch.setattr(app_config, "_PROJECT_ROOT", tmp_path)
    # All chatroom history/audio files land here.
    monkeypatch.setattr(persistence, "_PERSISTENCE_ROOT", tmp_path / "chatrooms")
    # The persistence router imported _PERSISTENCE_ROOT by value at import
    # time, so it needs its own patch to stay in sync.
    monkeypatch.setattr(persistence_router, "_PERSISTENCE_ROOT", tmp_path / "chatrooms")

    # Fresh config caches (the real YAML files are never read).
    monkeypatch.setattr(app_config, "_settings_cache", make_settings())
    monkeypatch.setattr(app_config, "_personas_cache", make_personas())
    monkeypatch.setattr(app_config, "_chatrooms_cache", make_chatrooms())

    # Module-level registries that survive across tests.
    persistence._pending_audio.clear()
    tool_registry.reset()
    # The TTS capabilities cache (single slot, docs and failures alike):
    # a doc cached by one test must not leak into the next.
    tts_client.invalidate_capabilities()
    # The LLM API key: never read the real llm_api_key file or the
    # developer's TALKWITHME_LLM_API_KEY, and never let a key cached by an
    # earlier test leak into the next one.
    monkeypatch.setattr(llm_auth, "_PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(llm_auth.ENV_VAR_NAME, raising=False)
    llm_auth.invalidate_llm_api_key()
    # The once-per-URL cleartext warning dedupe in llm.py must not survive
    # a test boundary.
    llm_module._warned_plaintext_urls.clear()

    # The global session singleton: start every test clean.
    global_session._history.clear()
    global_session._active_personas.clear()
    global_session.set_current_room("default")

    yield

    persistence._pending_audio.clear()
    tool_registry.reset()
    tts_client.invalidate_capabilities()
    llm_auth.invalidate_llm_api_key()
    llm_module._warned_plaintext_urls.clear()
    global_session._history.clear()
    global_session._active_personas.clear()
    global_session.set_current_room("default")


@pytest.fixture
def tmp_project_root(tmp_path) -> Path:
    """The tmp directory standing in for the project root (YAML files)."""
    return tmp_path


@pytest.fixture
def ansi_codepage_io_default(monkeypatch):
    """Make text-mode I/O that omits `encoding=` behave like a Windows box.

    Issue #166: the interpreter's default text encoding is the *locale* —
    UTF-8 on Linux/macOS, the ANSI codepage (cp1252 on a Western Windows
    install) elsewhere. Every config file here is written with
    allow_unicode=True, i.e. non-ASCII stored raw, so a loader or saver that
    forgets to pin the encoding either raises (bytes the codepage leaves
    undefined) or — the nastier case — silently decodes "Björk" as "BjÃ¶rk"
    and feeds that to the UI, the router and the LLM.

    Injecting cp1252 as the default is how a Windows run would have behaved,
    so this fixture turns that whole bug class into a loud failure on any
    runner. Binary modes are passed through untouched: they legitimately have
    no text encoding (and injecting one would raise TypeError).

    Three entry points need covering, because a caller does not always reach
    open() with the encoding left out (a single `setdefault` on the kwargs
    missed the pathlib half of the suite entirely — Copilot caught that on
    PR #167):
      * `open(...)` with no `encoding=` keyword -> injected below;
      * `Path.read_text()` / `Path.write_text()` — CPython 3.11+ resolves the
        default with io.text_encoding() BEFORE Path.open is called, so the
        keyword arrives already filled in with the runner's real encoding.
        io.text_encoding gets patched so that resolution yields cp1252;
      * any caller passing `encoding=None` explicitly (the pre-3.11 pathlib
        shape, and hand-written calls) -> the None check below.

    New tests that read or write a config/persona file should use this if
    they care about the encoding invariant; the app itself routes all YAML I/O
    through app/services/yaml_io.py, which pins UTF-8.

    The fixture's own teeth are pinned by TestAnsiCodepageFixtureIsReal in
    tests/test_yaml_io.py: if a Python upgrade ever moves default-encoding
    resolution somewhere this fixture cannot reach, THAT test fails — instead
    of every encoding test quietly proving nothing.
    """
    import builtins
    import io

    real_open = builtins.open
    ansi_codepage = "cp1252"

    def open_with_codepage_default(*args, **kwargs):
        mode = kwargs.get("mode") or (args[1] if len(args) > 1 else "r")
        if "b" in mode:
            return real_open(*args, **kwargs)
        # An absent key and an explicit None mean the same thing: "use the
        # interpreter default" — exactly what is being overridden here.
        if kwargs.get("encoding") is None:
            kwargs["encoding"] = ansi_codepage
        return real_open(*args, **kwargs)

    # builtins.open covers `open(...)`; Path.open covers `path.open(...)`.
    monkeypatch.setattr(builtins, "open", open_with_codepage_default)
    monkeypatch.setattr(Path, "open", open_with_codepage_default)
    # Path.read_text()/write_text() resolve the default through io.text_encoding
    # before Path.open is ever reached, so patching Path.open alone leaves them
    # on the runner's real locale. (io.text_encoding does not exist before
    # 3.11; there those methods pass encoding=None down to Path.open, which the
    # None check above handles.)
    if hasattr(io, "text_encoding"):
        def text_encoding_with_codepage(encoding=None):
            # None is the pre-3.15 "use the default" argument, "locale" is the
            # PEP 686 (3.15+) one. Both mean the same thing here — and both
            # must resolve to the codepage, or the emulation goes soft.
            if encoding is None or encoding == "locale":
                return ansi_codepage
            return encoding

        monkeypatch.setattr(io, "text_encoding", text_encoding_with_codepage)


@pytest.fixture
def persistence_root(tmp_path) -> Path:
    """The tmp directory standing in for the chatrooms/ persistence root."""
    return tmp_path / "chatrooms"


@pytest.fixture
def client():
    """FastAPI TestClient WITHOUT the startup lifespan.

    Deliberate: the lifespan re-reads the real YAML files (clobbering the
    test config caches) and attempts MCP discovery. Tests that exercise the
    lifespan do so explicitly (see test_main.py).
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def raw_asgi_get():
    """Run a GET against the app with an ASGI scope built the way uvicorn does.

    httpx (the TestClient's transport) normalizes dot segments and
    re-encodes the URL client-side, so a traversal URL can never reach the
    handler through a normal request. uvicorn instead sets scope["path"]
    to the percent-decoded target and scope["raw_path"] to the raw bytes —
    that combination is what makes "..%2Fx" a traversal at all. This
    helper reproduces uvicorn's scope construction (h11_impl.py) exactly,
    the receive() channel included (see below).
    """
    import asyncio

    from app.main import app

    def _raw_asgi_get(path: str, raw_path: bytes | None = None):
        scope = {
            "type": "http",
            # spec_version 2.3 is what uvicorn actually sends, and it is not
            # cosmetic: below ASGI 2.4 a FileResponse streams the file in a
            # sibling task while this channel is watched for "http.disconnect"
            # (starlette/responses.py, FileResponse.__call__). Omitting the key
            # takes the same branch (it defaults to "2.0"), but naming it keeps
            # the fake honest about which code path the app is exercising.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": (raw_path if raw_path is not None else path.encode("ascii")),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        # The two halves of the receive() channel, in order:
        #   1. the request body — a GET has a single empty, complete message;
        #   2. then the connection sits open until the response is finished,
        #      and ONLY then does the server report "http.disconnect".
        # That is uvicorn's blocking receive_queue.get(), and Starlette's own
        # TestClient honours the same contract (it awaits response_complete
        # before disconnecting). Skipping step 2 — returning a body message
        # immediately on every call — starves the sibling streaming task and
        # never disconnects, so FileResponse spins forever: the suite hung on
        # the first audio file served through this helper. Starlette < 1.x had
        # no disconnect watch at all, which is why this only bites now that
        # the unpinned starlette dependency has drifted to 1.x.
        request_complete = False
        # Signalled by the LAST response body message: a FileResponse larger
        # than its chunk size sends several with more_body=True, and claiming
        # completion earlier would cancel the transfer mid-file.
        response_complete = asyncio.Event()

        async def receive():
            nonlocal request_complete
            if not request_complete:
                request_complete = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await response_complete.wait()
            return {"type": "http.disconnect"}

        result = {"status": None, "body": b""}

        async def send(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
            elif message["type"] == "http.response.body":
                result["body"] += message.get("body", b"")
                if not message.get("more_body", False):
                    response_complete.set()

        asyncio.run(app(scope, receive, send))
        return result["status"], result["body"]

    return _raw_asgi_get
