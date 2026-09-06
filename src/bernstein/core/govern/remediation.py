"""Collect the declared remedies for a govern plan into one proposal (issue #5079).

A finding that reports a gap and stops there leaves the operator to close it by
hand. A clause may instead declare *how* its gap is closed, as an ordered change
set on the clause itself (``remediation_plan`` in
:mod:`bernstein.core.govern.playbook_models`). :func:`collect_remediation`
projects a :class:`~bernstein.core.govern.plan_models.GovernPlan` and the
playbook that judged it into a single :class:`RemediationProposal`.

Three properties hold, and are pinned by tests:

- The proposal is a **proposal**. It is DRAFT and unsigned, and carries no
  method that executes it; a human signs it before anything applies it, exactly
  as :class:`~bernstein.core.govern.proposal.DraftProposal` requires.
- Every finding lands in exactly one of two places: a step that answers it, or
  the ``unremediated`` list that names it and says why. A finding whose clause
  declared no remedy is never read as "nothing to do".
- The collection is a deterministic projection. Steps are ordered canonically
  and the proposal binds the plan and playbook digests it was collected from,
  so two operators reach a byte-identical artifact and a proposal cannot be
  re-pointed at a different world.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.govern.playbook_models import RemediationAction, parse_remediation_plan
from bernstein.core.govern.proposal import ProposalStatus

if TYPE_CHECKING:
    from bernstein.core.govern.plan_models import GovernPlan

_CLAUSE_KINDS = ("forbidden", "permitted", "required")

_NO_PLAN = "no remediation_plan declared"
_EMPTY_PLAN = "remediation_plan declared no actions"


@dataclass(frozen=True, slots=True)
class RemediationStep:
    """One declared change, bound to the finding it answers.

    A step is a :class:`RemediationAction` plus the finding that called for it,
    so a reviewer reading the proposal alone can say which observation and which
    clause produced each change.

    Attributes:
        surface: The surface the finding was raised against.
        playbook_clause: The clause that judged the surface.
        finding_kind: The plan entry kind, e.g. ``forbidden`` or ``absent``.
        action: The verb to apply.
        target: What the verb applies to.
        value: The value the verb writes, or None for verbs that take none.
    """

    surface: str
    playbook_clause: str
    finding_kind: str
    action: str
    target: str
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "action": self.action,
            "finding_kind": self.finding_kind,
            "playbook_clause": self.playbook_clause,
            "surface": self.surface,
            "target": self.target,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RemediationStep:
        """Rebuild a step from a serialized dict."""
        return cls(
            surface=str(raw["surface"]),
            playbook_clause=str(raw["playbook_clause"]),
            finding_kind=str(raw["finding_kind"]),
            action=str(raw["action"]),
            target=str(raw["target"]),
            value=raw.get("value"),
        )


@dataclass(frozen=True, slots=True)
class UnremediatedFinding:
    """A finding the playbook judged but declared no way to close.

    Its presence in a proposal is the whole point: an operator reading the
    proposal sees what it does *not* cover without opening the playbook.

    Attributes:
        surface: The surface the finding was raised against.
        playbook_clause: The clause that judged the surface.
        finding_kind: The plan entry kind, e.g. ``wider_ceiling``.
        reason: Why no step was collected for this finding.
    """

    surface: str
    playbook_clause: str
    finding_kind: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "finding_kind": self.finding_kind,
            "playbook_clause": self.playbook_clause,
            "reason": self.reason,
            "surface": self.surface,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> UnremediatedFinding:
        """Rebuild an unremediated finding from a serialized dict."""
        return cls(
            surface=str(raw["surface"]),
            playbook_clause=str(raw["playbook_clause"]),
            finding_kind=str(raw["finding_kind"]),
            reason=str(raw["reason"]),
        )


@dataclass(frozen=True, slots=True)
class RemediationProposal:
    """The remedies for one plan, collected into one unsigned draft.

    The proposal is content-addressed and bound to the plan and playbook it was
    collected from. It has no method that executes it: applying it is a separate
    act, on a signed copy, by whoever holds the signature.

    Attributes:
        plan_hash: Content address of the plan whose findings were collected.
        playbook_hash: Content address of the playbook that judged them.
        steps: The collected changes, in canonical order.
        unremediated: The findings for which no change was declared.
        timestamp: Integer timestamp; caller-chosen but stable, so identical
            inputs produce byte-identical artifacts.
        status: DRAFT until a human signs it.
        human_signature: The operator's signature, or None while DRAFT.
    """

    plan_hash: str
    playbook_hash: str
    steps: tuple[RemediationStep, ...]
    unremediated: tuple[UnremediatedFinding, ...]
    timestamp: int
    status: ProposalStatus = ProposalStatus.DRAFT
    human_signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "human_signature": self.human_signature,
            "plan_hash": self.plan_hash,
            "playbook_hash": self.playbook_hash,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "timestamp": self.timestamp,
            "unremediated": [u.to_dict() for u in self.unremediated],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RemediationProposal:
        """Rebuild a proposal from a serialized dict."""
        return cls(
            plan_hash=str(raw["plan_hash"]),
            playbook_hash=str(raw["playbook_hash"]),
            steps=tuple(RemediationStep.from_dict(s) for s in raw.get("steps", [])),
            unremediated=tuple(UnremediatedFinding.from_dict(u) for u in raw.get("unremediated", [])),
            timestamp=int(raw["timestamp"]),
            status=ProposalStatus(str(raw["status"])),
            human_signature=raw.get("human_signature"),
        )

    def to_canonical_bytes(self) -> bytes:
        """Serialize the proposal to canonical JSON bytes.

        Sorted keys, minimal separators, UTF-8 — the form hashed into the
        lineage spine, so two replays over the same inputs anchor the same
        bytes.
        """
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def content_hash(self) -> str:
        """Return the ``sha256:``-prefixed content address of this proposal."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    def is_signed(self) -> bool:
        """Return True once a human has signed this proposal."""
        return self.status == ProposalStatus.SIGNED and self.human_signature is not None

    def sign(self, signature: str) -> RemediationProposal:
        """Return a signed copy of this proposal, leaving this one DRAFT.

        Args:
            signature: The human operator's signature.
        """
        return RemediationProposal(
            plan_hash=self.plan_hash,
            playbook_hash=self.playbook_hash,
            steps=self.steps,
            unremediated=self.unremediated,
            timestamp=self.timestamp,
            status=ProposalStatus.SIGNED,
            human_signature=signature,
        )


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _remedies_by_clause(playbook: dict[str, Any]) -> dict[tuple[str, str], tuple[RemediationAction, ...] | None]:
    """Index each ``(surface, clause)`` to the remedy it declares, if any.

    Raises:
        ValueError: When a declared ``remediation_plan`` is malformed.
    """
    index: dict[tuple[str, str], tuple[RemediationAction, ...] | None] = {}
    for kind in _CLAUSE_KINDS:
        for clause in playbook.get(kind, []):
            key = (str(clause["surface"]), str(clause["clause"]))
            index[key] = parse_remediation_plan(clause.get("remediation_plan"))
    return index


