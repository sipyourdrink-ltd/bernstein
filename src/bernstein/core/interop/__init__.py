"""Cross-organisation interoperability surfaces.

This package holds the A2A (agent-to-agent) capability-card primitives and
the lineage-chain wrapper that lets a Bernstein run delegated to a peer
agent stay auditable across an organisational boundary.

Submodules:

* :mod:`bernstein.core.interop.a2a_card` -- issue, sign, and verify a
  capability card describing the local orchestrator (identity, advertised
  tools, supported policies, public key, expiry).
* :mod:`bernstein.core.interop.a2a_consume` -- fetch a peer card, verify it
  against a trusted-issuer set, and gate delegation on whether the peer's
  advertised policies meet the operator's required policies.
* :mod:`bernstein.core.interop.a2a_lineage` -- wrap a signed Bernstein
  lineage chain into an A2A envelope payload and append a cross-org
  boundary marker on the receiving side, reusing the existing
  :mod:`bernstein.core.lineage.tracker_audit` HMAC chain; and turn every
  inbound/outbound A2A message into a signed lineage receipt anchored to the
  message-receipt spine, with ``verify_thread`` proving the cross-agent thread
  equals the executed actions offline.
"""

from __future__ import annotations

from bernstein.core.interop.a2a_card import (
    CapabilityCard,
    CardPolicies,
    SignedCapabilityCard,
    issue_capability_card,
    verify_capability_card,
)
from bernstein.core.interop.a2a_consume import (
    PolicyRequirements,
    PolicyVerdict,
    consume_peer_card,
    policies_meet_requirements,
)
from bernstein.core.interop.a2a_lineage import (
    CROSS_ORG_BOUNDARY_MARKER,
    LINEAGE_ENVELOPE_FIELD,
    A2AMessageReceipt,
    A2AThreadVerifyResult,
    InboundTaskIsolation,
    InboundTaskRejected,
    LineageEnvelope,
    accept_inbound_task,
    append_cross_org_segment,
    isolate_inbound_task,
    map_task_state,
    record_a2a_message,
    verify_thread,
    wrap_lineage_chain,
)

__all__ = [
    "CROSS_ORG_BOUNDARY_MARKER",
    "LINEAGE_ENVELOPE_FIELD",
    "A2AMessageReceipt",
    "A2AThreadVerifyResult",
    "CapabilityCard",
    "CardPolicies",
    "InboundTaskIsolation",
    "InboundTaskRejected",
    "LineageEnvelope",
    "PolicyRequirements",
    "PolicyVerdict",
    "SignedCapabilityCard",
    "accept_inbound_task",
    "append_cross_org_segment",
    "consume_peer_card",
    "isolate_inbound_task",
    "issue_capability_card",
    "map_task_state",
    "policies_meet_requirements",
    "record_a2a_message",
    "verify_capability_card",
    "verify_thread",
    "wrap_lineage_chain",
]
