"""SLA trigger source - normalise a violation receipt into a TriggerEvent (#2549).

When a per-goal SLA contract is found breached inside a supervisor tick, the
breach is normalised into a :class:`TriggerEvent` so the existing trigger
pipeline (:mod:`bernstein.core.orchestration.trigger_manager`) can route it,
exactly as ``trigger_sources.schedule`` routes a fire. This is the only handoff
this ticket makes: delivery of the event to external automation platforms rides
the bridge in #2512 and is out of scope here.

The normalised event carries only the receipt's identity (contract hash, breached
axes, remediation decision, receipt digest) - never goal text or artifact
contents - so a downstream automation subscribes to the fact of the breach and
fetches the full signed receipt when it needs the evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.core.tasks.models import TriggerEvent

if TYPE_CHECKING:
    from bernstein.core.orchestration.sla_receipt import SLAViolationReceipt


def normalize_sla_violation(receipt: SLAViolationReceipt) -> TriggerEvent:
    """Normalise an SLA violation receipt into a :class:`TriggerEvent`.

    Args:
        receipt: The signed violation receipt assembled for the breach.

    Returns:
        A TriggerEvent with ``source="sla"`` whose metadata carries the receipt
        identity so the trigger pipeline can route the breach without re-reading
        the receipt.
    """
    breached_axes = [str(v.get("axis", "")) for v in receipt.verdicts if v.get("breached")]
    subject_type = str(receipt.contract_body.get("subject_type", ""))
    subject_id = str(receipt.contract_body.get("subject_id", ""))
    remediation = receipt.remediation
    metadata: dict[str, Any] = {
        "source_type": "sla",
        "contract_id": receipt.contract_id,
        "contract_hash": receipt.contract_hash,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "tick_instant": receipt.tick_instant,
        "breached_axes": breached_axes,
        "receipt_id": receipt.receipt_id,
        "receipt_digest": receipt.payload_digest,
        "requested_action": remediation.get("requested_action", ""),
        "effective_action": remediation.get("effective_action", ""),
        "remediation_blocked": bool(remediation.get("blocked", False)),
    }
    axes = ", ".join(breached_axes) or "unknown"
    message = f"SLA breach on {subject_type}:{subject_id} ({axes})"
    return TriggerEvent(
        source="sla",
        timestamp=float(receipt.tick_instant),
        raw_payload={
            "contract_id": receipt.contract_id,
            "contract_hash": receipt.contract_hash,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "tick_instant": receipt.tick_instant,
            "breached_axes": breached_axes,
            "receipt_digest": receipt.payload_digest,
        },
        message=message[:500],
        metadata=metadata,
    )


__all__ = ["normalize_sla_violation"]
