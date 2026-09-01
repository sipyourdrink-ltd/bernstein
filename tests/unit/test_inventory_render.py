"""Deterministic inventory topology render (#5133).

``test_same_store_same_bytes`` is the proof the issue asked for: two renders
of the same store compare equal as bytes. Written first; it failed on ``main``
with ``ModuleNotFoundError`` (an ``ImportError``) before ``inventory_render``
existed.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.govern.inventory_render import render_inventory

FIXTURE_STORE = Path(__file__).resolve().parents[1] / "fixtures" / "govern" / "inventory-store.json"
DOCS_DIAGRAM = Path(__file__).resolve().parents[2] / "docs" / "diagrams" / "inventory_topology.md"


def _store() -> dict[str, object]:
    loaded: object = json.loads(FIXTURE_STORE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_same_store_same_bytes() -> None:
    store = _store()
    first = render_inventory(store, "mermaid")
    second = render_inventory(store, "mermaid")
    assert first.encode("utf-8") == second.encode("utf-8")
    nodes = store["nodes"]
    edges = store["edges"]
    assert isinstance(nodes, list) and isinstance(edges, list)
    shuffled = {"nodes": list(reversed(nodes)), "edges": list(reversed(edges))}
    assert render_inventory(shuffled, "mermaid").encode("utf-8") == first.encode("utf-8")
    assert render_inventory(store, "dot").encode("utf-8") == render_inventory(shuffled, "dot").encode("utf-8")


def test_mermaid_is_flowchart_td_defaults_only() -> None:
    output = render_inventory(_store(), "mermaid")
    assert output.startswith("flowchart TD")
    assert "classDef" not in output
    assert 'agent_claude["Claude Code"]' in output
    assert "host_dev --> agent_claude" in output
    # Sorted: agent_claude before host_dev, regardless of fixture list order.
    assert output.index("agent_claude") < output.index("host_dev")


def test_committed_mermaid_matches_fixture_store() -> None:
    """CI gate: the docs diagram is the mermaid render of the fixture store."""
    mermaid = render_inventory(_store(), "mermaid")
    committed = DOCS_DIAGRAM.read_text(encoding="utf-8")
    assert f"```mermaid\n{mermaid}\n```" in committed, (
        "docs/diagrams/inventory_topology.md drifted from the fixture store. "
        "Regenerate the mermaid fence from render_inventory(fixture, 'mermaid')."
    )
