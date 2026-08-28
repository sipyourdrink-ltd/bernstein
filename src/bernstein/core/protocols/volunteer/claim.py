"""Volunteer claim protocol document.

A Claim records that a specific worker has taken responsibility for a specific
task at a specific moment.  It is a first-class signed artifact: the
dataclass is immutable, the canonical bytes are stable under key reordering,
and the DSSE envelope is verifiable offline by anyone holding the worker's
public key.

This module lives in the ``protocols/volunteer/`` namespace -- the formal
protocol documents that compose into the attestation substrate.  It is distinct
from :mod:`bernstein.core.volunteer.claim`, which implements the best-effort
GitHub-comment etiquette for coordinator-free claim signalling.  Both may coexist
because they serve different roles: the protocol document is cryptographically
verifiable evidence; the comment etiquette is a human-observable signalling
mechanism.

Design decisions
----------------

* **Frozen dataclass.**  ``Claim`` carries no mutable state.  Every field is
  set at construction and the object is hashable, making it safe to use as a
  dict key or cache member.

* **Validation at construction.**  Field constraints (non-empty strings,
  aware-datetime parse) are enforced in ``__post_init__``.  A ``Claim`` that
  passes construction is self-consistent.

* **Delegates DSSE to** :mod:`documents`.  ``build_claim_envelope`` calls
  the same DSSE machinery (``pae``, ``Envelope``, ``Statement``, ``Subject``,
  ``keyid_from_public_key``) that :func:`documents.sign_document` uses -- it
  is the same code path, just with a claim-specific predicate type.  No
  DSSE logic is re-implemented here; the delegation is to the *types*, not
  the *function*.

* **Deterministic signing.**  Ed25519 is deterministic by RFC 8032 §5.1.6.
  ``canonical_bytes`` guarantees the serialisation is byte-stable.  The
  combination means: same ``Claim`` + same Ed25519 key → byte-identical
  envelope on every run.  This property is asserted by the golden-vector test.

* **Claim-specific predicate type.**  The envelope uses
  ``https://bernstein.run/attestations/volunteer/claim/v1`` as the
  ``predicateType`` so verifiers can filter claim-only attestations without
  re-parsing the full document payload.  The ``document_kind`` field inside
  the predicate body is still set to ``"claim"`` for consistency.
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

#: Schema version for the claim document body.
CLAIM_SCHEMA_VERSION: str = "1.0.0"

#: Claim-specific predicate type URL.  Distinct from the base volunteer
#: predicate type so a verifier can filter claim-only attestations.
CLAIM_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/claim/v1"

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class ClaimError(ValueError):
    """Raised when a ``Claim`` field fails validation.

    Follows the :class:`VolunteerManifestError` pattern: the message names
    the offending field and the reason, making it self-documenting for callers
    and audit logs.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """A signed record of a worker's claim on a task.

    Attributes:
        schema_version: Document schema version, used for canonicalization.
        worker_id: Opaque identifier for the claiming worker (login handle,
            public-key fingerprint, or any stable string the project chooses).
            Must be a non-empty string.
        task_id: Opaque identifier for the task being claimed.  Must be a
            non-empty string.  The format (numeric, alphanumeric, URI) is
            project-specific and not enforced here.
        claimed_at: ISO-8601 timestamp with timezone (UTC recommended).  Must
            be parseable as a timezone-aware :class:`datetime`.  The format
            must include an offset or ``Z``; bare ``datetime`` (without
            ``tzinfo``) is rejected because it is ambiguous.
    """

    worker_id: str
    task_id: str
    claimed_at: str
    schema_version: str = CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Type guard: reject bool-as-int before anything else.
        for name, value in [
            ("schema_version", self.schema_version),
            ("worker_id", self.worker_id),
            ("task_id", self.task_id),
        ]:
            if isinstance(value, bool) or not isinstance(value, str):
                raise ClaimError(name, f"expected str, got {type(value).__name__}")
            if not value:
                raise ClaimError(name, "must be non-empty")

        if isinstance(self.claimed_at, bool) or not isinstance(self.claimed_at, str):
            raise ClaimError("claimed_at", f"expected str, got {type(self.claimed_at).__name__}")
        if not self.claimed_at:
            raise ClaimError("claimed_at", "must be non-empty")

        # Parse as aware datetime; reject naive datetimes.
        try:
            parsed = datetime.fromisoformat(self.claimed_at.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise ClaimError("claimed_at", f"not a valid ISO-8601 timestamp: {exc}") from None
        if parsed.tzinfo is None:
            raise ClaimError("claimed_at", "must be timezone-aware (include offset or Z suffix)")

    # -----------------------------------------------------------------------
    # Canonical form
    # -----------------------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the claim as a sorted, deterministic dict for signing.

        The field order is stable and round-tripping via ``Claim(**d)`` is
        lossless.  ``schema_version`` is included here as per the volunteer
        document substrate pattern.
        """
        return {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "claimed_at": self.claimed_at,
        }

    def digest(self) -> str:
        """SHA-256 hex digest of the canonical bytes (stable identity)."""
        return canonical_hash(self.to_canonical_dict())


# ---------------------------------------------------------------------------
# Sign / verify helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    """Outcome of :func:`verify_claim_envelope`.

    Attributes:
        ok: True iff the envelope signature verified, the predicate type
            matched, and the embedded claim re-serialised to the attested
            subject digest.
        claim: The embedded claim dict (populated even on failure when the
            envelope was parseable).
        keyid: keyid of the signature that successfully verified (empty on
            failure).
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    claim: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: tuple[str, ...] = ()


def build_claim_envelope(
    claim: Claim,
    signing_key: Ed25519PrivateKey,
) -> Envelope:
    """Sign a ``Claim`` into a DSSE envelope.

    Builds the same envelope structure as :func:`documents.sign_document` but
    uses :data:`CLAIM_PREDICATE_TYPE` as the ``predicateType`` instead of the
    base volunteer document predicate type, so verifiers can filter
    claim-only attestations without re-parsing the full payload.

    Args:
        claim: The claim to sign.  Must be a valid ``Claim`` instance.
        signing_key: Ed25519 private key used to sign the PAE input.

    Returns:
        A signed :class:`Envelope` ready to be persisted or transmitted.

    Raises:
        ClaimError: If ``claim`` is not a ``Claim`` instance.
    """
    if not isinstance(claim, Claim):
        raise ClaimError("<claim>", f"expected Claim, got {type(claim).__name__}")

    doc = claim.to_canonical_dict()
    doc_bytes = canonical_bytes(doc)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    subject = Subject(
        name=f"claim-{claim.task_id}",
        digest={"sha256": digest},
    )

    predicate: dict[str, Any] = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": "claim",
        "document": doc,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=CLAIM_PREDICATE_TYPE,
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


def verify_claim_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
) -> ClaimVerification:
    """Verify a claim envelope.

    Verifies the DSSE signature and predicate type, then validates the embedded
    claim document against the expected schema.  The predicate type must match
    :data:`CLAIM_PREDICATE_TYPE`; the ``document_kind`` inside the predicate
    body must be ``"claim"``.

    This function does **not** delegate to :func:`documents.verify_document`
    because that function hard-codes :data:`VOLUNTEER_DOCUMENT_PREDICATE_TYPE`
    as the expected predicate type.  Instead, it calls
    :func:`audit_dsse.verify_envelope` directly with
    :data:`CLAIM_PREDICATE_TYPE`.

    Args:
        envelope: Parsed DSSE envelope (typically from
            :func:`bernstein.core.security.audit_dsse.parse_envelope`).
        public_key: Ed25519 public key the signer used.

    Returns:
        :class:`ClaimVerification` with ``ok`` flag and details.
    """
    errors: list[str] = []

    # Verify the DSSE envelope with the claim-specific predicate type.
    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=CLAIM_PREDICATE_TYPE,
    )
    if not env_v.ok:
        return ClaimVerification(
            ok=False,
            errors=tuple(env_v.errors),
        )

    # Extract the embedded document.
    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return ClaimVerification(
            ok=False,
            errors=(f"predicate is {type(raw_predicate).__name__}, expected dict",),
        )

    raw_document = predicate_dict.get("document", {})
    document = raw_document if isinstance(raw_document, dict) else {}
    if not isinstance(raw_document, dict):
        errors.append(f"document is {type(raw_document).__name__}, expected dict")

    # Internal hash consistency: the embedded document must reproduce the
    # subject digest byte-for-byte.
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

    # document_kind must be "claim".
    document_kind = predicate_dict.get("document_kind", "")
    if document_kind != "claim":
        errors.append(
            f"document_kind is {document_kind!r}, expected 'claim'",
        )

    return ClaimVerification(
        ok=not errors,
        claim=document,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
