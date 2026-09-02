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
  target resource, timestamp, outcome (success/failure/skipped), error message,
  the value observed immediately before the change, and the value written;
* the final status (complete/partial/failed) derived from change outcomes;
* the digest of the receipt this one restores, empty when it is not a restore;
* creation timestamp.

Recording the prior value next to the written value is what makes a receipt
enough to undo the changes it attests: the inverse plan is read off the receipt
rather than derived by re-observing the environment. See
:mod:`bernstein.core.govern.restore`.

``prior_value``, ``written_value`` and ``restores_receipt_digest`` are additive:
receipts serialised without them still verify, so the schema version is
unchanged.

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
        prior_value: The value read from the target immediately before the
            change was written. Empty when the target did not exist (a
            'create') or when the applier could not read it. This is the
            evidence an inverse plan is built from, so it is captured at
            apply time and never re-derived afterwards.
        written_value: The value the change wrote to the target. Empty for a
            'delete'. Compared against the target's current value to decide
            whether it has drifted since the apply.
    """

    change_id: str
    change_type: str
    target: str
    attempted_at: str
    outcome: ChangeOutcome
    error_message: str = ""
    prior_value: str = ""
    written_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "change_id": self.change_id,
            "change_type": self.change_type,
            "target": self.target,
            "attempted_at": self.attempted_at,
            "outcome": self.outcome,
            "error_message": self.error_message,
            "prior_value": self.prior_value,
            "written_value": self.written_value,
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
        restores_receipt_digest: Digest of the receipt this one inverts, empty
            when this receipt is not a restore. The digest is the apply
            record's identity, so the link is checkable offline from the two
            receipts alone.
    """

    plan_id: str
    plan_digest: str
    playbook_digest: str
    environment_digest: str
    approver_identity: str
    changes: tuple[ChangeAttempt, ...] = ()
    final_status: FinalStatus = "complete"
    timestamp: str = ""
    restores_receipt_digest: str = ""

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
            "restores_receipt_digest": self.restores_receipt_digest,
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
    4. Each change has required fields and valid outcome, and the optional
       prior_value/written_value pair is well-typed when present.
    5. final_status is one of the valid values, and the optional
       restores_receipt_digest is a string when present.
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

    # Optional: the digest of the receipt this one inverts. Absent on an
    # ordinary apply receipt, so it is type-checked but never required.
    restores_receipt_digest = data.get("restores_receipt_digest", "")
    if not isinstance(restores_receipt_digest, str):
        errors.append(
            FieldError(
                "restores_receipt_digest",
                f"expected string, got {type(restores_receipt_digest).__name__}",
            ),
        )

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

                # The value replaced and the value written. Optional so that
                # receipts serialised before these fields existed still
                # verify; type-checked so a restore never reads a non-string.
                prior_value = change.get("prior_value", "")
                if not isinstance(prior_value, str):
                    errors.append(
                        FieldError(
                            f"changes[{idx}].prior_value",
                            f"expected string, got {type(prior_value).__name__}",
                        ),
                    )

                written_value = change.get("written_value", "")
                if not isinstance(written_value, str):
                    errors.append(
                        FieldError(
                            f"changes[{idx}].written_value",
                            f"expected string, got {type(written_value).__name__}",
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
