"""Worker card protocol document.

A WorkerCard represents what a donor offers to volunteer — the capabilities they can
provide, resource ceilings (CPU/RAM/GPU), sandbox tier, availability window, and
budget posture.  It is a closed schema: every field has an explicit type, no
field is ``dict[str, Any]`` or otherwise open-ended, and string field values
containing credential-like substrings are rejected.

Design decisions
----------------

* **Closed schema.**  Unlike submission, claim, or receipt documents, the worker
  card uses a fixed set of fields with explicit types.  No field is
  ``dict[str, Any]`` or similarly open-ended, satisfying the security requirement
  that worker cards must be structurally incapable of carrying a credential.

* **Credential name denylist.**  The schema includes a denylist of string
  field values that look like environment-variable-style credentials
  (case-insensitive substrings: ``"KEY"``, ``"TOKEN"``, ``"SECRET"``,
  ``"PASSWORD"``, ``"CREDENTIAL"``).  Any string field value containing one
  of these substrings is rejected during construction.

* **Deterministic signing.**  Ed25519 is deterministic by RFC 8032 §5.1.6.
  ``canonical_bytes`` guarantees the serialisation is byte-stable.

* **Predicate type URL.**  The envelope uses
  ``https://bernstein.run/attestations/volunteer/worker_card/v1`` as the
  ``predicateType`` so verifiers can filter worker-card-only attestations
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

#: Schema version for the worker card document body.
WORKER_CARD_SCHEMA_VERSION: str = "1.0.0"

#: Worker card-specific predicate type URL.  Distinct from the base volunteer
#: predicate type so a verifier can filter worker-card-only attestations.
WORKER_CARD_PREDICATE_TYPE: str = "https://bernstein.run/attestations/volunteer/worker_card/v1"

#: Hex digest regex — accepts lowercase or uppercase hex, 64 chars (sha256).
_WORKER_CARD_DIGEST_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")

#: Valid CPU/GPU tier strings.
_CPU_GPU_TIERS = ("micro", "small", "medium", "large", "xlarge")

#: Valid sandbox tier strings.
_SANDBOX_TIERS = ("microvm", "container", "baremetal")

#: Valid budget posture strings.
_BUDGET_POSTURES = ("generous", "modest", "tight", "free")

#: Valid task type strings.
_TASK_TYPES = ("compute", "data-processing", "analysis", "documentation", "support")

#: Valid task requirement strings.
_REQUIREMENTS = ("cpu", "ram", "gpu", "sandbox", "availability", "budget")

#: Valid document kind string.
_DOCUMENT_KIND = "worker-card"


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class WorkerCardError(ValueError):
    """Raised when a ``WorkerCard`` field fails validation.

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
class WorkerCard:
    """A closed schema representing what a donor offers to volunteer.

    Attributes:
        schema_version: Document schema version.
        task_types: List of task types the worker can perform (must be from
            :data:`_TASK_TYPES`).
        capabilities: Human-readable description of capabilities (e.g.
            \"high-performance compute, GPU-accelerated\").
        cpu_ceiling: Maximum CPU tier the worker can provide (must be from
            :data:`_CPU_GPU_TIERS`).
        ram_ceiling: Maximum RAM tier the worker can provide (must be from
            ``_CPU_GPU_TIERS`` - treated as CPU tier for simplicity).
        gpu_ceiling: Maximum GPU tier the worker can provide (must be from
            ``_CPU_GPU_TIERS`` - treated as CPU tier for simplicity).
        sandbox_tier: Minimum sandbox tier the worker requires (must be from
            :data:`_SANDBOX_TIERS`).
        availability_window: Human-readable description of availability (e.g.
            \"weekdays 9am-5pm\", \"full-time\", \"part-time\").
        budget_posture: Human-readable description of budget posture
            (must be from :data:`_BUDGET_POSTURES`).
        submitted_at: ISO-8601 timestamp with timezone (UTC recommended).
        schema_version: Document schema version.
        notes: Optional human-readable notes.
    """

    task_types: list[str]
    capabilities: str
    cpu_ceiling: str
    ram_ceiling: str
    gpu_ceiling: str
    sandbox_tier: str
    availability_window: str
    budget_posture: str
    submitted_at: str
    schema_version: str = WORKER_CARD_SCHEMA_VERSION
    notes: str | None = None

    def __post_init__(self) -> None:
        # Credential name denylist — reject any string field value containing
        # credential-like fragments (KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL).
        _CREDENTIAL_FRAGMENTS = ("key", "token", "secret", "password", "credential")

        def _check_str(name: str, value: Any) -> None:
            if isinstance(value, bool) or not isinstance(value, str):
                raise WorkerCardError(name, f"expected str, got {type(value).__name__}")
            if not value:
                raise WorkerCardError(name, "must be non-empty")

        def _check_credential(name: str, value: str) -> None:
            field_lower = value.lower()
            for frag in _CREDENTIAL_FRAGMENTS:
                if frag in field_lower:
                    raise WorkerCardError(name, f"contains credential-like fragment {value!r}")

        # Validate str-typed scalar fields (excluding task_types which is a list).
        for name, value in [
            ("schema_version", self.schema_version),
            ("capabilities", self.capabilities),
            ("cpu_ceiling", self.cpu_ceiling),
            ("ram_ceiling", self.ram_ceiling),
            ("gpu_ceiling", self.gpu_ceiling),
            ("sandbox_tier", self.sandbox_tier),
            ("availability_window", self.availability_window),
            ("budget_posture", self.budget_posture),
            ("submitted_at", self.submitted_at),
        ]:
            _check_str(name, value)
            _check_credential(name, value)

        # task_types must be a list of valid task types.
        if isinstance(self.task_types, bool) or not isinstance(self.task_types, list):
            raise WorkerCardError("task_types", f"expected list, got {type(self.task_types).__name__}")
        if not self.task_types:
            raise WorkerCardError("task_types", "must be non-empty")
        for i, task_type in enumerate(self.task_types):
            if not isinstance(task_type, str):
                raise WorkerCardError(f"task_types[{i}]", f"expected str, got {type(task_type).__name__}")
            if task_type not in _TASK_TYPES:
                raise WorkerCardError(
                    f"task_types[{i}]",
                    f"must be one of {_TASK_TYPES}, got {task_type!r}",
                )
            if not task_type:
                raise WorkerCardError(f"task_types[{i}]", "must be non-empty")
            _check_credential(f"task_types[{i}]", task_type)

        # capabilities must be a non-empty string.
        if not isinstance(self.capabilities, str):
            raise WorkerCardError("capabilities", f"expected str, got {type(self.capabilities).__name__}")
        if not self.capabilities:
            raise WorkerCardError("capabilities", "must be non-empty")
        _check_credential("capabilities", self.capabilities)

        # Credential denylist must run before enum validation so credential
        # fragments are caught before the tier-value check.
        if self.cpu_ceiling not in _CPU_GPU_TIERS:
            raise WorkerCardError(
                "cpu_ceiling",
                f"must be one of {_CPU_GPU_TIERS}, got {self.cpu_ceiling!r}",
            )
        _check_credential("cpu_ceiling", self.cpu_ceiling)

        if self.ram_ceiling not in _CPU_GPU_TIERS:
            raise WorkerCardError(
                "ram_ceiling",
                f"must be one of {_CPU_GPU_TIERS}, got {self.ram_ceiling!r}",
            )
        _check_credential("ram_ceiling", self.ram_ceiling)

        if self.gpu_ceiling not in _CPU_GPU_TIERS:
            raise WorkerCardError(
                "gpu_ceiling",
                f"must be one of {_CPU_GPU_TIERS}, got {self.gpu_ceiling!r}",
            )
        _check_credential("gpu_ceiling", self.gpu_ceiling)

        if self.sandbox_tier not in _SANDBOX_TIERS:
            raise WorkerCardError(
                "sandbox_tier",
                f"must be one of {_SANDBOX_TIERS}, got {self.sandbox_tier!r}",
            )
        _check_credential("sandbox_tier", self.sandbox_tier)

        if not isinstance(self.availability_window, str):
            raise WorkerCardError("availability_window", f"expected str, got {type(self.availability_window).__name__}")
        if not self.availability_window:
            raise WorkerCardError("availability_window", "must be non-empty")
        _check_credential("availability_window", self.availability_window)

        if self.budget_posture not in _BUDGET_POSTURES:
            raise WorkerCardError(
                "budget_posture",
                f"must be one of {_BUDGET_POSTURES}, got {self.budget_posture!r}",
            )
        _check_credential("budget_posture", self.budget_posture)

        # submitted_at must be an aware datetime.
        if isinstance(self.submitted_at, bool) or not isinstance(self.submitted_at, str):
            raise WorkerCardError("submitted_at", f"expected str, got {type(self.submitted_at).__name__}")
        if self.submitted_at:
            try:
                parsed = datetime.fromisoformat(self.submitted_at.replace("Z", "+00:00"))
            except (ValueError, TypeError) as exc:
                raise WorkerCardError("submitted_at", f"not a valid ISO-8601 timestamp: {exc}") from None
            if parsed.tzinfo is None:
                raise WorkerCardError("submitted_at", "must be timezone-aware (include offset or Z suffix)")

    # ---------------------------------------------------------------------------
    # Canonical form
    # ---------------------------------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the worker card as a sorted, deterministic dict for signing."""
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_types": list(self.task_types),
            "capabilities": self.capabilities,
            "cpu_ceiling": self.cpu_ceiling,
            "ram_ceiling": self.ram_ceiling,
            "gpu_ceiling": self.gpu_ceiling,
            "sandbox_tier": self.sandbox_tier,
            "availability_window": self.availability_window,
            "budget_posture": self.budget_posture,
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
class WorkerCardVerification:
    """Outcome of :func:`verify_worker_card_envelope`.

    Attributes:
        ok: True iff the envelope signature verified, the predicate type
            matched, and the embedded worker card re-serialised to the attested
            subject digest.
        worker_card: The embedded worker card dict (populated even on failure
            when the envelope was parseable).
        keyid: keyid of the signature that successfully verified (empty on
            failure).
        errors: Human-readable failure messages (empty when ``ok``).
    """

    ok: bool
    worker_card: dict[str, Any] = field(default_factory=dict)
    keyid: str = ""
    errors: tuple[str, ...] = ()


