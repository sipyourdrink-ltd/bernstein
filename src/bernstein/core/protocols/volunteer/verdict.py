"""Volunteer verification verdict protocol document.

A VerificationVerdict records the outcome of running gates on a submission.
It carries a recommendation (accept / request-changes / reject / no-gates),
per-gate pass/fail summary (not full logs — those live in the receipt bundle),
and the verifier's keyid.  It is a first-class signed artifact: the dataclass
is immutable, the canonical bytes are stable, and the DSSE envelope is
verifiable offline by anyone holding the verifier's public key.

Design decisions
----------------

* **Frozen dataclass.**  ``VerificationVerdict`` carries no mutable state.
  Every field is set at construction and the object is hashable.

* **Validation at construction.**  Field constraints (non-empty strings,
  aware-datetime parse, valid recommendation enum) are enforced in
  ``__post_init__``.

* **Delegates DSSE to** :mod:`documents`.  ``build_verdict_envelope`` calls
  the same DSSE machinery (``pae``, ``Envelope``, ``Statement``, ``Subject``,
  ``keyid_from_public_key``) that :func:`documents.sign_document` uses.

* **Deterministic signing.**  Ed25519 is deterministic by RFC 8032 §5.1.6.
  ``canonical_bytes`` guarantees the serialisation is byte-stable.

* **Verdict-specific predicate type.**  The envelope uses
  ``https://bernstein.run/attestations/volunteer/verdict/v1`` as the
  ``predicateType`` so verifiers can filter verdict-only attestations without
  re-parsing the full document payload.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
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

#: Schema version for the verdict document body.
VERDICT_SCHEMA_VERSION: str = "1.0.0"

#: Verdict-specific predicate type URL.  Distinct from the base volunteer
#: predicate type so a verifier can filter verdict-only attestations.
VERDICT_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/verdict/v1"

# ---------------------------------------------------------------------------
# Recommendation enum
# ---------------------------------------------------------------------------


class Recommendation(str, Enum):  # noqa: UP042
    """Gate-run outcome recommendation.

    Attributes:
        ACCEPT: Gates passed; submission is ready to merge.
        REQUEST_CHANGES: Gates failed; maintainer review needed.
        REJECT: Manifest mismatch or security concern; do not merge.
        NO_GATES: No gates were run (e.g. manifest not declared).
    """

    ACCEPT = "accept"
    REQUEST_CHANGES = "request-changes"
    REJECT = "reject"
    NO_GATES = "no-gates"


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class VerdictError(ValueError):
    """Raised when a ``VerificationVerdict`` field fails validation.

    Follows the :class:`ClaimError` pattern: the message names the offending
    field and the reason.
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationVerdict:
    """A signed record of a verifier's gate-run outcome on a submission.

    Attributes:
        submission_digest: SHA-256 digest of the submission document this
            verdict is about.  Must be a non-empty hex string.
        gate_results: Per-gate pass/fail summary.  Each entry is a dict with
            ``command`` (str) and ``passed`` (bool).  NOT full logs — those
            live in the receipt bundle.
        verifier_keyid: keyid of the verifier who signed this verdict.
            Must be a non-empty string.
        recommendation: One of the :class:`Recommendation` enum values.
        verified_at: ISO-8601 timestamp with timezone (UTC recommended).
            Must be parseable as a timezone-aware :class:`datetime`.
        schema_version: Document schema version.
        notes: Optional human-readable notes from the verifier.
    """

    submission_digest: str
    gate_results: list[dict[str, Any]]
    verifier_keyid: str
    recommendation: str
    verified_at: str
    schema_version: str = VERDICT_SCHEMA_VERSION
    notes: str | None = None

    def __post_init__(self) -> None:
        for name, value in [
            ("submission_digest", self.submission_digest),
            ("verifier_keyid", self.verifier_keyid),
            ("recommendation", self.recommendation),
        ]:
            if isinstance(value, bool) or not isinstance(value, str):
                raise VerdictError(name, f"expected str, got {type(value).__name__}")
            if not value:
                raise VerdictError(name, "must be non-empty")

        # Validate recommendation against the enum.
        valid_values = {r.value for r in Recommendation}
        if self.recommendation not in valid_values:
            raise VerdictError(
                "recommendation",
                f"must be one of {sorted(valid_values)}, got {self.recommendation!r}",
            )

        # gate_results must be a list of dicts with 'command' and 'passed'.
        if isinstance(self.gate_results, bool) or not isinstance(self.gate_results, list):
            raise VerdictError(
                "gate_results",
                f"expected list, got {type(self.gate_results).__name__}",
            )
        for i, entry in enumerate(self.gate_results):
            if not isinstance(entry, dict):
                raise VerdictError(
                    f"gate_results[{i}]",
                    f"expected dict, got {type(entry).__name__}",
                )
            if "command" not in entry:
                raise VerdictError(f"gate_results[{i}]", "missing 'command' key")
            if "passed" not in entry:
                raise VerdictError(f"gate_results[{i}]", "missing 'passed' key")
            if not isinstance(entry.get("command"), str):
                raise VerdictError(
                    f"gate_results[{i}].command",
                    f"expected str, got {type(entry.get('command')).__name__}",
                )
            if not isinstance(entry.get("passed"), bool):
                raise VerdictError(
                    f"gate_results[{i}].passed",
                    f"expected bool, got {type(entry.get('passed')).__name__}",
                )

        # verified_at must be an aware datetime.
        if isinstance(self.verified_at, bool) or not isinstance(self.verified_at, str):
            raise VerdictError("verified_at", f"expected str, got {type(self.verified_at).__name__}")
        if not self.verified_at:
            raise VerdictError("verified_at", "must be non-empty")
        try:
            parsed = datetime.fromisoformat(self.verified_at.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise VerdictError("verified_at", f"not a valid ISO-8601 timestamp: {exc}") from None
        if parsed.tzinfo is None:
            raise VerdictError("verified_at", "must be timezone-aware (include offset or Z suffix)")

        # notes is optional but must be str or None if provided.
        if self.notes is not None and not isinstance(self.notes, str):
            raise VerdictError("notes", f"expected str or None, got {type(self.notes).__name__}")

    # ---------------------------------------------------------------------------
    # Canonical form
    # ---------------------------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the verdict as a sorted, deterministic dict for signing."""
        result: dict[str, Any] = {
            "schema_version": VERDICT_SCHEMA_VERSION,
            "submission_digest": self.submission_digest,
            "gate_results": self.gate_results,
            "verifier_keyid": self.verifier_keyid,
            "recommendation": self.recommendation,
            "verified_at": self.verified_at,
        }
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
class VerdictVerification:
    """Outcome of :func:`verify_verdict_envelope`.

    Attributes:
        ok: True iff the envelope signature verified, the predicate type
            matched, and the embedded verdict re-serialised to the attested
            subject digest.
        verdict: The embedded verdict dict (populated even on failure when the
            envelope was parseable).
        keyid: keyid of the signature that successfully verified (empty on
            failure).
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    verdict: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: tuple[str, ...] = ()


def build_verdict_envelope(
    verdict: VerificationVerdict,
    signing_key: Ed25519PrivateKey,
) -> Envelope:
    """Sign a ``VerificationVerdict`` into a DSSE envelope.

    Args:
        verdict: The verdict to sign.
        signing_key: Ed25519 private key used to sign the PAE input.

    Returns:
        A signed :class:`Envelope`.

    Raises:
        VerdictError: If ``verdict`` is not a ``VerificationVerdict`` instance.
    """
    if not isinstance(verdict, VerificationVerdict):
        raise VerdictError("<verdict>", f"expected VerificationVerdict, got {type(verdict).__name__}")

    doc = verdict.to_canonical_dict()
    doc_bytes = canonical_bytes(doc)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    subject = Subject(
        name=f"verdict-{verdict.submission_digest[:16]}",
        digest={"sha256": digest},
    )

    predicate: dict[str, Any] = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": "verification-verdict",
        "document": doc,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=VERDICT_PREDICATE_TYPE,
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


def verify_verdict_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
) -> VerdictVerification:
    """Verify a verdict envelope.

    Verifies the DSSE signature and predicate type, then validates the embedded
    verdict document.  The predicate type must match :data:`VERDICT_PREDICATE_TYPE`.

    Args:
        envelope: Parsed DSSE envelope.
        public_key: Ed25519 public key the signer used.

    Returns:
        :class:`VerdictVerification` with ``ok`` flag and details.
    """
    errors: list[str] = []

    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=VERDICT_PREDICATE_TYPE,
    )
    if not env_v.ok:
        return VerdictVerification(ok=False, errors=tuple(env_v.errors))

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return VerdictVerification(
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

    # document_kind must be "verification-verdict".
    document_kind = predicate_dict.get("document_kind", "")
    if document_kind != "verification-verdict":
        errors.append(
            f"document_kind is {document_kind!r}, expected 'verification-verdict'",
        )

    return VerdictVerification(
        ok=not errors,
        verdict=document,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
