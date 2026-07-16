"""Verifiable claim receipts for the MCP pull-worker loop (issue #2555).

The task server exposes claim over HTTP and the CLI, but a granted claim
returns a *mutable* task projection: the worker cannot carry the claim it
acted on and re-verify it offline when ownership is later disputed. The
dependency snapshot the claim was granted under already lands on the audit
chain as a ``task.claim_receipt`` event (#2357), yet the client never
receives it as an object it holds.

This module supplies the returned, client-verifiable object, modelled on
the ``RunHandle`` receipt (:mod:`bernstein.core.protocols.mcp.tasks_extension`):

* :class:`ClaimReceipt` - the receipt a worker receives from a claim. Its
  :attr:`~ClaimReceipt.receipt_hash` is a content-addressed digest over a
  canonical tuple ``(task_id, claimer_card_fingerprint, backlog_head,
  filter_digest, chain_head, spec_revision)``. Wall-clock is deliberately
  excluded, so two replays of the same backlog snapshot, claimer, and filter
  hash identically: the receipt is a deterministic projection, not a
  timestamped record. The receipt embeds the audit-chain head the granting
  ``task.claim_receipt`` event recorded, so a client can later prove the
  claim it made against the audited run.

* :func:`backlog_head` / :func:`filter_digest` - the content-addressed
  projections that anchor a receipt to a backlog snapshot and a claim
  filter. Both are wall-clock free so the receipt reprojects byte-identically
  from the on-disk backlog after the claim has landed.

* :func:`sign_claim_receipt` - Ed25519-signs a receipt with the install's
  signing identity, binding every field via the receipt hash. Tampering any
  field invalidates the signature.

* :func:`verify_claim_receipt` - the offline verifier (no network, no running
  server): reprojects :func:`backlog_head` from the on-disk backlog, checks
  the Ed25519 signature, verifies the audit chain, and confirms a matching
  ``task.claim_receipt`` event sits at the receipt's embedded chain head.
  ``bernstein audit verify`` walks the same chain.

A refusal is also a receipt: when a filter matches no eligible row (for
example a dependency-gated task whose ``depends_on`` are not all complete),
:meth:`ClaimReceipt.refusal` returns a signed receipt with an empty
``task_id`` - a claim attempt is never a silent skip.

Determinism
-----------
Every hash here is canonical (sorted-key, compact-separator JSON) and free
of clocks and sockets, so any server instance reprojects byte-identical wire
values from the same on-disk state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.mcp.stateless_core import encode_request_state
from bernstein.core.protocols.mcp.tasks_extension import SPEC_REVISION
from bernstein.core.security.audit_chain import EVENT_TASK_CLAIM_RECEIPT

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tasks.claim import ClaimFilter

__all__ = [
    "REFUSED_TASK_ID",
    "ClaimReceipt",
    "backlog_head",
    "filter_digest",
    "sign_claim_receipt",
    "verify_claim_receipt",
]

#: The ``task_id`` a refusal receipt carries. An empty id means the filter
#: matched no eligible row against the backlog snapshot; the receipt still
#: proves that outcome (no silent skip).
REFUSED_TASK_ID = ""

#: Backlog row fields excluded from :func:`backlog_head`. ``claimed_at`` is a
#: wall-clock stamp: including it would make the digest non-deterministic and
#: break the byte-identical-replay contract, and would prevent the receipt
#: from reprojecting from the post-claim on-disk backlog.
_WALLCLOCK_FIELDS: frozenset[str] = frozenset({"claimed_at"})


def _canonical(payload: Any) -> bytes:
    """Return canonical (sorted-key, compact) JSON bytes for hashing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def backlog_head(rows: Iterable[Mapping[str, Any]]) -> str:
    """Return the content-addressed digest of an ordered backlog snapshot.

    The digest covers every row in order, with wall-clock fields
    (:data:`_WALLCLOCK_FIELDS`) stripped, so the head is a deterministic
    projection: the same logical backlog hashes identically regardless of
    when a claim stamped ``claimed_at``. This is what lets
    :func:`verify_claim_receipt` reproject the head from the post-claim
    on-disk backlog and match the receipt.

    Args:
        rows: Ordered backlog rows (each a mapping, as produced by
            ``BacklogEntry.to_dict``).

    Returns:
        A ``sha256:`` prefixed hex digest of the normalised rows.
    """
    normalized = [{k: v for k, v in dict(row).items() if k not in _WALLCLOCK_FIELDS} for row in rows]
    return "sha256:" + hashlib.sha256(_canonical(normalized)).hexdigest()


def filter_digest(claim_filter: ClaimFilter) -> str:
    """Return the content-addressed digest of a claim filter's predicates.

    The digest covers exactly the eligibility inputs (project, role,
    capability, the sorted completed dependency set, and the attempt
    ceiling), so two receipts granted under the same filter carry the same
    ``filter_digest`` and a client can prove which eligibility policy a claim
    was granted under.

    Args:
        claim_filter: The :class:`~bernstein.core.tasks.claim.ClaimFilter`
            the claim was evaluated against.

    Returns:
        A ``sha256:`` prefixed hex digest of the filter predicates.
    """
    preimage = {
        "project": claim_filter.project,
        "role": claim_filter.role,
        "capability": claim_filter.capability,
        "completed_ids": sorted(claim_filter.completed_ids),
        "max_attempts": claim_filter.max_attempts,
    }
    return "sha256:" + hashlib.sha256(_canonical(preimage)).hexdigest()


