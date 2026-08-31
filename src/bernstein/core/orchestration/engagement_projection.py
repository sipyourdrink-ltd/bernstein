"""Deterministic projection from an engagement playbook to a canonical task graph.

The projection is the load-bearing property of the engagement runner:
two operators with identical ``(playbook_id, scope, target_state_snapshot_hash)``
MUST land on the byte-identical task graph. Any drift breaks the reproducible
engagement contract that downstream audit walks depend on.

Discipline (do not relax without parent approval):

- The projection function is pure. No ``time.time()``, no wall-clock
  comparisons, no random shuffling, no host-dependent ordering, no
  network reads, no environment lookups.
- All inputs flow in via the function signature; all outputs flow out via
  the returned task-graph mapping.
- The canonical encoding sorts keys and freezes container order so two
  dicts that compare equal serialise to byte-identical JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

#: Schema-rev marker baked into the engagement projection.
ENGAGEMENT_PROJECTION_REV = "1"


@dataclass(frozen=True)
class EngagementMandate:
    """Scope-grant check for an engagement phase action.

    Represents a content-addressed admission receipt issued by the
    orchestrator's mandate resolver. If the scope check fails the
    phase is refused without aborting sibling phases.

    Attributes:
        playbook_id: Parent playbook identifier.
        scope: The scope granted by the mandate.
        scope_ref: Content-addressed reference to the mandate grant.
        admitted: Whether the mandate admits the requested scope.
        refusal_reason: Set when admitted is False.
    """

    playbook_id: str
    scope: str
    scope_ref: str
    admitted: bool
    refusal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "admitted": self.admitted,
            "refusal_reason": self.refusal_reason,
        }


@dataclass(frozen=True)
class TaskNode:
    """One node of the projected engagement task graph.

    Frozen + sortable so the canonical encoder can lay nodes out by a
    stable key regardless of how the projection iterated through state.
    """

    task_id: str
    phase_name: str
    action: str
    scanner_names: tuple[str, ...]
    scope_ref: str
    mandate_status: str
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProjectionResult:
    """The byte-stable output of the engagement projection function.

    Attributes:
        nodes: Tuple of task nodes ordered by task_id.
        graph_hash: SHA-256 over the canonical bytes.
        canonical_bytes: The exact bytes hashed.
    """

    nodes: tuple[TaskNode, ...]
    graph_hash: str
    canonical_bytes: bytes
    rev: str = ENGAGEMENT_PROJECTION_REV
    playbook_id: str = ""
    target_state_snapshot_hash: str = ""
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_bytes.decode())


def _canonical_nodes(nodes: list[TaskNode]) -> tuple[TaskNode, ...]:
    """Sort task nodes by task_id so projection output is order-independent."""
    return tuple(sorted(nodes, key=lambda n: n.task_id))


def _node_to_dict(node: TaskNode) -> dict[str, Any]:
    """Serialise a TaskNode to a sort-friendly dict."""
    return {
        "task_id": node.task_id,
        "phase_name": node.phase_name,
        "action": node.action,
        "scanner_names": sorted(node.scanner_names),
        "scope_ref": node.scope_ref,
        "mandate_status": node.mandate_status,
        "metadata": sorted([list(item) for item in node.metadata]),
    }


def canonical_graph_bytes(nodes: list[TaskNode]) -> bytes:
    """Encode a task-graph node set into the projection's canonical bytes."""
    canonical_obj: dict[str, Any] = {
        "rev": ENGAGEMENT_PROJECTION_REV,
        "nodes": [_node_to_dict(n) for n in _canonical_nodes(nodes)],
    }
    return json.dumps(canonical_obj, sort_keys=True, separators=(",", ":")).encode()


def canonical_graph_digest(nodes: list[TaskNode]) -> str:
    """Return the SHA-256 over :func:`canonical_graph_bytes` for *nodes*."""
    return hashlib.sha256(canonical_graph_bytes(nodes)).hexdigest()


