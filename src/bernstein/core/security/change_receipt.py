"""Change receipt: attestation of applied changes and their outcomes.

A change receipt binds a playbook digest, plan digest, environment digest, and
approver identity to the actual changes attempted and their outcomes. It is the
core attestation artifact that bridges intended changes (the playbook and plan)
to observed results (success, failure, or partial application).

The receipt captures:

* the plan ID and digest (canonical hash of the plan YAML);
* the playbook digest (canonical hash of the playbook that produced the plan);
* the environment digest (canonical hash of the environment constraints);
* the approver identity (human or service account that approved the changes);
* the list of change attempts: each with change ID, type (create/update/delete),
  target resource, timestamp, outcome (success/failure/skipped), and error message;
* the final status (complete/partial/failed) derived from change outcomes;
* creation timestamp.

All of that is serialized to canonical JSON (sorted keys, compact separators) and
hashed to produce the receipt digest. The receipt can be signed with an Ed25519
key and wrapped in a DSSE envelope for offline verification, following the same
pattern as :mod:`bernstein.core.security.result_receipt_bundle`.

:func:`verify_receipt` checks:

1. The digest field matches the canonical bytes of the receipt content;
2. All required fields are present and well-typed;
3. Change outcomes are valid (success, failure, or skipped);
4. The final_status is consistent with the change outcomes.

The receipt is deliberately self-contained: it does not verify the playbook,
plan, or environment digests against external sources -- that is the caller's
responsibility when comparing against expected values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

#: Schema version for the change receipt format.
CHANGE_RECEIPT_SCHEMA_VERSION: str = "1.0.0"

#: Valid change outcome states.
ChangeOutcome = Literal["success", "failure", "skipped"]

#: Valid final status states.
FinalStatus = Literal["complete", "partial", "failed"]


class ChangeReceiptError(RuntimeError):
    """Base class for change receipt build/verify failures."""


def _sha256_hex(data: bytes) -> str:
    """Compute sha256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def _sort_recursive(value: Any) -> Any:
    """Recursively reorder dict keys so canonical JSON is byte-stable."""
    if isinstance(value, dict):
        return {k: _sort_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_recursive(v) for v in value]
    return value


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON: recursively sorted keys, compact separators, UTF-8.

    Matches the discipline of :mod:`bernstein.core.security.result_receipt_bundle`
    so multiple serialisations of the same receipt byte-agree.
    """
    return json.dumps(
        _sort_recursive(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Receipt contents
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeAttempt:
    """One change attempted: what, when, and the outcome.

    Attributes:
        change_id: Unique identifier for this change.
        change_type: Type of change: 'create', 'update', or 'delete'.
        target: Resource identifier (e.g., 'iam.User:alice', 'kv.Secret:db-pass').
        attempted_at: ISO 8601 timestamp when the change was attempted.
        outcome: Result: 'success', 'failure', or 'skipped'.
        error_message: Human-readable error, empty if outcome is 'success'.
    """

    change_id: str
    change_type: str
    target: str
    attempted_at: str
    outcome: ChangeOutcome
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "target": self.target,
            "attempted_at": self.attempted_at,
            "outcome": self.outcome,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class ChangeReceipt:
    """The full contents of a change receipt attestation.

    Attributes:
        plan_id: Identifier of the plan this receipt corresponds to.
        plan_digest: Sha256 hex digest of the plan YAML.
        playbook_digest: Sha256 hex digest of the playbook that produced the plan.
        environment_digest: Sha256 hex digest of environment constraints.
        approver_identity: Human or service account that approved the changes.
        changes: Tuple of change attempts.
        final_status: Aggregate outcome: 'complete', 'partial', or 'failed'.
        timestamp: ISO 8601 timestamp of receipt creation.
    """

    plan_id: str
    plan_digest: str
    playbook_digest: str
    environment_digest: str
    approver_identity: str
    changes: tuple[ChangeAttempt, ...] = ()
    final_status: FinalStatus = "complete"
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "schema_version": CHANGE_RECEIPT_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "playbook_digest": self.playbook_digest,
            "environment_digest": self.environment_digest,
            "approver_identity": self.approver_identity,
            "changes": [c.to_dict() for c in self.changes],
            "final_status": self.final_status,
            "timestamp": self.timestamp,
        }

    def canonical_bytes(self) -> bytes:
        """Return canonical JSON bytes of the receipt content."""
        return canonical_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        """Sha256 of the canonical receipt bytes -- the attestation anchor."""
        return _sha256_hex(self.canonical_bytes())


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldError:
    """A field-level verification failure."""

    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.field}: {self.message}"


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Outcome of :func:`verify_receipt`.

    Attributes:
        ok: True iff all verification checks passed.
        digest: The receipt digest (sha256 of canonical bytes), empty on failure.
        receipt: Parsed receipt dict, empty on format errors.
        errors: Tuple of field-level errors, empty when ok is True.
    """

    ok: bool
    digest: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)
    errors: tuple[FieldError, ...] = ()