def _claim_receipt_hash(
    *,
    task_id: str,
    claimer_card_fingerprint: str,
    backlog_head: str,
    filter_digest: str,
    chain_head: str,
    spec_revision: str,
) -> str:
    """Return the content-addressed digest that identifies a claim receipt.

    The pre-image is a canonical field tuple, so two receipts projecting the
    same claim hash identically and any tampered field surfaces as a
    different receipt hash. Wall-clock is deliberately excluded: the receipt
    is a deterministic projection, not a timestamped record.
    """
    preimage = _canonical(
        {
            "task_id": task_id,
            "claimer_card_fingerprint": claimer_card_fingerprint,
            "backlog_head": backlog_head,
            "filter_digest": filter_digest,
            "chain_head": chain_head,
            "spec_revision": spec_revision,
        }
    )
    return hashlib.sha256(preimage).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimReceipt:
    """The verifiable receipt a worker receives from a claim.

    The receipt is a projection of the claim decision, not standalone state:
    :attr:`backlog_head` and :attr:`filter_digest` anchor it to the backlog
    snapshot and eligibility filter, and :attr:`chain_head` embeds the
    audit-chain head the granting ``task.claim_receipt`` event recorded, so a
    client can later verify the claim it made against the audited run. Strip
    the chain and the signature and the receipt loses its meaning (an
    unprovable queue mutation), not merely its log.

    A refusal carries an empty :attr:`task_id`: a filter that matched no
    eligible row still produces a signed receipt, so a claim attempt is never
    a silent skip.

    Attributes:
        task_id: The claimed task id, or :data:`REFUSED_TASK_ID` on refusal.
        claimer_card_fingerprint: ``sha256:`` fingerprint of the claimer's
            agent card key, or ``"unregistered"``.
        backlog_head: Content-addressed digest of the backlog snapshot.
        filter_digest: Content-addressed digest of the claim filter.
        chain_head: The audit-chain head the granting event recorded (the
            ``prev_chain_digest`` embedded by ``record_task_claim_receipt``).
        spec_revision: The pinned Tasks-extension revision.
        signature: Base64url Ed25519 signature over the receipt hash.
        signer_public_key_pem: PEM public half of the signing identity.
    """

    task_id: str
    claimer_card_fingerprint: str
    backlog_head: str
    filter_digest: str
    chain_head: str
    spec_revision: str = SPEC_REVISION
    signature: str = ""
    signer_public_key_pem: str = ""

    @property
    def granted(self) -> bool:
        """Whether the claim was granted (a non-empty task id was claimed)."""
        return self.task_id != REFUSED_TASK_ID

    @property
    def receipt_hash(self) -> str:
        """The content-addressed digest of this receipt (the proof anchor)."""
        return _claim_receipt_hash(
            task_id=self.task_id,
            claimer_card_fingerprint=self.claimer_card_fingerprint,
            backlog_head=self.backlog_head,
            filter_digest=self.filter_digest,
            chain_head=self.chain_head,
            spec_revision=self.spec_revision,
        )

    @property
    def signed_bytes(self) -> bytes:
        """Return the canonical bytes the Ed25519 signature covers.

        The receipt hash already binds every canonical field, so signing it
        binds the whole receipt: tampering any field changes the hash and
        invalidates the signature.
        """
        return _canonical({"receipt_hash": self.receipt_hash})

    @property
    def poll_token(self) -> str:
        """An opaque, stateless token any server instance decodes to re-verify.

        The token carries only the receipt identity and the pinned revision -
        no session id and no server-side state - so a different instance
        answers a verify by reprojecting from the on-disk backlog and chain
        alone (stateless-core contract, #2506).
        """
        return encode_request_state(
            {
                "task_id": self.task_id,
                "backlog_head": self.backlog_head,
                "filter_digest": self.filter_digest,
                "chain_head": self.chain_head,
                "spec_revision": self.spec_revision,
            }
        )

    def to_wire(self) -> dict[str, Any]:
        """Return the claim-receipt body for a client."""
        return {
            "taskId": self.task_id,
            "granted": self.granted,
            "claimerCardFingerprint": self.claimer_card_fingerprint,
            "backlogHead": self.backlog_head,
            "filterDigest": self.filter_digest,
            "chainHead": self.chain_head,
            "specRevision": self.spec_revision,
            "receiptHash": self.receipt_hash,
            "signature": self.signature,
            "signerPublicKeyPem": self.signer_public_key_pem,
            "pollToken": self.poll_token,
        }

    @staticmethod
    def from_wire(wire: Mapping[str, Any]) -> ClaimReceipt:
        """Reconstruct a receipt from its :meth:`to_wire` body.

        The derived fields (``receiptHash``, ``pollToken``, ``granted``) are
        ignored: they are recomputed from the canonical fields, so a client
        cannot smuggle a mismatched hash past reconstruction.
        """
        return ClaimReceipt(
            task_id=str(wire["taskId"]),
            claimer_card_fingerprint=str(wire["claimerCardFingerprint"]),
            backlog_head=str(wire["backlogHead"]),
            filter_digest=str(wire["filterDigest"]),
            chain_head=str(wire["chainHead"]),
            spec_revision=str(wire.get("specRevision", SPEC_REVISION)),
            signature=str(wire.get("signature", "")),
            signer_public_key_pem=str(wire.get("signerPublicKeyPem", "")),
        )

    @staticmethod
    def granted_receipt(
        *,
        task_id: str,
        claimer_card_fingerprint: str,
        backlog_head: str,
        filter_digest: str,
        chain_head: str,
    ) -> ClaimReceipt:
        """Build an unsigned granted receipt for ``task_id``."""
        return ClaimReceipt(
            task_id=task_id,
            claimer_card_fingerprint=claimer_card_fingerprint,
            backlog_head=backlog_head,
            filter_digest=filter_digest,
            chain_head=chain_head,
        )

    @staticmethod
    def refusal(
        *,
        claimer_card_fingerprint: str,
        backlog_head: str,
        filter_digest: str,
        chain_head: str,
    ) -> ClaimReceipt:
        """Build an unsigned refusal receipt (no eligible row matched)."""
        return ClaimReceipt(
            task_id=REFUSED_TASK_ID,
            claimer_card_fingerprint=claimer_card_fingerprint,
            backlog_head=backlog_head,
            filter_digest=filter_digest,
            chain_head=chain_head,
        )