def _build_mandate(
    playbook_id: str,
    scope: str,
    scope_ref: str,
    phase_scope_ref: str,
) -> EngagementMandate:
    """Evaluate whether a phase may run against the given scope.

    A phase is admitted when its ``phase_scope_ref`` is contained within the
    mandate's ``scope`` (or when the mandate is a wildcard). The check is
    content-addressed and purely deterministic — no I/O.
    """
    if scope == "scope:*" or scope == phase_scope_ref or phase_scope_ref.startswith(scope + ":"):
        return EngagementMandate(
            playbook_id=playbook_id,
            scope=scope,
            scope_ref=scope_ref,
            admitted=True,
        )
    # Phase scope not covered by mandate scope — refusal receipt
    return EngagementMandate(
        playbook_id=playbook_id,
        scope=scope,
        scope_ref=scope_ref,
        admitted=False,
        refusal_reason=f"Phase scope {phase_scope_ref!r} not admitted by mandate scope {scope!r}",
    )


def project(
    playbook_id: str,
    scope: str,
    target_state_snapshot_hash: str,
    *,
    phases: tuple[dict[str, Any], ...] = (),
    scope_ref: str = "",
) -> ProjectionResult:
    """Project an engagement playbook onto a deterministic task graph.

    PURE function. No wall-clock, no randomness, no host-dependent state.

    Each phase of the playbook is materialised as a ``TaskNode`` gated on
    the ``EngagementMandate`` scope check. Phases that fail the scope check
    receive a ``mandate_status`` of ``REFUSED`` without aborting sibling
    phases.

    Args:
        playbook_id: Stable playbook identifier.
        scope: The engagement scope granted by the mandate.
        target_state_snapshot_hash: Content-addressed hash of the
            recorded target state (mirror of
            :func:`schedule_projection._digest_last_state`).
        phases: Ordered phase definitions from the playbook YAML.
        scope_ref: Mandate scope grant reference.

    Returns:
        A :class:`ProjectionResult` with the canonical task graph and
        its ``graph_hash``.
    """
    nodes: list[TaskNode] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("name", "")).strip()
        action = str(phase.get("action", "")).strip()
        if not phase_name or not action:
            continue

        phase_scope_ref = str(phase.get("scope_ref", "")).strip() or scope_ref
        mandate = _build_mandate(playbook_id, scope, scope_ref, phase_scope_ref)

        scanner_names: tuple[str, ...] = ()
        scanners_raw = phase.get("scanners")
        if action == "scanner" and isinstance(scanners_raw, list):
            names: list[str] = []
            for sc in cast("list[object]", scanners_raw):
                if isinstance(sc, dict):
                    adapter = str(sc.get("adapter", "")).strip()
                    if adapter:
                        names.append(adapter)
            scanner_names = tuple(names)

        mandate_status = "ADMITTED" if mandate.admitted else "REFUSED"

        task_id_seed = json.dumps(
            {
                "playbook_id": playbook_id,
                "phase_name": phase_name,
                "action": action,
                "scope": scope,
                "target_state_snapshot_hash": target_state_snapshot_hash,
                "rev": ENGAGEMENT_PROJECTION_REV,
            },
            sort_keys=True,
        ).encode()
        task_id = "eng-task-" + hashlib.sha256(task_id_seed).hexdigest()[:16]

        metadata: tuple[tuple[str, str], ...] = (
            ("playbook_id", playbook_id),
            ("phase_name", phase_name),
            ("action", action),
            ("mandate_status", mandate_status),
            ("target_state_snapshot_hash", target_state_snapshot_hash),
            ("rev", ENGAGEMENT_PROJECTION_REV),
        )

        nodes.append(
            TaskNode(
                task_id=task_id,
                phase_name=phase_name,
                action=action,
                scanner_names=scanner_names,
                scope_ref=phase_scope_ref,
                mandate_status=mandate_status,
                metadata=metadata,
            )
        )

    sorted_nodes = _canonical_nodes(nodes)

    canonical_obj: dict[str, Any] = {
        "rev": ENGAGEMENT_PROJECTION_REV,
        "playbook_id": playbook_id,
        "target_state_snapshot_hash": target_state_snapshot_hash,
        "scope": scope,
        "nodes": [_node_to_dict(n) for n in sorted_nodes],
    }
    canonical_bytes = json.dumps(
        canonical_obj,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    graph_hash = hashlib.sha256(canonical_bytes).hexdigest()

    return ProjectionResult(
        nodes=tuple(sorted_nodes),
        graph_hash=graph_hash,
        canonical_bytes=canonical_bytes,
        rev=ENGAGEMENT_PROJECTION_REV,
        playbook_id=playbook_id,
        target_state_snapshot_hash=target_state_snapshot_hash,
        scope=scope,
    )