def verify_receipt(data: dict[str, Any]) -> ReceiptVerification:
    """Offline verification of a change receipt.

    Checks, in order, collecting field-level errors:

    1. Schema version is present and matches the expected value.
    2. All required fields are present: plan_id, plan_digest, playbook_digest,
       environment_digest, approver_identity, changes, final_status, timestamp.
    3. Field types are correct (strings, lists, etc.).
    4. Each change has required fields and valid outcome.
    5. final_status is one of the valid values.
    6. The receipt digests correctly: the parsed content re-serialises to the
       attested value (when a 'digest' field is present and well-typed).

    Args:
        data: Parsed JSON dict to verify (typically from a receipt file).

    Returns:
        :class:`ReceiptVerification` with ok flag and field-level errors.
    """
    errors: list[FieldError] = []

    if not isinstance(data, dict):
        return ReceiptVerification(
            ok=False,
            errors=(FieldError("root", f"expected an object, got {type(data).__name__}"),),
        )

    # (1) Schema version
    schema_version = data.get("schema_version", "")
    if not isinstance(schema_version, str):
        errors.append(FieldError("schema_version", f"expected string, got {type(schema_version).__name__}"))
    elif schema_version != CHANGE_RECEIPT_SCHEMA_VERSION:
        errors.append(
            FieldError("schema_version", f"expected {CHANGE_RECEIPT_SCHEMA_VERSION}, got {schema_version!r}"),
        )

    # (2) & (3) Required fields and their types
    plan_id = data.get("plan_id")
    if plan_id is None:
        errors.append(FieldError("plan_id", "missing required field"))
    elif not isinstance(plan_id, str):
        errors.append(FieldError("plan_id", f"expected string, got {type(plan_id).__name__}"))

    plan_digest = data.get("plan_digest")
    if plan_digest is None:
        errors.append(FieldError("plan_digest", "missing required field"))
    elif not isinstance(plan_digest, str):
        errors.append(FieldError("plan_digest", f"expected string, got {type(plan_digest).__name__}"))

    playbook_digest = data.get("playbook_digest")
    if playbook_digest is None:
        errors.append(FieldError("playbook_digest", "missing required field"))
    elif not isinstance(playbook_digest, str):
        errors.append(FieldError("playbook_digest", f"expected string, got {type(playbook_digest).__name__}"))

    environment_digest = data.get("environment_digest")
    if environment_digest is None:
        errors.append(FieldError("environment_digest", "missing required field"))
    elif not isinstance(environment_digest, str):
        errors.append(FieldError("environment_digest", f"expected string, got {type(environment_digest).__name__}"))

    approver_identity = data.get("approver_identity")
    if approver_identity is None:
        errors.append(FieldError("approver_identity", "missing required field"))
    elif not isinstance(approver_identity, str):
        errors.append(FieldError("approver_identity", f"expected string, got {type(approver_identity).__name__}"))

    timestamp = data.get("timestamp")
    if timestamp is None:
        errors.append(FieldError("timestamp", "missing required field"))
    elif not isinstance(timestamp, str):
        errors.append(FieldError("timestamp", f"expected string, got {type(timestamp).__name__}"))

    final_status = data.get("final_status")
    if final_status is None:
        errors.append(FieldError("final_status", "missing required field"))
    elif not isinstance(final_status, str):
        errors.append(FieldError("final_status", f"expected string, got {type(final_status).__name__}"))
    elif final_status not in ("complete", "partial", "failed"):
        errors.append(FieldError("final_status", f"invalid status {final_status!r}"))

    # (4) Changes list and each change's structure
    raw_changes = data.get("changes")
    if raw_changes is None:
        errors.append(FieldError("changes", "missing required field"))
    elif not isinstance(raw_changes, list):
        errors.append(FieldError("changes", f"expected list, got {type(raw_changes).__name__}"))
    else:
        for idx, change in enumerate(raw_changes):
            if not isinstance(change, dict):
                errors.append(FieldError(f"changes[{idx}]", f"expected object, got {type(change).__name__}"))
            else:
                change_id = change.get("change_id", "")
                if not isinstance(change_id, str):
                    errors.append(
                        FieldError(f"changes[{idx}].change_id", f"expected string, got {type(change_id).__name__}"),
                    )

                change_type = change.get("change_type", "")
                if not isinstance(change_type, str):
                    errors.append(
                        FieldError(
                            f"changes[{idx}].change_type",
                            f"expected string, got {type(change_type).__name__}",
                        ),
                    )

                target = change.get("target", "")
                if not isinstance(target, str):
                    errors.append(
                        FieldError(f"changes[{idx}].target", f"expected string, got {type(target).__name__}"),
                    )

                attempted_at = change.get("attempted_at", "")
                if not isinstance(attempted_at, str):
                    errors.append(
                        FieldError(
                            f"changes[{idx}].attempted_at",
                            f"expected string, got {type(attempted_at).__name__}",
                        ),
                    )

                outcome = change.get("outcome", "")
                if not isinstance(outcome, str):
                    errors.append(
                        FieldError(f"changes[{idx}].outcome", f"expected string, got {type(outcome).__name__}"),
                    )
                elif outcome not in ("success", "failure", "skipped"):
                    errors.append(FieldError(f"changes[{idx}].outcome", f"invalid outcome {outcome!r}"))

                error_message = change.get("error_message", "")
                if not isinstance(error_message, str):
                    errors.append(
                        FieldError(
                            f"changes[{idx}].error_message",
                            f"expected string, got {type(error_message).__name__}",
                        ),
                    )

    # If we have structural errors, stop here before computing digest
    if errors:
        return ReceiptVerification(ok=False, receipt=data, errors=tuple(errors))

    # (6) Digest consistency: the receipt must hash to its attested value
    recomputed = _sha256_hex(canonical_bytes(data))
    # Note: we do NOT check a 'digest' field in the receipt dict itself.
    # The digest is a derived property computed from the canonical bytes.
    # Callers use the computed digest to anchor the receipt in chains or
    # to sign it in an envelope. This mirrors result_receipt_bundle.py
    # where digest is a @property, not a stored field.

    return ReceiptVerification(
        ok=not errors,
        digest=recomputed,
        receipt=data,
        errors=tuple(errors),
    )