def sign_claim_receipt(receipt: ClaimReceipt, *, private_key_pem: str, public_key_pem: str) -> ClaimReceipt:
    """Return a copy of ``receipt`` Ed25519-signed with the install identity.

    The signature covers :attr:`ClaimReceipt.signed_bytes` (the receipt hash),
    so it binds every canonical field. The public half is embedded so a
    verifier checks the signature offline without any key lookup.

    Args:
        receipt: The unsigned receipt to sign.
        private_key_pem: PEM-encoded Ed25519 private key.
        public_key_pem: PEM-encoded Ed25519 public key to embed.

    Returns:
        A new signed :class:`ClaimReceipt`.
    """
    from bernstein.core.skills.catalog.signature import sign_payload

    signed = replace(receipt, signer_public_key_pem=public_key_pem)
    signature = sign_payload(signed.signed_bytes, private_key_pem)
    return replace(signed, signature=signature)


def verify_claim_receipt(
    receipt: ClaimReceipt,
    backlog_events: Iterable[Mapping[str, Any]],
    chain: AuditChainStore,
) -> tuple[bool, str | None]:
    """Confirm a claim receipt is a faithful, chain-anchored projection.

    The check is fully offline: it reads only the supplied backlog rows and
    the audit chain, no network and no running server. In order:

    1. Reproject :func:`backlog_head` from ``backlog_events`` and confirm it
       equals the receipt's ``backlog_head`` (the backlog reprojection).
    2. Verify the Ed25519 signature over the receipt hash, which binds every
       canonical field - a tampered ``claimer_card_fingerprint``,
       ``backlog_head``, or ``chain_head`` invalidates it.
    3. Verify the audit chain end to end.
    4. For a granted receipt, confirm a ``task.claim_receipt`` event for this
       task sits at the receipt's embedded chain head (its recorded
       ``prev_chain_digest``), tying the receipt to the audited claim.

    Args:
        receipt: The receipt presented by a client.
        backlog_events: The authoritative on-disk backlog rows.
        chain: The audit chain store the claim event was mirrored into.

    Returns:
        ``(True, None)`` when the receipt verifies, or ``(False, reason)``
        naming the first mismatch.
    """
    from bernstein.core.skills.catalog.signature import verify_payload

    expected_backlog_head = backlog_head(backlog_events)
    if receipt.backlog_head != expected_backlog_head:
        return False, "backlog_head does not match the on-disk backlog"

    if receipt.signature or receipt.signer_public_key_pem:
        outcome = verify_payload(
            receipt.signed_bytes,
            receipt.signature or None,
            receipt.signer_public_key_pem or None,
            allow_unverified=True,
        )
        if not outcome.verified:
            return False, f"receipt signature does not verify: {outcome.reason}"
    elif receipt.granted:
        return False, "granted receipt is not signed"

    ok, errors = chain.verify()
    if not ok:
        return False, f"audit chain does not verify: {'; '.join(errors) or 'unknown'}"

    if receipt.granted:
        for event in chain.query(event_type=EVENT_TASK_CLAIM_RECEIPT):
            details = event.details
            if (
                str(details.get("task_id", "")) == receipt.task_id
                and str(details.get("prev_chain_digest", "")) == receipt.chain_head
            ):
                return True, None
        return False, "no task.claim_receipt event at the embedded chain head"

    return True, None
