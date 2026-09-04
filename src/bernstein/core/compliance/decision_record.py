"""Decision-provenance records projected out of verified approval cards.

Issue #2917. The audit chain already records who authorised a consequential
tool call, against what displayed context, and when -- but it records it as
HMAC-chained JSONL. A reviewer who is not an engineer cannot read that, so
today they read an engineer's summary of it instead, which is precisely the
trust the chain exists to remove.

This module folds a verified issue/resolve pair into a closed-schema
:class:`DecisionRecord`: one row per settled approval, every field taken from
a source event rather than typed by hand, and each record naming the chain
anchors of the two events it was derived from so a reader can re-verify it.

The projection is deliberately total and dumb: it maps
:class:`~bernstein.core.approval.card_verify.VerifiedApprovalCard` values into
record fields and does no checking of its own. All the checking happens in
:func:`~bernstein.core.approval.card_verify.verify_approval_card_events`,
which only reconstructs a pair when the stored envelope still hashes to its
committed ``card_hash``, the decision echoed that hash, the issue preceded the
settlement, the card settled exactly once, and the settlement landed inside
the envelope's window. A settlement that fails any of those contributes no
reconstructed pair, so it can produce no record here -- there is no path that
emits a record with a caveat attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.approval.card_verify import (
        ApprovalCardVerifyResult,
        VerifiedApprovalCard,
    )

__all__ = [
    "DECISION_RECORD_SCHEMA_VERSION",
    "DecisionRecord",
    "build_decision_records",
    "render_decision_records",
]

#: Schema marker carried by every emitted record. The record is a closed
#: schema on purpose: a reviewer's checklist maps onto fixed fields, and a
#: projection that could grow free-form keys would let an engineer smuggle
#: unattested prose into an attested document.
DECISION_RECORD_SCHEMA_VERSION = "decision-record/v1"

#: How much of a hash to show inline in the human-readable rendering. The full
#: value is always in :meth:`DecisionRecord.to_dict`, so the short form is a
#: reading aid and never the only place a reviewer can find the anchor.
_SHORT_HASH_CHARS = 16


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One settled approval, projected into reviewer-facing fields.

    Attributes:
        decision_id: The envelope's ``card_hash``. It is the decision's
            identity because it commits to the whole context the approver was
            shown, so two decisions are the same decision exactly when they
            were made against the same displayed facts.
        approver: The identity recorded on the settlement event.
        decision: ``approve`` or ``reject``.
        tool_name: The tool the approval gated.
        args_digest: Canonical digest of the arguments the tool would run
            with, so the approval binds the concrete invocation.
        reasoning: The agent's stated intent, as it was displayed and hashed.
        impact_score: Blast-radius score in ``[0, 1]``.
        impact_hard_one_way: ``True`` when the change contains a one-way door.
        impact_rationale: The scorer's structured rationale.
        fired_detectors: Ordered ids of every blast-radius detector that fired.
        rollback_procedure: The undo path shown alongside the request.
        rollback_irreversible: ``True`` when no clean automatic undo exists.
        issued_at: Envelope creation time (unix epoch seconds).
        not_after: Envelope expiry (unix epoch seconds).
        resolved_at: Settlement time (unix epoch seconds).
        source_events: Chain anchors keyed by role -- ``issued`` and
            ``resolved`` map to the HMAC of the event each field came from.
    """

    decision_id: str
    approver: str
    decision: str
    tool_name: str
    args_digest: str
    reasoning: str
    impact_score: float
    impact_hard_one_way: bool
    impact_rationale: str
    fired_detectors: tuple[str, ...]
    rollback_procedure: str
    rollback_irreversible: bool
    issued_at: float
    not_after: float
    resolved_at: float
    source_events: dict[str, str]

    def statement(self) -> str:
        """Return the plain-language sentence a non-engineer reads first.

        The sentence names the approving identity, the action it authorised,
        and the ``card_hash`` that locates the approval receipt in the chain,
        so the reviewer can follow the claim back to the event that backs it
        without being handed a JSONL file.
        """
        verb = "approved" if self.decision == "approve" else f"recorded a {self.decision!r} decision on"
        irreversible = (
            " The change was flagged as a one-way door with no clean automatic undo."
            if self.rollback_irreversible
            else ""
        )
        return (
            f"{self.approver} {verb} the tool call {self.tool_name} "
            f"(arguments {self.args_digest[:_SHORT_HASH_CHARS]}), "
            f"with a blast-radius score of {self.impact_score:.2f}."
            f"{irreversible} "
            f"The approval receipt is approval card {self.decision_id}, "
            f"anchored in the audit chain at {self.source_events['resolved'][:_SHORT_HASH_CHARS]}."
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a JSON-ready dict, in schema field order."""
        return {
            "schema": DECISION_RECORD_SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "approver": self.approver,
            "decision": self.decision,
            "tool_name": self.tool_name,
            "args_digest": self.args_digest,
            "reasoning": self.reasoning,
            "impact_score": self.impact_score,
            "impact_hard_one_way": self.impact_hard_one_way,
            "impact_rationale": self.impact_rationale,
            "fired_detectors": list(self.fired_detectors),
            "rollback_procedure": self.rollback_procedure,
            "rollback_irreversible": self.rollback_irreversible,
            "issued_at": self.issued_at,
            "not_after": self.not_after,
            "resolved_at": self.resolved_at,
            "source_events": dict(self.source_events),
        }


def _record_from(verified: VerifiedApprovalCard) -> DecisionRecord:
    """Fold one verified pair into its decision record."""
    card = verified.card
    return DecisionRecord(
        decision_id=verified.card_hash,
        approver=verified.approver,
        decision=verified.decision,
        tool_name=card.action.tool_name,
        args_digest=card.action.args_digest,
        reasoning=card.reasoning,
        impact_score=card.impact.score,
        impact_hard_one_way=card.impact.hard_one_way,
        impact_rationale=card.impact.rationale,
        fired_detectors=card.impact.fired_detectors,
        rollback_procedure=card.rollback.procedure,
        rollback_irreversible=card.rollback.irreversible,
        issued_at=card.created_at,
        not_after=card.not_after,
        resolved_at=verified.resolved_at,
        source_events={"issued": verified.issued_hmac, "resolved": verified.resolved_hmac},
    )


def build_decision_records(result: ApprovalCardVerifyResult) -> list[DecisionRecord]:
    """Project the settlements *result* reconstructed into decision records.

    Only ``result.records`` is read, so a settlement the verifier could not
    reconstruct -- a mutated envelope, an unmatched hash, a double settlement,
    a decision outside the envelope's window -- yields no record at all rather
    than a record carrying a warning. A reviewer holding this list is holding
    only decisions whose whole context re-derives from the chain.
    """
    return [_record_from(verified) for verified in result.records]


def render_decision_records(records: Sequence[DecisionRecord] | Iterable[DecisionRecord]) -> str:
    """Render *records* as the human-readable decision-provenance report."""
    materialised = list(records)
    if not materialised:
        return "No verified approval decisions were reconstructed from the audit chain."

    lines: list[str] = [
        f"Decision-provenance report ({DECISION_RECORD_SCHEMA_VERSION})",
        f"{len(materialised)} verified decision(s); every field below is taken from a referenced audit event.",
        "",
    ]
    for index, record in enumerate(materialised, start=1):
        lines.extend(
            [
                f"[{index}] decision {record.decision_id[:_SHORT_HASH_CHARS]}",
                f"    {record.statement()}",
                f"    Stated intent      : {record.reasoning}",
                f"    Impact rationale   : {record.impact_rationale}",
                f"    Detectors fired    : {', '.join(record.fired_detectors) or 'none'}",
                f"    Rollback           : {record.rollback_procedure}",
                f"    Decided at         : {record.resolved_at:.0f} "
                f"(valid {record.issued_at:.0f} to {record.not_after:.0f})",
                f"    Source events      : issued {record.source_events['issued']}, "
                f"resolved {record.source_events['resolved']}",
                "",
            ],
        )
    return "\n".join(lines).rstrip() + "\n"
