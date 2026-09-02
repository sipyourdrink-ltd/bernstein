"""Render an inventory topology graph from a store.

Issue #5133. The store is a JSON object with ``nodes`` and ``edges`` — a
hand-written fixture until the entity-per-file store (#5129) lands. Walking
those two lists, sorted, is the whole job: the same store produces the same
bytes. Layout and styling stay at Mermaid / DOT defaults.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

# Shared cast-type aliases to avoid string duplication (Sonar S1192), matching
# ``graph_cmd.py``.
type _CAST_LIST_DICT_STR_ANY = list[dict[str, Any]]


def load_inventory_store(path: Path) -> dict[str, Any]:
    """Read a graph JSON file (``nodes`` + ``edges``)."""
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"inventory store {path} must be a JSON object")
    return raw


def render_inventory(store: Mapping[str, Any], fmt: str) -> str:
    """Render *store* as ``mermaid`` or ``dot``.

    Nodes sort by ``id``, edges by ``(from, to)``. *fmt* is matched
    case-insensitively.
    """
    key = fmt.lower()
    if key == "mermaid":
        return _render_mermaid_graph(store)
    if key == "dot":
        return _render_dot_graph(store)
    raise ValueError(f"unsupported render format: {fmt!r} (want mermaid or dot)")


def _nodes(store: Mapping[str, Any]) -> _CAST_LIST_DICT_STR_ANY:
    nodes = store.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("inventory store nodes must be a list")
    typed = [node for node in nodes if isinstance(node, dict)]
    return sorted(typed, key=lambda node: str(node.get("id", "")))


def _edges(store: Mapping[str, Any]) -> _CAST_LIST_DICT_STR_ANY:
    edges = store.get("edges", [])
    if not isinstance(edges, list):
        raise ValueError("inventory store edges must be a list")
    typed = [edge for edge in edges if isinstance(edge, dict)]
    return sorted(
        typed,
        key=lambda edge: (_edge_end(edge, "from", "source"), _edge_end(edge, "to", "target")),
    )


def _edge_end(edge: dict[str, Any], primary: str, alias: str) -> str:
    return str(edge.get(primary) or edge.get(alias) or "")


def _mermaid_node_map(nodes: _CAST_LIST_DICT_STR_ANY) -> dict[str, str]:
    """Map store ids → positional mermaid ids (``n0``, ``n1``, …).

    Sanitising punctuation to ``_`` would collide (``host-dev`` vs ``host.dev``).
    Positional ids stay deterministic after the sorted ``_nodes`` walk and cannot
    collide; the store id stays in the node label.
    """
    return {str(node.get("id", "")): f"n{index}" for index, node in enumerate(nodes)}


def _mermaid_label(node: dict[str, Any]) -> str:
    raw_id = str(node.get("id", ""))
    friendly = str(node.get("label") or raw_id or "?")
    text = friendly if friendly == raw_id else f"{raw_id}: {friendly}"
    return text.replace('"', "'")


def _render_mermaid_graph(store: Mapping[str, Any]) -> str:
    """Render the inventory graph as Mermaid flowchart markup.

    Shape copied from ``graph_cmd._render_mermaid_graph`` (flowchart TD,
    ``id["label"]``, ``from --> to``). No classDef / styling — defaults only.
    Mermaid node ids are positional (``n0``…) so punctuation in store ids
    cannot collapse distinct nodes.
    """
    nodes = _nodes(store)
    ids = _mermaid_node_map(nodes)
    lines = ["flowchart TD"]
    for node in nodes:
        raw_id = str(node.get("id", ""))
        lines.append(f'    {ids[raw_id]}["{_mermaid_label(node)}"]')
    for edge in _edges(store):
        src = ids.get(_edge_end(edge, "from", "source"), "n_missing")
        dst = ids.get(_edge_end(edge, "to", "target"), "n_missing")
        lines.append(f"    {src} --> {dst}")
    return "\n".join(lines)


def _render_dot_graph(store: Mapping[str, Any]) -> str:
    """Render the inventory graph as Graphviz DOT, defaults only."""
    lines = ["digraph inventory {"]
    for node in _nodes(store):
        node_id = str(node.get("id", ""))
        label = str(node.get("label") or node_id or "?")
        lines.append(f"    {_dot_string(node_id)} [label={_dot_string(label)}];")
    for edge in _edges(store):
        src = _edge_end(edge, "from", "source")
        dst = _edge_end(edge, "to", "target")
        lines.append(f"    {_dot_string(src)} -> {_dot_string(dst)};")
    lines.append("}")
    return "\n".join(lines)


def _dot_string(raw: str) -> str:
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


__all__ = [
    "load_inventory_store",
    "render_inventory",
]
