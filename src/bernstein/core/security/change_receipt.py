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
hashed to produce the receipt digest. The receipt is signed and carried in the
shared receipt envelope (:mod:`bernstein.core.receipts.protocol`), which binds
the kind to the payload so an offline holder can check it with one command.

The receipt is verified through the one shared verifier,
:func:`bernstein.core.receipts.protocol.verify_receipt`: this module registers
the ``security.change`` kind and contributes only the payload check that is
specific to it -- required fields and their types, valid change outcomes, and
an allowed final status. Envelope shape, canonical bytes, payload digest and
signature are the protocol's, identical for every receipt kind, so a holder of
a change receipt does not need to know which module produced it.

The receipt is deliberately self-contained: it does not verify the playbook,
plan, or environment digests against external sources -- that is the caller's
responsibility when comparing against expected values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from bernstein.core.receipts.protocol import register_receipt_kind

if TYPE_CHECKING:
    from collections.abc import Mapping

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
        typed = cast("dict[str, Any]", value)
        return {key: _sort_recursive(typed[key]) for key in sorted(typed)}
    if isinstance(value, list):
        return [_sort_recursive(item) for item in cast("list[Any]", value)]
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
# Kind registration: the payload check the shared verifier calls
# ---------------------------------------------------------------------------

#: Kind string this receipt registers with the shared protocol.
RECEIPT_KIND = "security.change"

#: Fields every change receipt must carry as a string.
_REQUIRED_STRING_FIELDS: tuple[str, ...] = (
    "plan_id",
    "plan_digest",
    "playbook_digest",
    "environment_digest",
    "approver_identity",
    "timestamp",
)

#: Fields every change attempt must carry as a string.
_CHANGE_STRING_FIELDS: tuple[str, ...] = (
    "change_id",
    "change_type",
    "target",
    "attempted_at",
    "error_message",
)


def _required_string_errors(payload: Mapping[str, Any]) -> list[str]:
    """Return errors for missing or non-string top-level string fields."""
    errors: list[str] = []
    for name in _REQUIRED_STRING_FIELDS:
        value = payload.get(name)
        if value is None:
            errors.append(f"{name}: missing required field")
        elif not isinstance(value, str):
            errors.append(f"{name}: expected string, got {type(value).__name__}")
    return errors


def _change_entry_errors(index: int, change: Any) -> list[str]:
    """Return errors for one entry of the ``changes`` list."""
    if not isinstance(change, dict):
        return [f"changes[{index}]: expected object, got {type(change).__name__}"]

    errors: list[str] = []
    entry = cast("dict[str, Any]", change)
    for name in _CHANGE_STRING_FIELDS:
        value = entry.get(name, "")
        if not isinstance(value, str):
            errors.append(f"changes[{index}].{name}: expected string, got {type(value).__name__}")

    outcome = entry.get("outcome", "")
    if not isinstance(outcome, str):
        errors.append(f"changes[{index}].outcome: expected string, got {type(outcome).__name__}")
    elif outcome not in ("success", "failure", "skipped"):
        errors.append(f"changes[{index}].outcome: invalid outcome {outcome!r}")
    return errors


def change_receipt_payload_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the change receipt's semantic errors, empty when well-formed.

    Registered as the ``security.change`` payload check, so a change receipt is
    verified by :func:`bernstein.core.receipts.protocol.verify_receipt` like
    every other kind: schema version, required fields and their types, valid
    change outcomes, and a final status the schema allows.

    The receipt stays deliberately self-contained: the plan, playbook and
    environment digests are not checked against external sources, which is the
    caller's job when comparing against expected values.

    Args:
        payload: Parsed receipt payload (typically from a receipt document).

    Returns:
        Tuple of ``field: message`` errors, empty when the payload is valid.
    """
    errors: list[str] = []

    schema_version = payload.get("schema_version", "")
    if not isinstance(schema_version, str):
        errors.append(f"schema_version: expected string, got {type(schema_version).__name__}")
    elif schema_version != CHANGE_RECEIPT_SCHEMA_VERSION:
        errors.append(f"schema_version: expected {CHANGE_RECEIPT_SCHEMA_VERSION}, got {schema_version!r}")

    errors.extend(_required_string_errors(payload))

    final_status = payload.get("final_status")
    if final_status is None:
        errors.append("final_status: missing required field")
    elif not isinstance(final_status, str):
        errors.append(f"final_status: expected string, got {type(final_status).__name__}")
    elif final_status not in ("complete", "partial", "failed"):
        errors.append(f"final_status: invalid status {final_status!r}")

    raw_changes = payload.get("changes")
    if raw_changes is None:
        errors.append("changes: missing required field")
    elif not isinstance(raw_changes, list):
        errors.append(f"changes: expected list, got {type(raw_changes).__name__}")
    else:
        for index, change in enumerate(cast("list[Any]", raw_changes)):
            errors.extend(_change_entry_errors(index, change))

    return tuple(errors)


register_receipt_kind(RECEIPT_KIND, payload_check=change_receipt_payload_errors)
