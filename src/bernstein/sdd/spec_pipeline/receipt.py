"""Approval stage: bind the requirement-set hash into the audit chain.

The approval receipt is the plan-approval gate for the spec pipeline (issue
#2361, AC4). :func:`approve_requirement_set` records a ``spec.requirement_set``
event into the HMAC-chained audit log, binding the content-addressed
requirement-set hash, the source-spec hash, the compiled graph hash, and the
decision. The receipt is the load-bearing artefact: a verifier can prove, from
the chain alone, that a task graph was compiled from the exact requirement set
the operator approved, and any post-approval edit to a requirement line breaks
the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.security.audit_chain import record_spec_requirement_set

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.sdd.spec_pipeline.compiler import TaskGraph
    from bernstein.sdd.spec_pipeline.requirements import RequirementSet

__all__ = ["RequirementSetReceipt", "approve_requirement_set"]

_APPROVED = "approved"
_REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RequirementSetReceipt:
    """The content of a spec-pipeline approval receipt.

    Attributes:
        requirement_set_hash: Set hash bound into the chain.
        source_hash: Hash of the source spec document.
        requirement_count: Number of requirements approved.
        graph_hash: Hash of the compiled task graph.
        decision: ``approved`` or ``rejected``.
    """

    requirement_set_hash: str
    source_hash: str
    requirement_count: int
    graph_hash: str
    decision: str

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_set_hash": self.requirement_set_hash,
            "source_hash": self.source_hash,
            "requirement_count": self.requirement_count,
            "graph_hash": self.graph_hash,
            "decision": self.decision,
        }


def approve_requirement_set(
    *,
    chain: AuditChainStore,
    req_set: RequirementSet,
    graph: TaskGraph,
    approve: bool = True,
    actor: str = "spec-pipeline",
) -> tuple[RequirementSetReceipt, AuditEvent]:
    """Record an approval (or rejection) receipt for *req_set* into *chain*.

    Args:
        chain: The audit chain store accepting the receipt.
        req_set: The drafted requirement set under review.
        graph: The task graph compiled from ``req_set``. Its
            ``requirement_set_hash`` must match ``req_set.set_hash``.
        approve: ``True`` to approve, ``False`` to reject.
        actor: Recorded actor; defaults to ``"spec-pipeline"``.

    Returns:
        A ``(receipt, event)`` pair. The event is the chained audit record.

    Raises:
        ValueError: When *graph* was compiled from a different requirement set.
    """
    if graph.requirement_set_hash != req_set.set_hash:
        raise ValueError(
            f"graph was not compiled from this requirement set: {graph.requirement_set_hash} != {req_set.set_hash}"
        )
    decision = _APPROVED if approve else _REJECTED
    receipt = RequirementSetReceipt(
        requirement_set_hash=req_set.set_hash,
        source_hash=req_set.source_hash,
        requirement_count=len(req_set.requirements),
        graph_hash=graph.graph_hash,
        decision=decision,
    )
    event = record_spec_requirement_set(
        chain=chain,
        requirement_set_hash=receipt.requirement_set_hash,
        source_hash=receipt.source_hash,
        requirement_count=receipt.requirement_count,
        graph_hash=receipt.graph_hash,
        decision=receipt.decision,
        actor=actor,
    )
    return receipt, event
