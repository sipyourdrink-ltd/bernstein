"""Compile stage: pure transformation from requirements to a task graph.

The compiler is a deterministic, model-free function (issue #2361, AC1). Given
the same approved :class:`RequirementSet` it always produces a byte-identical
:class:`TaskGraph`, hash-asserted via :attr:`TaskGraph.graph_hash`.

Every task node carries the content hashes of the requirement lines it
implements (AC2), so any artefact a node later produces traces back to spec
lines through lineage. Node identity is itself content-addressed over
``(title, role, requirement_hashes)`` and nothing else -- crucially not over
sibling nodes -- so editing one requirement changes only that node's identity
and leaves every unaffected node byte-identical across recompiles (AC3, via
:mod:`bernstein.sdd.spec_pipeline.delta`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.sdd.spec_pipeline.requirements import Requirement, RequirementSet

__all__ = [
    "CoverageResult",
    "TaskGraph",
    "TaskNode",
    "compile_requirements",
    "infer_role",
    "lineage_coverage",
]

_DEFAULT_ROLE = "backend"

# Deterministic keyword -> role table. First match in this fixed order wins so
# the mapping is stable and independent of dict iteration order.
_ROLE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("document", "docs"),
    ("readme", "docs"),
    ("test", "qa"),
    ("verify", "qa"),
    ("deploy", "devops"),
    ("infrastructure", "devops"),
    ("pipeline", "devops"),
    ("auth", "security"),
    ("encrypt", "security"),
    ("token", "security"),
    ("permission", "security"),
    ("ui", "frontend"),
    ("button", "frontend"),
    ("page", "frontend"),
    ("schema", "data"),
    ("database", "data"),
    ("migration", "data"),
)

_MAX_TITLE_LEN = 72


def infer_role(text: str) -> str:
    """Return a deterministic specialist role for requirement *text*.

    Keyword-driven and order-stable; falls back to ``backend`` when no keyword
    matches, so the mapping never depends on hash or iteration order.
    """
    lowered = text.lower()
    for keyword, role in _ROLE_KEYWORDS:
        if keyword in lowered:
            return role
    return _DEFAULT_ROLE


def _derive_title(text: str) -> str:
    """Return a single-line title derived from requirement *text*."""
    single = " ".join(text.split())
    if len(single) <= _MAX_TITLE_LEN:
        return single
    return single[: _MAX_TITLE_LEN - 1].rstrip() + "…"


def _node_id(*, title: str, role: str, requirement_hashes: tuple[str, ...]) -> str:
    """Return the content-addressed id of a task node.

    The pre-image is only the node's own content ``(title, role, sorted
    requirement hashes)`` -- never sibling nodes -- so a node's identity is a
    pure function of the requirement it implements.
    """
    preimage = json.dumps(
        {"title": title, "role": role, "requirement_hashes": sorted(requirement_hashes)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskNode:
    """One node of a compiled task graph.

    Attributes:
        node_id: Content-addressed id over ``(title, role, requirement_hashes)``.
        title: Human-readable task title derived from the requirement.
        role: Specialist role assigned by :func:`infer_role`.
        requirement_ids: Requirement ids this node implements.
        requirement_hashes: Content hashes of those requirements (the lineage
            anchor: every node carries at least one).
        depends_on: Node ids this node depends on (empty in v1 -- nodes are
            independently gated behind the single approval receipt).
    """

    node_id: str
    title: str
    role: str
    requirement_ids: tuple[str, ...]
    requirement_hashes: tuple[str, ...]
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "role": self.role,
            "requirement_ids": list(self.requirement_ids),
            "requirement_hashes": list(self.requirement_hashes),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class TaskGraph:
    """A deterministic task graph compiled from a requirement set.

    Attributes:
        nodes: Task nodes in requirement (document) order.
        requirement_set_hash: The approved set hash this graph was compiled
            from.
        graph_hash: Content hash over the ordered node ids plus the set hash --
            the value AC1 asserts is stable across recompiles.
    """

    nodes: tuple[TaskNode, ...]
    requirement_set_hash: str
    graph_hash: str

    def node_ids(self) -> tuple[str, ...]:
        """Return the ordered node ids."""
        return tuple(n.node_id for n in self.nodes)

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_set_hash": self.requirement_set_hash,
            "graph_hash": self.graph_hash,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    def to_plan_dict(self, *, name: str, description: str = "") -> dict[str, object]:
        """Project the graph onto a Bernstein plan dict (stages + steps).

        The result validates against
        :func:`bernstein.core.planning.plan_schema.validate_plan`. Requirement
        ids are recorded per step so the plan file keeps the lineage anchor.
        """
        steps: list[dict[str, object]] = []
        for node in self.nodes:
            steps.append(
                {
                    "title": node.title,
                    "role": node.role,
                    "description": "Implements " + ", ".join(node.requirement_ids),
                }
            )
        return {
            "name": name,
            "description": description or f"Compiled from requirement set {self.requirement_set_hash}",
            "stages": [{"name": "implement", "steps": steps}],
        }


def _compile_node(requirement: Requirement) -> TaskNode:
    title = _derive_title(requirement.text)
    role = infer_role(requirement.text)
    requirement_hashes = (requirement.line_hash,)
    return TaskNode(
        node_id=_node_id(title=title, role=role, requirement_hashes=requirement_hashes),
        title=title,
        role=role,
        requirement_ids=(requirement.id,),
        requirement_hashes=requirement_hashes,
    )


def _graph_hash(node_ids: tuple[str, ...], requirement_set_hash: str) -> str:
    preimage = json.dumps(
        {"nodes": list(node_ids), "requirement_set_hash": requirement_set_hash},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def compile_requirements(req_set: RequirementSet) -> TaskGraph:
    """Compile *req_set* into a deterministic :class:`TaskGraph`.

    One node is emitted per requirement, in document order. This is a pure
    transformation: no model call, no clock, no randomness -- identical input
    yields byte-identical output including node ids and the graph hash.
    """
    nodes = tuple(_compile_node(req) for req in req_set.requirements)
    graph_hash = _graph_hash(tuple(n.node_id for n in nodes), req_set.set_hash)
    return TaskGraph(nodes=nodes, requirement_set_hash=req_set.set_hash, graph_hash=graph_hash)


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Outcome of a requirement-hash lineage-coverage check.

    Attributes:
        covered: ``True`` iff every node carries at least one requirement hash.
        uncovered_node_ids: Node ids that carry no requirement hash.
    """

    covered: bool
    uncovered_node_ids: tuple[str, ...]


def lineage_coverage(graph: TaskGraph) -> CoverageResult:
    """Check that every node in *graph* traces to at least one requirement hash.

    This is the AC2 gate: a node with no requirement hash could produce an
    artefact that traces to nothing, so the compiler / verifier must reject it.
    """
    uncovered = tuple(n.node_id for n in graph.nodes if not n.requirement_hashes)
    return CoverageResult(covered=not uncovered, uncovered_node_ids=uncovered)
