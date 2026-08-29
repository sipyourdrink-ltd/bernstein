"""Project card protocol document.

A ProjectCard represents what a project offers to volunteers — the task types it
supports, requirements, demand, and current status.  It is built from the
VolunteerManifest but does not import or depend on the manifest module
directly, keeping the dependency flow one-way: protocols/volunteer → core/volunteer.

Design decisions
-----------------

* **One-way dependency.**  The project card is built *from* the manifest, never
  the other way around.  This matches how the manifest already works and
  prevents the manifest from becoming a dependency of the not-yet-shipped
  protocol layer.

* **Typed, closed schema.**  The card uses a fixed set of fields with explicit
  types.  No field is ``dict[str, Any]`` or otherwise open-ended, unlike
  submission, claim, or receipt documents.  This satisfies the security
  requirement that worker cards must be structurally incapable of carrying a
  credential (no env-var-style strings that could smuggle credentials).

* **Credential name denylist.**  The schema includes a denylist of string
  field values that look like environment-variable-style credentials
  (case-insensitive substrings: ``"KEY"``, ``"TOKEN"``, ``"SECRET"``,
  ``"PASSWORD"``, ``"CREDENTIAL"``).  Any string field value containing one
  of these substrings is rejected during construction.

* **Deterministic signing.**  Ed25519 is deterministic by RFC 8032 §5.1.6.
  ``canonical_bytes`` guarantees the serialisation is byte-stable.

* **Predicate type URL.**  The envelope uses
  ``https://bernstein.run/attestations/volunteer/project_card/v1`` as the
  ``predicateType`` so verifiers can filter project-card-only attestations
  without re-parsing the full document payload.
"""

from __future__ import annotations

import base64
import hashlib
import json
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

#: Schema version for the project card document body.
PROJECT_CARD_SCHEMA_VERSION: str = "1.0.0"

#: Project card-specific predicate type URL.  Distinct from the base volunteer
#: predicate type so a verifier can filter project-card-only attestations.
PROJECT_CARD_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/project_card/v1"