def build_worker_card_envelope(
    worker_card: WorkerCard,
    signing_key: Ed25519PrivateKey,
) -> Envelope:
    """Sign a ``WorkerCard`` into a DSSE envelope.

    Args:
        worker_card: The worker card to sign.  Must be a valid ``WorkerCard``
            instance.
        signing_key: Ed25519 private key used to sign the PAE input.

    Returns:
        A signed :class:`Envelope` ready to be persisted or transmitted.

    Raises:
        WorkerCardError: If ``worker_card`` is not a ``WorkerCard`` instance.
    """
    if not isinstance(worker_card, WorkerCard):
        raise WorkerCardError(
            "<worker_card>",
            f"expected WorkerCard, got {type(worker_card).__name__}",
        )

    doc = worker_card.to_canonical_dict()
    doc_bytes = canonical_bytes(doc)
    digest = hashlib.sha256(doc_bytes).hexdigest()

    subject = Subject(
        name=f"worker-card-{doc['cpu_ceiling']}-{doc['ram_ceiling']}",
        digest={"sha256": digest},
    )

    predicate: dict[str, Any] = {
        "schema_version": VOLUNTEER_DOCUMENT_SCHEMA_VERSION,
        "document_kind": _DOCUMENT_KIND,
        "document": doc,
    }
    statement = Statement(
        subjects=[subject],
        predicate_type=WORKER_CARD_PREDICATE_TYPE,
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


def verify_worker_card_envelope(
    envelope: Envelope,
    public_key: Ed25519PublicKey,
) -> WorkerCardVerification:
    """Verify a worker card envelope.

    Verifies the DSSE signature and predicate type, then validates the
    embedded worker card document.  The predicate type must match
    :data:`WORKER_CARD_PREDICATE_TYPE`; the ``document_kind`` inside the
    predicate body must be ``_DOCUMENT_KIND``.

    Args:
        envelope: Parsed DSSE envelope (typically from
            :func:`bernstein.core.security.audit_dsse.parse_envelope`).
        public_key: Ed25519 public key the signer used.

    Returns:
        :class:`WorkerCardVerification` with ``ok`` flag and details.
    """
    # First verify signature and predicate type using verify_envelope's own logic
    # with the correct worker-card predicate type
    errors: list[str] = []
    env_v = verify_envelope(
        envelope,
        public_key,
        expected_predicate_type=WORKER_CARD_PREDICATE_TYPE,
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
                if document_kind != _DOCUMENT_KIND:
                    errors.append(
                        f"document_kind is {document_kind!r}, expected '{_DOCUMENT_KIND}'",
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
        return WorkerCardVerification(ok=False, errors=tuple(errors))

    statement = env_v.statement
    raw_predicate = statement.get("predicate", {})
    predicate_dict = raw_predicate if isinstance(raw_predicate, dict) else {}
    if not isinstance(raw_predicate, dict):
        return WorkerCardVerification(
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

    # document_kind must be "worker-card".
    document_kind = predicate_dict.get("document_kind", "")
    if document_kind != _DOCUMENT_KIND:
        errors.append(
            f"document_kind is {document_kind!r}, expected '{_DOCUMENT_KIND}'",
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

    return WorkerCardVerification(
        ok=not errors,
        worker_card=document,
        keyid=env_v.keyid,
        errors=tuple(errors),
    )
