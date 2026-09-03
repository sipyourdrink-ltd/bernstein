"""Every Bernstein-owned JSON Schema is published at its ``$id``.

``$id`` is the canonical retrieval URI of a schema; a validator that
dereferences it must find the same document. The schemas under ``schemas/``
are served from https://bernstein.run/schemas/<file name>, so each id has to
name exactly that location. Vendored third-party schemas keep their own ids.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
PUBLISHED_BASE = "https://bernstein.run/schemas/"
UNSERVED_HOSTS = ("bernstein.alexchernysh.com", "bernstein.dev")


def _bernstein_schemas() -> list[Path]:
    paths = sorted(SCHEMAS.glob("*.json"))
    assert paths, "no schemas found"
    return paths


def test_no_schema_id_points_at_an_unserved_host() -> None:
    for path in _bernstein_schemas():
        schema_id = json.loads(path.read_text(encoding="utf-8")).get("$id", "")
        assert not any(host in schema_id for host in UNSERVED_HOSTS), (
            f"{path.name}: $id {schema_id!r} names a host that serves nothing"
        )


def test_bernstein_schema_ids_match_their_published_path() -> None:
    owned = 0
    for path in _bernstein_schemas():
        schema_id = json.loads(path.read_text(encoding="utf-8")).get("$id", "")
        if "bernstein.run" not in schema_id:
            continue  # vendored schema with a third-party id
        owned += 1
        assert schema_id == PUBLISHED_BASE + path.name, (
            f"{path.name}: $id {schema_id!r} does not match its published path"
        )
    assert owned >= 6, f"expected the Bernstein schemas to carry bernstein.run ids, found {owned}"