#: Hex digest regex — accepts lowercase or uppercase hex, 64 chars (sha256).
_PROJECT_CARD_DIGEST_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class ProjectCardError(ValueError):
    """Raised when a ``ProjectCard`` field fails validation.

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
class ProjectCard:
    """A typed, closed schema representing what a project offers to volunteers.

    Attributes:
        schema_version: Document schema version.
        demand: Human-readable description of current demand for the project's
            task types (e.g. \"high\", \"moderate\", \"low\").
        task_types: List of task type identifiers that the project supports.
        requirements: Human-readable description of requirements for task types.
        demand_snapshot: Structured snapshot of demand (counts, duration bands,
            status, etc.).
        status: Project status (\"active\" or \"paused\").
        submitted_at: ISO-8601 timestamp with timezone (UTC recommended).
        schema_version: Document schema version.
        notes: Optional human-readable notes.
    """

    demand: str
    task_types: list[str]
    requirements: str
    demand_snapshot: dict[str, Any]
    status: str
    submitted_at: str
    schema_version: str = PROJECT_CARD_SCHEMA_VERSION
    notes: str | None = None

    def __post_init__(self) -> None:
        # Credential name denylist — reject any string field value containing
        # credential-like fragments (KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL).
        _CREDENTIAL_FRAGMENTS = ("key", "token", "secret", "password", "credential")

        def _check_str(name: str, value: Any) -> None:
            if isinstance(value, bool) or not isinstance(value, str):
                raise ProjectCardError(name, f"expected str, got {type(value).__name__}")
            if not value:
                raise ProjectCardError(name, "must be non-empty")

        def _check_credential(name: str, value: str) -> None:
            field_lower = value.lower()
            for frag in _CREDENTIAL_FRAGMENTS:
                if frag in field_lower:
                    raise ProjectCardError(name, f"contains credential-like fragment {value!r}")

        # Validate string scalar fields.
        for name, value in [
            ("schema_version", self.schema_version),
            ("demand", self.demand),
            ("requirements", self.requirements),
            ("status", self.status),
            ("submitted_at", self.submitted_at),
        ]:
            _check_str(name, value)

        # Credential denylist for string content.
        for name in ("demand", "requirements", "submitted_at"):
            _check_credential(name, getattr(self, name))

        # status must be "active" or "paused".
        if self.status not in ("active", "paused"):
            raise ProjectCardError("status", f"must be 'active' or 'paused', got {self.status!r}")

        # task_types must be a non-empty list of non-empty strings.
        if isinstance(self.task_types, bool) or not isinstance(self.task_types, list):
            raise ProjectCardError("task_types", f"expected list, got {type(self.task_types).__name__}")
        if not self.task_types:
            raise ProjectCardError("task_types", "must be non-empty")
        for i, task_type in enumerate(self.task_types):
            if not isinstance(task_type, str):
                raise ProjectCardError(f"task_types[{i}]", f"expected str, got {type(task_type).__name__}")
            if not task_type:
                raise ProjectCardError(f"task_types[{i}]", "must be non-empty")

        # demand_snapshot must be a dict.
        if not isinstance(self.demand_snapshot, dict):
            raise ProjectCardError("demand_snapshot", f"expected dict, got {type(self.demand_snapshot).__name__}")

        # notes is optional but must be str if provided.
        if self.notes is not None and not isinstance(self.notes, str):
            raise ProjectCardError("notes", f"expected str or None, got {type(self.notes).__name__}")
        if self.notes is not None:
            _check_credential("notes", self.notes)

        # submitted_at must be an aware datetime.
        if self.submitted_at:
            try:
                parsed = datetime.fromisoformat(self.submitted_at.replace("Z", "+00:00"))
            except (ValueError, TypeError) as exc:
                raise ProjectCardError("submitted_at", f"not a valid ISO-8601 timestamp: {exc}") from None
            if parsed.tzinfo is None:
                raise ProjectCardError("submitted_at", "must be timezone-aware (include offset or Z suffix)")

    # ---------------------------------------------------------------------------
    # Canonical form
    # ---------------------------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the project card as a sorted, deterministic dict for signing."""
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "demand": self.demand,
            "task_types": list(self.task_types),
            "requirements": self.requirements,
            "demand_snapshot": self.demand_snapshot,
            "status": self.status,
            "submitted_at": self.submitted_at,
        }
        if self.notes is not None:
            result["notes"] = self.notes
        # Sort keys to ensure deterministic ordering
        return {k: result[k] for k in sorted(result.keys())}

    def digest(self) -> str:
        """SHA-256 hex digest of the canonical bytes (stable identity)."""
        return canonical_hash(self.to_canonical_dict())


# ---------------------------------------------------------------------------
# Sign / verify helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectCardVerification:
    """Outcome of :func:`verify_project_card_envelope`.

    Attributes:
        ok: True iff the envelope signature verified, the predicate type
            matched, and the embedded project card re-serialised to the attested
            subject digest.
        project_card: The embedded project card dict (populated even on failure
            when the envelope was parseable).
        keyid: keyid of the signature that successfully verified (empty on
            failure).
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    project_card: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: tuple[str, ...] = ()


def build_project_card_envelope(
    project_card: ProjectCard,
    signing_key: Ed25519PrivateKey,
) -> Envelope:
    """Sign a ``ProjectCard`` into a DSSE envelope.

    Args:
        project_card: The project card to sign.  Must be a valid ``ProjectCard``
            instance.
        signing_key: Ed25519 private key used to sign the PAE input.

    Returns:
        A signed :class:`Envelope` ready to be persisted or transmitted.

    Raises:
        ProjectCardError: If ``project_card`` is not a ``ProjectCard`` instance.
    """
    if not isinstance(project_card, ProjectCard):
        raise ProjectCardError(
            "<project_card>",
            f"expected ProjectCard, got {type(project_card).__name__}",
        )

    doc = project_card.to_canonical_dict()
    doc_bytes = canonical_bytes(doc)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    subject = Subject(
        name=f"project-card-{project_card.demand}",
        digest={"sha256": digest},
    )

    predicate: dict[str, Any] = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": "project-card",
        "document": doc,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=PROJECT_CARD_PREDICATE_TYPE,
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


def verify_project_card_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
) -> ProjectCardVerification:
    """Verify a project card envelope.

    Verifies the DSSE signature and predicate type, then validates the
    embedded project card document against the expected schema.  The predicate
    type must match :data:`PROJECT_CARD_PREDICATE_TYPE`; the
    ``document_kind`` inside the predicate body must be ``"project-card"``.

    Args:
        envelope: Parsed DSSE envelope (typically from
            :func:`bernstein.core.security.audit_dsse.parse_envelope`).
        public_key: Ed25519 public key the signer used.

    Returns:
        :class:`ProjectCardVerification` with ``ok`` flag and details.
    """
    # First verify signature and predicate type using verify_envelope's own logic
    # with the correct project-card predicate type
    errors: list[str] = []
    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=PROJECT_CARD_PREDICATE_TYPE,
    )
    if not env_v.ok:
        # Even when signature/predicate-type verification fails, we want to collect
        # document-level validation errors (wrong document_kind, credential fragments)
        # to help distinguish between "wrong signer" and "wrong document type".
        errors.extend(env_v.errors)
        # Extract the predicate body and validate document fields even when signature fails
        try:
            statement = json.loads(envelope.payload_bytes.decode("utf-8"))
            predicate = statement.get("predicate", {})
            if isinstance(predicate, dict):
                document_kind = predicate.get("document_kind", "")
                if document_kind != "project-card":
                    errors.append(
                        f"document_kind is {document_kind!r}, expected 'project-card'",
                    )
                document = predicate.get("document", {})
                if isinstance(document, dict):
                    credential_fragments = ("key", "token", "secret", "password", "credential")
                    for field_name, field_value in document.items():
                        if isinstance(field_value, str):
                            field_lower = field_value.lower()
                            if any(frag in field_lower for frag in credential_fragments):
                                errors.append(
                                    f"string field '{field_name}' contains credential-like fragment {field_value!r}",
                                )
        except Exception:
            pass
        return ProjectCardVerification(ok=False, errors=tuple(errors))

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return ProjectCardVerification(
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

    # document_kind must be "project-card".
    document_kind = predicate_dict.get("document_kind", "")
    if document_kind != "project-card":
        errors.append(
            f"document_kind is {document_kind!r}, expected 'project-card'",
        )

    # Check for credential name fragments in any string field value.
    # This is a second line of defense against credential smuggling.
    credential_fragments = ("key", "token", "secret", "password", "credential")
    for field_name, field_value in document.items():
        if isinstance(field_value, str):
            field_lower = field_value.lower()
            if any(fragment in field_lower for fragment in credential_fragments):
                errors.append(
                    f"string field '{field_name}' contains credential-like fragment {field_value!r}",
                )

    return ProjectCardVerification(
        ok=not errors,
        project_card=document,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
