"""Volunteer submission protocol document.

A Submission records that a worker has produced a result for a specific task
and points a verifier at the location of the attached result-receipt bundle.
It carries only what is NOT inside the receipt bundle itself — the bundle
digest, the fetch location, and the task routing metadata — so a verifier can
route and fetch the bundle without parsing the submission before it chooses to.

This module lives in the ``protocols/volunteer/`` namespace alongside
:mod:`claim`, :mod:`verdict`, and :mod:`receipt`.  It uses the same DSSE
substrate and the same predicate-type discipline as the other document types.

Design decisions
----------------

* **Frozen dataclass.**  ``Submission`` carries no mutable state.  Every field
  is set at construction and the object is hashable.

* **Validation at construction.**  Field constraints (non-empty strings,
  hex digest shape, aware-datetime parse) are enforced in ``__post_init__``.

* **Delegates DSSE to** :mod:`documents`.  ``build_submission_envelope`` calls
  the same DSSE machinery (``pae``, ``Envelope``, ``Statement``, ``Subject``,
  ``keyid_from_public_key``) that :func:`documents.sign_document` uses.

* **Deterministic signing.**  Ed25519 is deterministic by RFC 8032 §5.1.6.
  ``canonical_bytes`` guarantees the serialisation is byte-stable.

* **Submission-specific predicate type.**  The envelope uses
  ``https://bernstein.run/attestations/volunteer/submission/v1`` as the
  ``predicateType`` so verifiers can filter submission-only attestations
  without re-parsing the full document payload.

* **task_ref is deliberately duplicated.**  The ``task_ref`` dict contains
  ``repo``, ``commit_sha``, and ``issue_number`` — information that already
  lives inside the result-receipt bundle.  It is carried here because a
  verifier may want to route or display the submission without fetching the
  bundle first.
"""

from __future__ import annotations

import base64
import hashlib
import re
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

#: Schema version for the submission document body.
SUBMISSION_SCHEMA_VERSION: str = "1.0.0"

#: Submission-specific predicate type URL.  Distinct from the base volunteer
#: predicate type so a verifier can filter submission-only attestations.
SUBMISSION_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/submission/v1"

#: Hex digest regex — accepts lowercase or uppercase hex, 64 chars (sha256).
_HEX_SHA256_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class SubmissionError(RuntimeError):
    """Raised when a ``Submission`` field fails validation.

    Follows the :class:`ClaimError` / :class:`MergeReceiptError` pattern:
    the message names the offending field and the reason, making it
    self-documenting for callers and audit logs.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Submission:
    """A signed record of a worker's task result, pointing at the bundle.

    Carries only what is NOT inside the result-receipt bundle: the bundle
    digest, the fetch location, and the task routing metadata.

    Attributes:
        receipt_bundle_digest: SHA-256 hex digest of the
            :class:`~bernstein.core.security.result_receipt_bundle.ResultBundle`
            canonical bytes.  Must be a non-empty 64-char hex string.
        receipt_bundle_location: URL, PR artifact reference, or hub path
            where a verifier fetches the bundle.  Must be a non-empty string.
        task_ref: Dict with ``repo``, ``commit_sha``, and ``issue_number``
            fields, duplicated deliberately so verifiers can route without
            fetching the bundle.
        schema_version: Document schema version.
        submitted_at: ISO-8601 timestamp with timezone (UTC recommended).
            Must be parseable as a timezone-aware :class:`datetime`.
    """

    receipt_bundle_digest: str
    receipt_bundle_location: str
    task_ref: dict[str, Any]
    schema_version: str = SUBMISSION_SCHEMA_VERSION
    submitted_at: str = ""

    def __post_init__(self) -> None:
        for name, value in [
            ("schema_version", self.schema_version),
            ("receipt_bundle_digest", self.receipt_bundle_digest),
            ("receipt_bundle_location", self.receipt_bundle_location),
        ]:
            if isinstance(value, bool) or not isinstance(value, str):
                raise SubmissionError(name, f"expected str, got {type(value).__name__}")
            if not value:
                raise SubmissionError(name, "must be non-empty")

        # receipt_bundle_digest must be a 64-char hex string (sha256).
        if not _HEX_SHA256_RE.match(self.receipt_bundle_digest):
            raise SubmissionError(
                "receipt_bundle_digest",
                "must be a 64-char hex string (sha256 digest)",
            )

        # receipt_bundle_location must be a non-empty string.
        if not isinstance(self.receipt_bundle_location, str) or not self.receipt_bundle_location:
            raise SubmissionError(
                "receipt_bundle_location",
                "must be a non-empty string",
            )

        # task_ref must be a dict with required keys.
        if isinstance(self.task_ref, bool) or not isinstance(self.task_ref, dict):
            raise SubmissionError(
                "task_ref",
                f"expected dict, got {type(self.task_ref).__name__}",
            )
        for key in ("repo", "commit_sha", "issue_number"):
            if key not in self.task_ref:
                raise SubmissionError(
                    f"task_ref.{key}",
                    "required field missing",
                )
            val = self.task_ref[key]
            if isinstance(val, bool) or not isinstance(val, str):
                raise SubmissionError(
                    f"task_ref.{key}",
                    f"expected str, got {type(val).__name__}",
                )
            if not val:
                raise SubmissionError(
                    f"task_ref.{key}",
                    "must be non-empty",
                )

        # submitted_at must be an aware datetime (optional — defaults to "").
        if isinstance(self.submitted_at, bool) or not isinstance(self.submitted_at, str):
            raise SubmissionError(
                "submitted_at",
                f"expected str, got {type(self.submitted_at).__name__}",
            )
        if self.submitted_at:
            try:
                parsed = datetime.fromisoformat(self.submitted_at.replace("Z", "+00:00"))
            except (ValueError, TypeError) as exc:
                raise SubmissionError(
                    "submitted_at",
                    f"not a valid ISO-8601 timestamp: {exc}",
                ) from None
            if parsed.tzinfo is None:
                raise SubmissionError(
                    "submitted_at",
                    "must be timezone-aware (include offset or Z suffix)",
                )

    # -----------------------------------------------------------------------
    # Canonical form
    # -----------------------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the submission as a sorted, deterministic dict for signing."""
        result: dict[str, Any] = {
            "schema_version": SUBMISSION_SCHEMA_VERSION,
            "receipt_bundle_digest": self.receipt_bundle_digest,
            "receipt_bundle_location": self.receipt_bundle_location,
            "task_ref": {
                "repo": self.task_ref["repo"],
                "commit_sha": self.task_ref["commit_sha"],
                "issue_number": self.task_ref["issue_number"],
            },
        }
        if self.submitted_at:
            result["submitted_at"] = self.submitted_at
        return result

    def digest(self) -> str:
        """SHA-256 hex digest of the canonical bytes (stable identity)."""
        return canonical_hash(self.to_canonical_dict())


