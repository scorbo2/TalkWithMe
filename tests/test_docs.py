"""Docs consistency: AGENTS.md's API table must match the registered routes.

The endpoint table is hand-maintained next to the code, so it drifts
silently (it has, historically). This test extracts the real /api/ routes
from the FastAPI app and compares them against the markdown table, failing
loudly if either set diverges. Route metadata (methods, paths) is static
at import time, so no lifespan, config, or network access is needed.
"""

import re
from collections import Counter
from pathlib import Path

from app.main import app

AGENTS_MD = Path(__file__).resolve().parent.parent / "AGENTS.md"

# Starlette auto-registers HEAD (for GET routes) and OPTIONS; they are not
# part of the documented API surface.
_IGNORED_METHODS = {"HEAD", "OPTIONS"}


def _actual_api_routes() -> set[tuple[str, str]]:
    """(METHOD, path) pairs exposed by the app, scoped to /api/ routes.

    Derived from the OpenAPI schema rather than app.routes: modern
    Starlette keeps included routers as lazy wrapper nodes in app.routes,
    so a flat scan no longer enumerates them, and the schema is the public
    surface the table actually documents. (It is also what HEAD/OPTIONS
    filtering falls out of for free — the schema lists neither.)
    Scoped to /api/ on purpose: the table documents the API, not the UI
    (GET /) or the /static mount.
    """
    routes = set()
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method in operations:
            if method.upper() in _IGNORED_METHODS:
                continue
            routes.add((method.upper(), path))
    return routes


def _documented_api_routes() -> list[tuple[str, str]]:
    """(METHOD, path) pairs from the '## API endpoints' table in AGENTS.md.

    Returns a list (not a set) so the test can also detect duplicate rows.
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    match = re.search(
        r"^## API endpoints\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "AGENTS.md is missing the '## API endpoints' section"

    rows = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        method, path = cells[0], cells[1]
        # Skip the header row and the |---|---|---| separator row.
        if method.upper() == "METHOD" or set(method) <= {"-"}:
            continue
        method = method.strip("`").upper()
        # The table annotates one endpoint with an illustrative query string
        # (?room=<room>); route paths carry no query part.
        path = path.strip("`").split("?", 1)[0]
        rows.append((method, path))

    assert rows, "The AGENTS.md '## API endpoints' table contains no data rows"
    return rows


def test_agents_md_api_endpoints_table_matches_registered_routes():
    actual = _actual_api_routes()
    documented_rows = _documented_api_routes()
    documented = set(documented_rows)

    duplicates = sorted(r for r, count in Counter(documented_rows).items() if count > 1)
    assert not duplicates, f"Duplicate rows in the AGENTS.md API table: {duplicates}"

    missing_from_docs = sorted(actual - documented)
    assert not missing_from_docs, (
        "Routes registered on the app but missing from the AGENTS.md API table: "
        + ", ".join(f"{method} {path}" for method, path in missing_from_docs)
    )

    ghost_rows = sorted(documented - actual)
    assert not ghost_rows, (
        "Rows in the AGENTS.md API table that match no registered route: "
        + ", ".join(f"{method} {path}" for method, path in ghost_rows)
    )
