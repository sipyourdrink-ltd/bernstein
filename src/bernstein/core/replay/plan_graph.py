"""Pure structural graph fold over an append-ordered journal prefix.

This checks graph payloads against the producer's canonical digest. It does
not authenticate the journal chain, its completeness, or an external head;
callers must establish those separately before relying on the input record.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from bernstein.core.orchestration.schedule_projection import (
    SCHEDULE_PROJECTION_REV,
    TaskNode,
    canonical_graph_bytes,
    canonical_graph_digest,
)


@dataclass(frozen=True)
class PlanGraphResult:
    """Immutable structural view; only ``available`` exposes graph data.

    ``empty`` means no graph event occurred (distinct from an available
    zero-node graph). ``unavailable`` means the latest digest has no matching
    full snapshot. ``invalid`` means a relevant row failed validation anywhere
    in the prefix. Digests describe the latest examined graph row; a digest
    mismatch also exposes the recomputed value for diagnosis.
    """

    status: Literal["available", "unavailable", "invalid", "empty"]
    nodes: tuple[TaskNode, ...] = ()
    recorded_digest: str | None = None
    recomputed_digest: str | None = None
    canonical_bytes: bytes | None = None
    reason: str = ""


def _header_error(row: Mapping[str, Any]) -> str:
    if row.get("rev") != SCHEDULE_PROJECTION_REV:
        return "unsupported graph revision"
    digest = row.get("digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return "malformed graph digest"
    count = row.get("task_count")
    if type(count) is not int or count < 0:
        return "malformed graph task count"
    return ""


def _structural_nodes(raw: Any) -> tuple[TaskNode, ...] | None:
    if not isinstance(raw, list):
        return None
    nodes: list[TaskNode] = []
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        task_id, role, dependencies = item.get("id"), item.get("role"), item.get("depends_on")
        if not isinstance(task_id, str) or not isinstance(role, str) or not isinstance(dependencies, list):
            return None
        if task_id in ids or any(not isinstance(dep, str) for dep in dependencies):
            return None
        # TaskStore keys tasks by id. Preserve repeated/dangling edges: the
        # producer encoder accepts them and imposes no DAG validation here.
        ids.add(task_id)
        nodes.append(TaskNode(task_id, role, "", "", tuple(sorted(dependencies))))
    return tuple(sorted(nodes, key=lambda node: node.task_id))


def fold_plan_graph(events: Iterable[Mapping[str, Any]]) -> PlanGraphResult:
    """Derive the latest recorded structural graph without mutating *events*.

    Rows are consumed in append order, ignoring timestamps and unrelated
    events. A newer marker hides an unmatched snapshot, while a valid full
    row supersedes any older marker: producer appends fail independently.
    A delayed matching marker can reuse the latest full snapshot. Malformed,
    unsupported or inconsistent graph rows invalidate the entire supplied
    prefix, even if a later row would otherwise supply a usable snapshot.
    Titles, descriptions, goals and metadata do not participate in identity.
    """
    result = PlanGraphResult("empty", reason="no graph events")
    snapshot: PlanGraphResult | None = None
    counts: dict[str, int] = {}
    for row in events:
        event = row.get("event")
        if event not in ("plan.graph", "plan.graph.full"):
            continue
        raw_digest = row.get("digest")
        digest = raw_digest if isinstance(raw_digest, str) else None
        error = _header_error(row)
        if error:
            return PlanGraphResult("invalid", recorded_digest=digest, reason=error)
        count = row["task_count"]
        if row["digest"] in counts and counts[row["digest"]] != count:
            return PlanGraphResult("invalid", recorded_digest=digest, reason="inconsistent graph task count")
        counts[row["digest"]] = count
        if event == "plan.graph":
            if snapshot is not None and snapshot.recorded_digest == digest:
                result = snapshot
            else:
                result = PlanGraphResult("unavailable", recorded_digest=digest, reason="matching full snapshot missing")
            continue
        nodes = _structural_nodes(row.get("nodes"))
        if nodes is None:
            return PlanGraphResult("invalid", recorded_digest=digest, reason="malformed graph nodes")
        if len(nodes) != count:
            return PlanGraphResult("invalid", recorded_digest=digest, reason="snapshot task count mismatch")
        recomputed = canonical_graph_digest(nodes)
        if recomputed != digest:
            return PlanGraphResult(
                "invalid", recorded_digest=digest, recomputed_digest=recomputed, reason="graph digest mismatch"
            )
        snapshot = result = PlanGraphResult("available", nodes, digest, recomputed, canonical_graph_bytes(nodes))
    return result