def collect_remediation(
    *,
    plan: GovernPlan,
    playbook: dict[str, Any],
    timestamp: int,
) -> RemediationProposal:
    """Collect the declared remedies for *plan*'s findings into one proposal.

    Every entry in *plan* is looked up against the clause that judged it. A
    clause that declares a remedy contributes its actions as steps, in declared
    order; a clause that declares none puts the finding in ``unremediated`` with
    the reason. Findings are visited in canonical order — ``(surface, kind,
    clause)`` — so the proposal does not depend on how the inputs were
    serialized.

    Args:
        plan: The posture diff whose findings are to be remedied.
        playbook: The declared posture, in the schema
            :func:`~bernstein.core.govern.compute_plan` reads.
        timestamp: Integer timestamp recorded on the proposal.

    Returns:
        An unsigned DRAFT :class:`RemediationProposal`.

    Raises:
        ValueError: When a clause declares a malformed ``remediation_plan``.
    """
    remedies = _remedies_by_clause(playbook)

    steps: list[RemediationStep] = []
    unremediated: list[UnremediatedFinding] = []

    for entry in sorted(plan.entries, key=lambda e: (e.surface, e.kind.value, e.playbook_clause)):
        actions = remedies.get((entry.surface, entry.playbook_clause))
        if not actions:
            unremediated.append(
                UnremediatedFinding(
                    surface=entry.surface,
                    playbook_clause=entry.playbook_clause,
                    finding_kind=entry.kind.value,
                    reason=_EMPTY_PLAN if actions == () else _NO_PLAN,
                )
            )
            continue
        steps.extend(
            RemediationStep(
                surface=entry.surface,
                playbook_clause=entry.playbook_clause,
                finding_kind=entry.kind.value,
                action=action.action,
                target=action.target,
                value=action.value,
            )
            for action in actions
        )

    return RemediationProposal(
        plan_hash=_canonical_hash(plan.to_dict()),
        playbook_hash=_canonical_hash(playbook),
        steps=tuple(steps),
        unremediated=tuple(unremediated),
        timestamp=timestamp,
        status=ProposalStatus.DRAFT,
        human_signature=None,
    )


__all__ = [
    "RemediationProposal",
    "RemediationStep",
    "UnremediatedFinding",
    "collect_remediation",
]
