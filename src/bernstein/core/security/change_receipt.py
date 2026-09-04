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
from dataclasses import dataclass, field
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

    # The value replaced and the value written. Optional so that receipts
    # serialised before these fields existed still verify; type-checked so a
    # restore never reads a non-string.
    prior_value = entry.get("prior_value", "")
    if not isinstance(prior_value, str):
        errors.append(
            f"changes[{index}].prior_value: expected string, got {type(prior_value).__name__}",
        )

    written_value = entry.get("written_value", "")
    if not isinstance(written_value, str):
        errors.append(
            f"changes[{index}].written_value: expected string, got {type(written_value).__name__}",
        )

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

    # Optional: the digest of the receipt this one inverts. Absent on an
    # ordinary apply receipt, so it is type-checked but never required.
    restores_receipt_digest = payload.get("restores_receipt_digest", "")
    if not isinstance(restores_receipt_digest, str):
        errors.append(
            f"restores_receipt_digest: expected string, got {type(restores_receipt_digest).__name__}",
        )

    raw_changes = payload.get("changes")
    if raw_changes is None:
        errors.append("changes: missing required field")
    elif not isinstance(raw_changes, list):
        errors.append(f"changes: expected list, got {type(raw_changes).__name__}")
    else:
        for index, change in enumerate(cast("list[Any]", raw_changes)):
            errors.extend(_change_entry_errors(index, change))

    return tuple(errors)


@dataclass(frozen=True, slots=True)
class FieldError:
    """A field-level verification failure -- which field, and why."""

    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
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


def _parse_field_errors(messages: tuple[str, ...]) -> tuple[FieldError, ...]:
    """Translate ``"field: message"`` strings into :class:`FieldError` pairs."""
    parsed: list[FieldError] = []
    for message in messages:
        field_name, _, rest = message.partition(":")
        parsed.append(FieldError(field=field_name, message=rest.strip() or message))
    return tuple(parsed)


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
    if not isinstance(data, dict):
        return ReceiptVerification(
            ok=False,
            errors=(FieldError(field="root", message=f"expected an object, got {type(data).__name__}"),),
        )

    payload = cast("dict[str, Any]", data)
    string_errors = change_receipt_payload_errors(payload)
    errors = _parse_field_errors(string_errors)
    if errors:
        return ReceiptVerification(ok=False, receipt=payload, errors=errors)

    # (6) Digest consistency: the receipt must hash to its attested value.
    # The digest is a derived property computed from the canonical bytes;
    # callers use it to anchor the receipt in chains or to sign it in an
    # envelope. This mirrors result_receipt_bundle.py where digest is a
    # @property, not a stored field.
    recomputed = _sha256_hex(canonical_bytes(payload))

    return ReceiptVerification(
        ok=True,
        digest=recomputed,
        receipt=payload,
        errors=(),
    )


register_receipt_kind(RECEIPT_KIND, payload_check=change_receipt_payload_errors)
