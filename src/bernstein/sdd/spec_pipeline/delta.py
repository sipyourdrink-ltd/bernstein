"""Graph-delta computation for spec edits (issue #2361, AC3).

Because a task node's identity is content-addressed over the requirement it
implements and nothing else, recompiling after a spec edit produces a graph in
which every unedited requirement keeps its exact node id. The delta between old
and new graphs is therefore a plain set difference over node ids: only the
subgraph touching a changed requirement shows up as added / removed, and every
other node reports as unchanged (identity retained).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.sdd.spec_pipeline.compiler import TaskGraph

__all__ = ["GraphDelta", "graph_delta"]


@dataclass(frozen=True, slots=True)
class GraphDelta:
    """The difference between two compiled task graphs.

    Attributes:
        added: Node ids present only in the new graph.
        removed: Node ids present only in the old graph.
        unchanged: Node ids present in both (identity retained).
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        """True when no node was added or removed."""
        return not self.added and not self.removed


def graph_delta(old: TaskGraph, new: TaskGraph) -> GraphDelta:
    """Return the :class:`GraphDelta` from *old* to *new*.

    Node ids are compared as sets; results are sorted for deterministic output.
    """
    old_ids = set(old.node_ids())
    new_ids = set(new.node_ids())
    return GraphDelta(
        added=tuple(sorted(new_ids - old_ids)),
        removed=tuple(sorted(old_ids - new_ids)),
        unchanged=tuple(sorted(old_ids & new_ids)),
    )