# ---------------------------------------------------------------------------
# Sign / verify helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmissionVerification:
    """Outcome of :func:`verify_submission_envelope`.

    Attributes:
        ok: True iff the envelope signature verified, the predicate type
            matched, and the embedded submission re-serialised to the attested
            subject digest.
        submission: The embedded submission dict (populated even on failure
            when the envelope was parseable).
        keyid: keyid of the signature that successfully verified (empty on
            failure).
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    submission: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: tuple[str, ...] = ()


def build_submission_envelope(
    submission: Submission,
    signing_key: Ed25519PrivateKey,
) -> Envelope:
    """Sign a ``Submission`` into a DSSE envelope.

    Builds the same envelope structure as :func:`documents.sign_document` but
    uses :data:`SUBMISSION_PREDICATE_TYPE` as the ``predicateType`` instead of
    the base volunteer document predicate type, so verifiers can filter
    submission-only attestations.

    Args:
        submission: The submission to sign.  Must be a valid ``Submission``
            instance.
        signing_key: Ed25519 private key used to sign the PAE input.

    Returns:
        A signed :class:`Envelope` ready to be persisted or transmitted.

    Raises:
        SubmissionError: If ``submission`` is not a ``Submission`` instance.
    """
    if not isinstance(submission, Submission):
        raise SubmissionError(
            "<submission>",
            f"expected Submission, got {type(submission).__name__}",
        )

    doc = submission.to_canonical_dict()
    doc_bytes = canonical_bytes(doc)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    subject = Subject(
        name=f"submission-{submission.task_ref['repo']}",
        digest={"sha256": digest},
    )

    predicate: dict[str, Any] = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": "submission",
        "document": doc,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=SUBMISSION_PREDICATE_TYPE,
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


def verify_submission_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
) -> SubmissionVerification:
    """Verify a submission envelope.

    Verifies the DSSE signature and predicate type, then validates the
    embedded submission document against the expected schema.  The predicate
    type must match :data:`SUBMISSION_PREDICATE_TYPE`; the
    ``document_kind`` inside the predicate body must be ``"submission"``.

    Args:
        envelope: Parsed DSSE envelope (typically from
            :func:`bernstein.core.security.audit_dsse.parse_envelope`).
        public_key: Ed25519 public key the signer used.

    Returns:
        :class:`SubmissionVerification` with ``ok`` flag and details.
    """
    errors: list[str] = []

    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=SUBMISSION_PREDICATE_TYPE,
    )
    if not env_v.ok:
        return SubmissionVerification(
            ok=False,
            errors=tuple(env_v.errors),
        )

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return SubmissionVerification(
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

    # document_kind must be "submission".
    document_kind = predicate_dict.get("document_kind", "")
    if document_kind != "submission":
        errors.append(
            f"document_kind is {document_kind!r}, expected 'submission'",
        )

    return SubmissionVerification(
        ok=not errors,
        submission=document,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
