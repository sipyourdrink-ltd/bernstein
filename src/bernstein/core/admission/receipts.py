"""Signed admission receipts: waiver + tag-conformance (#2544).

Two receipt shapes live here, both signed with the install's Ed25519 identity
via :func:`bernstein.core.skills.catalog.signature.sign_payload` (the same
primitive the escalation receipt and catalog signing paths use):

* :class:`WaiverReceipt` -- emitted whenever an ADVISE-posture gate lets an
  over-limit request through. Soft enforcement degrades *observably*: every
  over-limit pass leaves a signed artifact, so the count of waivers equals the
  count of over-limit passes exactly.
* :class:`TagConformanceReceipt` -- the post-hoc seal over a completed task's
  declared-tag contract. A task declaring ``docs-only`` whose diff touched
  ``src/`` carries a signed *violation* receipt that merge gates can read.

Both receipts bind ``prev_chain_digest`` so they link into the admission
ledger, and both are canonical-JSON signed so two operators serialising the
same envelope produce byte-identical signing bytes. The lease-expiry escalation
receipt is intentionally *not* redefined here: it is assembled in the
:mod:`bernstein.core.orchestration.supervisor_receipt` shape by
:mod:`bernstein.core.admission.engine`, per the issue.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.skills.catalog.signature import (
    ManifestSignatureError,
    sign_payload,
    verify_payload,
)

if TYPE_CHECKING:
    from bernstein.core.admission.tags import TagViolation

__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "TagConformanceReceipt",
    "WaiverReceipt",
    "assemble_tag_conformance_receipt",
    "assemble_waiver_receipt",
    "canonical_receipt_bytes",
    "sign_receipt",
    "verify_receipt",
]

#: Schema version embedded in every admission receipt.
RECEIPT_SCHEMA_VERSION: str = "1.0.0"


def canonical_receipt_bytes(payload: dict[str, Any]) -> bytes:
    """Return deterministic signing bytes: sorted keys, compact, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_receipt_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class WaiverReceipt:
    """A signed record of one ADVISE-posture over-limit pass."""

    schema_version: str
    gate: str
    resource: str
    task_id: str
    worker_id: str
    granted_ts: int
    prev_chain_digest: str
    payload_digest: str
    signature: str = ""

    def signing_payload(self) -> dict[str, Any]:
        """Return the dict that gets signed (excludes signature + digest)."""
        return {
            "gate": self.gate,
            "granted_ts": self.granted_ts,
            "kind": "admission.waiver",
            "prev_chain_digest": self.prev_chain_digest,
            "resource": self.resource,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.signing_payload() | {"payload_digest": self.payload_digest, "signature": self.signature}


@dataclass(frozen=True, slots=True)
class TagConformanceReceipt:
    """A signed post-hoc seal over a task's declared-tag contract."""

    schema_version: str
    task_id: str
    worker_id: str
    declared_tags: tuple[str, ...]
    conformant: bool
    violations: tuple[TagViolation, ...]
    diff_digest: str
    prev_chain_digest: str
    payload_digest: str
    signature: str = ""
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    def signing_payload(self) -> dict[str, Any]:
        return {
            "conformant": self.conformant,
            "declared_tags": list(self.declared_tags),
            "details": self.details,
            "diff_digest": self.diff_digest,
            "kind": "admission.tag_conformance",
            "prev_chain_digest": self.prev_chain_digest,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "violations": [v.to_dict() for v in self.violations],
            "worker_id": self.worker_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.signing_payload() | {"payload_digest": self.payload_digest, "signature": self.signature}


# ---------------------------------------------------------------------------
# Assembly + signing
# ---------------------------------------------------------------------------


def assemble_waiver_receipt(
    *,
    gate: str,
    resource: str,
    task_id: str,
    worker_id: str,
    granted_ts: int,
    prev_chain_digest: str,
) -> WaiverReceipt:
    """Build an unsigned waiver receipt with its payload digest filled in."""
    base = WaiverReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        gate=gate,
        resource=resource,
        task_id=task_id,
        worker_id=worker_id,
        granted_ts=granted_ts,
        prev_chain_digest=prev_chain_digest,
        payload_digest="",
    )
    return _with_digest(base)


def assemble_tag_conformance_receipt(
    *,
    task_id: str,
    worker_id: str,
    declared_tags: tuple[str, ...],
    violations: tuple[TagViolation, ...],
    diff_digest: str,
    prev_chain_digest: str,
    details: dict[str, Any] | None = None,
) -> TagConformanceReceipt:
    """Build an unsigned tag-conformance receipt with its digest filled in."""
    base = TagConformanceReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        task_id=task_id,
        worker_id=worker_id,
        declared_tags=declared_tags,
        conformant=not violations,
        violations=violations,
        diff_digest=diff_digest,
        prev_chain_digest=prev_chain_digest,
        payload_digest="",
        details=details or {},
    )
    return _with_digest(base)


def _with_digest(receipt: WaiverReceipt | TagConformanceReceipt) -> Any:
    import dataclasses

    digest = _payload_digest(receipt.signing_payload())
    return dataclasses.replace(receipt, payload_digest=digest)


def sign_receipt(receipt: WaiverReceipt | TagConformanceReceipt, *, private_key_pem: str) -> Any:
    """Attach a detached Ed25519 signature over the canonical signing bytes."""
    import dataclasses

    signature = sign_payload(canonical_receipt_bytes(receipt.signing_payload()), private_key_pem)
    return dataclasses.replace(receipt, signature=signature)


def verify_receipt(
    receipt: WaiverReceipt | TagConformanceReceipt,
    *,
    public_key_pem: str,
) -> tuple[bool, str]:
    """Verify a receipt's digest and signature.

    Returns ``(ok, reason)``. ``reason`` is empty on success. The digest is
    checked first so a tampered payload is caught even before the signature.
    """
    expected_digest = _payload_digest(receipt.signing_payload())
    if expected_digest != receipt.payload_digest:
        return (
            False,
            f"payload_digest mismatch (expected {expected_digest[:16]}..., got {receipt.payload_digest[:16]}...)",
        )
    if not receipt.signature:
        return False, "receipt is unsigned"
    try:
        outcome = verify_payload(
            canonical_receipt_bytes(receipt.signing_payload()),
            receipt.signature,
            public_key_pem,
            allow_unverified=True,
        )
    except ManifestSignatureError as exc:  # pragma: no cover - defensive
        return False, str(exc)
    if not outcome.verified:
        return False, outcome.reason
    return True, ""
