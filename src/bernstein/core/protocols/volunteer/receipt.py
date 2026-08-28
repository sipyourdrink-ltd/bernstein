"""Volunteer merge receipt protocol document.

A MergeReceipt records that a work-item was merged and the contributor
rewarded.  It closes the volunteer protocol lifecycle by tying together the
submission, verdict, and an out-of-band reward decision.  It is a first-class
signed artifact: the dataclass is immutable, the canonical bytes are stable,
and the DSSE envelope is verifiable offline by anyone holding the maintainer's
public key.

Design decisions
----------------

* **Frozen dataclass.**  ``MergeReceipt`` carries no mutable state.

* **Validation at construction.**  Field constraints (non-empty strings,
  aware-datetime parse, valid reward structure) are enforced in
  ``__post_init__``.

* **Delegates DSSE to** :mod:`documents`.  ``build_merge_receipt_envelope``
  calls the same DSSE machinery (``pae``, ``Envelope``, ``Statement``,
  ``Subject``, ``keyid_from_public_key``) that
  :func:`documents.sign_document` uses.

* **Deterministic signing.**  Ed25519 is deterministic by RFC 8032 §5.1.6.
  ``canonical_bytes`` guarantees the serialisation is byte-stable.

* **Receipt-specific predicate type.**  The envelope uses
  ``https://bernstein.run/attestations/volunteer/receipt/v1`` as the
  ``predicateType`` so verifiers can filter receipt-only attestations.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.protocols.volunteer.documents import (
    VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
    canonical_bytes,
    canonical_hash,
)
from bernstein.core.security.audit_dsse import (
    DSSE_PAYLOAD_TYPE,
    Envelope,
    Signature,
    Statement,
    Subject,
    keyid_from_public_key,
    pae,
    verify_envelope,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version for the merge receipt document body.
RECEIPT_SCHEMA_VERSION: str = "1.0.0"

#: Merge receipt-specific predicate type URL.  Distinct from the base
#: volunteer predicate type so a verifier can filter receipt-only attestations.
RECEIPT_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/receipt/v1"

#: Valid reward kinds.
VALID_REWARD_KINDS: frozenset[str] = frozenset({"points", "badge", "bounty"})

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class MergeReceiptError(ValueError):
    """Raised when a ``MergeReceipt`` field fails validation."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MergeReceipt:
    """A signed record that a work-item was merged and the contributor rewarded.

    Attributes:
        submission_digest: SHA-256 digest of the submission document.  Must
            be a non-empty hex string.
        merged_by_keyid: keyid of the maintainer who merged.  Must be a
            non-empty string.
        merged_at: ISO-8601 timestamp with timezone.  Must be parseable as a
            timezone-aware :class:`datetime`.
        reward: Reward granted to the contributor.  Has ``kind`` (one of
            ``points``, ``badge``, ``bounty``), ``amount`` (``int``,
            ``float``, or ``None``), and ``label`` (``str``).
        task_id: The task that was merged.  Must be a non-empty string.
        pr_number: The GitHub PR number if available, else ``None``.
        schema_version: Document schema version.
        notes: Optional human-readable notes.
    """

    submission_digest: str
    merged_by_keyid: str
    merged_at: str
    reward: dict[str, Any]
    task_id: str
    pr_number: int | None = None
    schema_version: str = RECEIPT_SCHEMA_VERSION
    notes: str | None = None

    def __post_init__(self) -> None:
        for name, value in [
            ("submission_digest", self.submission_digest),
            ("merged_by_keyid", self.merged_by_keyid),
            ("merged_at", self.merged_at),
            ("task_id", self.task_id),
        ]:
            if isinstance(value, bool) or not isinstance(value, str):
                raise MergeReceiptError(name, f"expected str, got {type(value).__name__}")
            if not value:
                raise MergeReceiptError(name, "must be non-empty")

        # merged_at must be aware datetime.
        try:
            parsed = datetime.fromisoformat(self.merged_at.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise MergeReceiptError("merged_at", f"not a valid ISO-8601 timestamp: {exc}") from None
        if parsed.tzinfo is None:
            raise MergeReceiptError("merged_at", "must be timezone-aware (include offset or Z suffix)")

        # reward must be a dict with valid kind, amount, label.
        if isinstance(self.reward, bool) or not isinstance(self.reward, dict):
            raise MergeReceiptError("reward", f"expected dict, got {type(self.reward).__name__}")
        if "kind" not in self.reward:
            raise MergeReceiptError("reward", "missing 'kind' key")
        kind = self.reward.get("kind")
        if not isinstance(kind, str):
            raise MergeReceiptError("reward.kind", f"expected str, got {type(kind).__name__}")
        if kind not in VALID_REWARD_KINDS:
            raise MergeReceiptError(
                "reward.kind",
                f"must be one of {sorted(VALID_REWARD_KINDS)}, got {kind!r}",
            )
        if "amount" not in self.reward:
            raise MergeReceiptError("reward", "missing 'amount' key")
        amount = self.reward.get("amount")
        if amount is not None and not isinstance(amount, (int, float)):
            raise MergeReceiptError(
                "reward.amount",
                f"expected int, float, or null, got {type(amount).__name__}",
            )
        if "label" not in self.reward:
            raise MergeReceiptError("reward", "missing 'label' key")
        label = self.reward.get("label")
        if not isinstance(label, str):
            raise MergeReceiptError("reward.label", f"expected str, got {type(label).__name__}")

        # pr_number is optional, but must be int if provided.
        if self.pr_number is not None and not isinstance(self.pr_number, int):
            raise MergeReceiptError(
                "pr_number",
                f"expected int or None, got {type(self.pr_number).__name__}",
            )

        # notes is optional but must be str if provided.
        if self.notes is not None and not isinstance(self.notes, str):
            raise MergeReceiptError("notes", f"expected str or None, got {type(self.notes).__name__}")

    # ---------------------------------------------------------------------------
    # Canonical form
    # ---------------------------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the receipt as a sorted, deterministic dict for signing."""
        result: dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "submission_digest": self.submission_digest,
            "merged_by_keyid": self.merged_by_keyid,
            "merged_at": self.merged_at,
            "reward": self.reward,
            "task_id": self.task_id,
        }
        if self.pr_number is not None:
            result["pr_number"] = self.pr_number
        if self.notes is not None:
            result["notes"] = self.notes
        return result

    def digest(self) -> str:
        """SHA-256 hex digest of the canonical bytes."""
        return canonical_hash(self.to_canonical_dict())


