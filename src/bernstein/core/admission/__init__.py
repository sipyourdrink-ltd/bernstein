"""Named resource pools with lease-backed admission (#2544).

One admission subsystem where *the grant is the artifact*. Pool occupancy,
queue order, effective rate limits, and postures are never a mutable side
table: they are a pure projection of hash-chained ledger rows, mirrored into
the HMAC audit chain. Two operators replaying the same ledger derive the
byte-identical grant order, so slot forensics is a replay, not an
investigation.

Public surface:

* :class:`~bernstein.core.admission.engine.AdmissionEngine` -- the arbiter.
* :class:`~bernstein.core.admission.projection.AdmissionState` -- the pure
  projection (``from_rows``).
* :func:`~bernstein.core.admission.verify.verify_admission_ledger` -- recompute
  from genesis, fail closed.
* Models: :class:`~bernstein.core.admission.models.Posture`, ``PoolSpec``,
  ``TagLimitSpec``, ``RateLimitSpec``, ``QueueSpec``, ``Grant``.
"""

from __future__ import annotations

from bernstein.core.admission.engine import (
    AdmissionDecision,
    AdmissionEngine,
    ExpiryOutcome,
    QuarantineResult,
)
from bernstein.core.admission.models import (
    Grant,
    PoolSpec,
    Posture,
    QueueSpec,
    RateLimitSpec,
    TagLimitSpec,
)
from bernstein.core.admission.projection import AdmissionState, GrantInterval
from bernstein.core.admission.verify import AdmissionVerification, verify_admission_ledger

__all__ = [
    "AdmissionDecision",
    "AdmissionEngine",
    "AdmissionState",
    "AdmissionVerification",
    "ExpiryOutcome",
    "Grant",
    "GrantInterval",
    "PoolSpec",
    "Posture",
    "QuarantineResult",
    "QueueSpec",
    "RateLimitSpec",
    "TagLimitSpec",
    "verify_admission_ledger",
]
