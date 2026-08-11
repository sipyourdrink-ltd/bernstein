"""Task attribution over a content-addressed code graph (#3237, scope steps 1-2).

Parallel admission decides whether two tasks may run at once. Today that
decision is made from task *descriptions*; nothing reads the code. This module
supplies the half that reads the code: a protocol for graph acquisition, and a
deterministic mapping from a task's declared paths to the set of symbols it can
reach.

Two properties carry the whole design, and both are load-bearing for the
verdict that will sit on top of this:

**Determinism.** Every output is a sorted tuple. ``SemanticGraph.neighborhood``
returns a ``set`` and, on truncation, ranks the overflow with
``sorted(extras, ...)`` over that set -- so equal-scoring nodes are ordered by
whatever the set happened to yield. That is fine for assembling prompt context,
which is what it exists for, and unusable for a decision that has to be
recomputed by a third party. The traversal here is separate for that reason.

**Provable coverage.** A neighbourhood is ``PROVEN`` only when every edge it
crossed was read directly out of the source. An edge produced by name
resolution may point at the wrong symbol, so a boundary that rests on one is
not a boundary. Anything else is ``UNPROVEN`` and carries the reason, which is
the signal a scheduler turns into "run these serially".

The failure this exists to prevent is a graph with silent coverage holes
producing a confident "disjoint" answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.core.knowledge.ast_symbol_graph import (
    EDGE_ORIGIN_EXTRACTED,
    SemanticGraph,
    graph_digest,
    graph_document,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "ATTRIBUTION_PROVEN",
    "ATTRIBUTION_UNPROVEN",
    "DEFAULT_NEIGHBORHOOD_DEPTH",
    "REASON_INDEX_TRUNCATED",
    "REASON_INFERRED_EDGE",
    "REASON_PATH_NOT_INDEXED",
    "CodeGraph",
    "SemanticCodeGraph",
    "TaskNodeSet",
    "attribute_task",
]

#: Hops to expand from a task's declared symbols. One hop covers a task's own
#: symbols plus everything that calls them or that they call, which is the
#: boundary a merge conflict actually follows. Raise it and the neighbourhoods
#: grow fast enough that nothing is ever disjoint; the knob exists so that can
#: be measured rather than argued about.
DEFAULT_NEIGHBORHOOD_DEPTH = 1

#: Every edge on the boundary was read directly from source. Only this verdict
#: may back a claim that two tasks do not overlap.
ATTRIBUTION_PROVEN = "PROVEN"

#: The boundary could not be established from directly-extracted edges alone.
#: Not an error -- the caller serialises the task instead of refusing it.
ATTRIBUTION_UNPROVEN = "UNPROVEN"

#: A declared path contributed no symbols: not indexed, not Python, or it
#: failed to parse. The task may touch code the graph never saw.
REASON_PATH_NOT_INDEXED = "path_not_indexed"

#: Expansion crossed an edge produced by name resolution rather than read from
#: source, so the neighbourhood may be attributed to the wrong symbol.
REASON_INFERRED_EDGE = "inferred_edge"

#: The index itself hit its file cap, so the graph is missing edges it has no
#: way to know about. Nothing derived from it can be proven complete.
REASON_INDEX_TRUNCATED = "index_truncated"


@runtime_checkable
class CodeGraph(Protocol):
    """A content-addressed view of the workspace's symbols and their edges.

    Kept narrow deliberately: an implementation has to be content-addressable
    and has to say how each edge was obtained. Anything that cannot do both
    cannot back an admission verdict, so there is no point admitting it behind
    the protocol.
    """

    def digest(self) -> str:
        """Return the ``sha256:``-prefixed digest of :meth:`document`."""
        ...

    def document(self) -> bytes:
        """Return the canonical serialisation this digest was taken over."""
        ...

    def symbols_for_path(self, path: str) -> tuple[str, ...]:
        """Return the symbol ids defined in *path*, sorted. Empty if unknown."""
        ...

    def extracted_neighbors(self, symbol_id: str) -> tuple[str, ...]:
        """Return neighbours reachable over directly-extracted edges, sorted."""
        ...

    def has_inferred_edge(self, symbol_id: str) -> bool:
        """Whether any edge touching *symbol_id* was produced by resolution."""
        ...

    def is_truncated(self) -> bool:
        """Whether the index dropped files it enumerated."""
        ...


@dataclass(frozen=True)
class SemanticCodeGraph:
    """:class:`CodeGraph` over the in-repo AST symbol graph.

    Chosen over an external indexer because this one already carries the
    per-edge origin the verdict depends on, and adding a dependency to obtain a
    property we already have would trade a determinism guarantee for a
    changelog we do not control.
    """

    graph: SemanticGraph

    def digest(self) -> str:
        return graph_digest(self.graph)

    def document(self) -> bytes:
        return graph_document(self.graph)

    def symbols_for_path(self, path: str) -> tuple[str, ...]:
        return tuple(sorted(self.graph.file_symbols.get(path, [])))

    def extracted_neighbors(self, symbol_id: str) -> tuple[str, ...]:
        neighbors = {
            edge.target if edge.source == symbol_id else edge.source
            for edge in self._edges_touching(symbol_id)
            if edge.origin == EDGE_ORIGIN_EXTRACTED
        }
        neighbors.discard(symbol_id)
        return tuple(sorted(neighbors))

    def has_inferred_edge(self, symbol_id: str) -> bool:
        return any(edge.origin != EDGE_ORIGIN_EXTRACTED for edge in self._edges_touching(symbol_id))

    def is_truncated(self) -> bool:
        return self.graph.indexed_file_count < self.graph.source_file_count

    def _edges_touching(self, symbol_id: str) -> list:  # type: ignore[type-arg]
        return [e for e in self.graph.edges if e.source == symbol_id or e.target == symbol_id]


@dataclass(frozen=True)
class TaskNodeSet:
    """One task's attributed symbols and whether that attribution is provable.

    Attributes:
        task_id: The task this set belongs to.
        declared_paths: Paths the task said it owns, sorted.
        seed_symbols: Symbols defined in those paths, sorted.
        neighborhood: Seeds plus everything reachable within ``depth`` hops over
            directly-extracted edges, sorted. Always a superset of the seeds.
        depth: Hops the expansion used.
        verdict: :data:`ATTRIBUTION_PROVEN` or :data:`ATTRIBUTION_UNPROVEN`.
        reasons: Why the verdict is ``UNPROVEN``, sorted and deduplicated.
            Empty when proven.
    """

    task_id: str
    declared_paths: tuple[str, ...]
    seed_symbols: tuple[str, ...]
    neighborhood: tuple[str, ...]
    depth: int
    verdict: str
    reasons: tuple[str, ...]

    @property
    def proven(self) -> bool:
        """Whether this attribution may back a disjointness claim."""
        return self.verdict == ATTRIBUTION_PROVEN

    def to_dict(self) -> dict[str, object]:
        """Canonical mapping for the receipt projection."""
        return {
            "task_id": self.task_id,
            "declared_paths": list(self.declared_paths),
            "seed_symbols": list(self.seed_symbols),
            "neighborhood": list(self.neighborhood),
            "depth": self.depth,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }


def attribute_task(
    graph: CodeGraph,
    task_id: str,
    declared_paths: Iterable[str],
    *,
    depth: int = DEFAULT_NEIGHBORHOOD_DEPTH,
) -> TaskNodeSet:
    """Map *declared_paths* onto graph symbols and expand to the neighbourhood.

    The expansion is breadth-first over directly-extracted edges only, with
    every level sorted before it is walked, so the result depends on the graph
    and the inputs and on nothing else.

    A path that contributed no symbols does not fail: it produces an
    ``UNPROVEN`` verdict, because the task may touch code the graph never saw
    and silence is not evidence of absence. A task declaring no paths at all is
    ``UNPROVEN`` for the same reason.

    Args:
        graph: Content-addressed graph to attribute against.
        task_id: Identifier recorded on the result.
        declared_paths: Repository-relative paths the task owns.
        depth: Hops to expand. Must not be negative.

    Returns:
        The attribution, always with a verdict.

    Raises:
        ValueError: If *depth* is negative.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")

    paths = tuple(sorted({p for p in declared_paths if p}))
    reasons: set[str] = set()

    if graph.is_truncated():
        reasons.add(REASON_INDEX_TRUNCATED)

    seeds: set[str] = set()
    for path in paths:
        symbols = graph.symbols_for_path(path)
        if not symbols:
            reasons.add(REASON_PATH_NOT_INDEXED)
            continue
        seeds.update(symbols)

    if not paths:
        reasons.add(REASON_PATH_NOT_INDEXED)

    included = set(seeds)
    frontier = sorted(seeds)
    for _ in range(depth):
        next_frontier: list[str] = []
        for symbol_id in frontier:
            if graph.has_inferred_edge(symbol_id):
                # The boundary at this node rests on a guess, so the
                # neighbourhood's edge is not established. Expansion continues
                # over the extracted edges -- reporting a smaller set would
                # understate what the task reaches.
                reasons.add(REASON_INFERRED_EDGE)
            for neighbor in graph.extracted_neighbors(symbol_id):
                if neighbor not in included:
                    included.add(neighbor)
                    next_frontier.append(neighbor)
        if not next_frontier:
            break
        frontier = sorted(next_frontier)

    verdict = ATTRIBUTION_UNPROVEN if reasons else ATTRIBUTION_PROVEN
    return TaskNodeSet(
        task_id=task_id,
        declared_paths=paths,
        seed_symbols=tuple(sorted(seeds)),
        neighborhood=tuple(sorted(included)),
        depth=depth,
        verdict=verdict,
        reasons=tuple(sorted(reasons)),
    )