# ---------------------------------------------------------------------------
# Sign / verify helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MergeReceiptVerification:
    """Outcome of :func:`verify_merge_receipt_envelope`.

    Attributes:
        ok: True iff the envelope signature verified, the predicate type
            matched, and the embedded receipt re-serialised to the attested
            subject digest.
        receipt: The embedded receipt dict.
        keyid: keyid of the signature that successfully verified.
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    receipt: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: tuple[str, ...] = ()


def build_merge_receipt_envelope(
    receipt: MergeReceipt,
    signing_key: Ed25519PrivateKey,
) -> Envelope:
    """Sign a ``MergeReceipt`` into a DSSE envelope.

    Args:
        receipt: The receipt to sign.
        signing_key: Ed25519 private key used to sign the PAE input.

    Returns:
        A signed :class:`Envelope`.

    Raises:
        MergeReceiptError: If ``receipt`` is not a ``MergeReceipt`` instance.
    """
    if not isinstance(receipt, MergeReceipt):
        raise MergeReceiptError("<receipt>", f"expected MergeReceipt, got {type(receipt).__name__}")

    doc = receipt.to_canonical_dict()
    doc_bytes = canonical_bytes(doc)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    subject = Subject(
        name=f"merge-receipt-{receipt.task_id}",
        digest={"sha256": digest},
    )

    predicate: dict[str, Any] = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": "merge-receipt",
        "document": doc,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=RECEIPT_PREDICATE_TYPE,
        predicate=predicate,
    )

    payload = canonical_bytes(statement.to_dict())
    pae_bytes = pae(DSSE_PAYLOAD_TYPE, payload)
    signature = signing_key.sign(pae_bytes)
    keyid = keyid_from_public_key(signing_key.public_key())

    return Envelope(
        payload_type=DSSE_PAYLOAD_TYPE,
        payload_b64=base64.b64encode(payload).decode("ascii"),
        signatures=[Signature(keyid=keyid, sig=base64.b64encode(signature).decode("ascii"))],
    )


def verify_merge_receipt_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
) -> MergeReceiptVerification:
    """Verify a merge receipt envelope.

    Args:
        envelope: Parsed DSSE envelope.
        public_key: Ed25519 public key the signer used.

    Returns:
        :class:`MergeReceiptVerification` with ``ok`` flag and details.
    """
    errors: list[str] = []

    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=RECEIPT_PREDICATE_TYPE,
    )
    if not env_v.ok:
        return MergeReceiptVerification(ok=False, errors=tuple(env_v.errors))

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return MergeReceiptVerification(
            ok=False,
            errors=(f"predicate is {type(raw_predicate).__name__}, expected dict",),
        )

    raw_document = predicate_dict.get("document", {})
    document = raw_document if isinstance(raw_document, dict) else {}
    if not isinstance(raw_document, dict):
        errors.append(f"document is {type(raw_document).__name__}, expected dict")

    # Internal hash consistency.
    raw_subject = statement.get("subject", [])
    attested_digest = ""
    if isinstance(raw_subject, list) and raw_subject:
        first_subject = raw_subject[0]
        if isinstance(first_subject, dict):
            digest_dict = first_subject.get("digest", {})
            if isinstance(digest_dict, dict):
                attested_digest = digest_dict.get("sha256", "")

    if document and attested_digest:
        recomputed = hashlib.sha256(canonical_bytes(document)).hexdigest()
        if recomputed != attested_digest:
            errors.append(
                f"embedded document hashes to {recomputed}, envelope attests {attested_digest}",
            )

    # document_kind must be "merge-receipt".
    document_kind = predicate_dict.get("document_kind", "")
    if document_kind != "merge-receipt":
        errors.append(
            f"document_kind is {document_kind!r}, expected 'merge-receipt'",
        )

    return MergeReceiptVerification(
        ok=not errors,
        receipt=document,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
