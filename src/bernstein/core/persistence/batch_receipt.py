"""Signed pass receipts over a batch ledger.

A batch pass walks a flat list of items and the
:class:`~bernstein.core.persistence.batch_ledger.BatchLedger` chains the
successes so a resumed pass skips them. That chain answers "which items are
done"; it says nothing checkable about the rest of the pass - which items
failed and on what error, how many attempts each took, what the worker
process exited with, what its output ended on. Operators reconstructed that
from log tails by hand, and a pass that exited 0 with half its items failed
read as a clean run.

This module is the receipt for one pass. Its payload carries every item's
outcome (``success`` / ``failed`` / ``skipped``), attempts, exit code, the
sha256 of the output tail and, for a failure, the reason lifted from the last
non-empty line of that tail; ``error_breakdown`` counts the reasons across the
failures so a pass with forty items and one upstream outage reads as one
line. The receipt is a kind of the one receipt protocol
(:mod:`bernstein.core.receipts.protocol`): signed with Ed25519, verified
offline from its own bytes by :func:`~bernstein.core.receipts.protocol.verify_receipt`
and by ``bernstein verify <receipt.json>``.

Substrate coupling: the payload names the ledger's chain head before and
after the pass. :func:`verify_batch_pass_against_ledger` re-verifies the
ledger's hash chain and requires the segment between those heads to be exactly
the receipt's ``success`` list, in order. A receipt cannot claim a success the
ledger never chained, omit one it did, or be re-pointed at another pass
without either the ledger chain or the signature failing. Strip the ledger and
the receipt is still a signed, internally consistent statement; strip the
signature and it is a log line.

Determinism: no wall-clock value enters the payload - the ledger entries
carry their instants. Two builds over the same outcomes and the same heads
produce byte-identical canonical bytes.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from bernstein.core.persistence.batch_ledger import GENESIS_HASH, BatchLedgerError
from bernstein.core.receipts.protocol import register_receipt_kind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.persistence.batch_ledger import BatchLedger

__all__ = [
    "BATCH_PASS_SCHEMA_VERSION",
    "FAILURE_REASON_LIMIT",
    "RECEIPT_KIND",
    "BatchItemOutcome",
    "ItemOutcome",
    "batch_pass_payload_errors",
    "build_batch_pass_payload",
    "failure_reason_from_tail",
    "output_tail_digest",
    "verify_batch_pass_against_ledger",
]

#: Kind string this receipt registers with the shared protocol.
RECEIPT_KIND = "batch.pass"

#: Stamped into every payload. Bump on a wire-format change only, so a
#: verifier can reject shapes it does not know.
BATCH_PASS_SCHEMA_VERSION = "batch-pass/v1"

#: Longest failure reason carried per item. A reason is one line of an error
#: tail; anything longer is a stack dump, and the digest already covers it.
FAILURE_REASON_LIMIT = 200

type ItemOutcome = Literal["success", "failed", "skipped"]

_OUTCOMES: tuple[ItemOutcome, ...] = ("success", "failed", "skipped")


def output_tail_digest(tail: str | bytes) -> str:
    """Return the sha256 hex digest of an output tail.

    Args:
        tail: The last bytes of the worker's output, as text or bytes.

    Returns:
        64 hex characters.
    """
    data = tail.encode("utf-8") if isinstance(tail, str) else tail
    return hashlib.sha256(data).hexdigest()


def failure_reason_from_tail(tail: str, *, limit: int = FAILURE_REASON_LIMIT) -> str:
    """Return the last non-empty line of an output tail, bounded to *limit*.

    The last line of an error tail is the exception or the tool's own verdict
    (``ConnectionError: upstream 503``, ``exit 137``); the lines above it are
    the trace. One line groups across items; a trace does not.

    Args:
        tail: The output tail.
        limit: Maximum characters kept.

    Returns:
        The reason, or ``""`` when the tail has no non-empty line.
    """
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


@dataclass(frozen=True, slots=True)
class BatchItemOutcome:
    """What one pass did with one item.

    Attributes:
        entity_id: The item's identity, as the batch names it.
        outcome: ``success``, ``failed`` or ``skipped``.
        attempts: Attempts made; ``0`` for a skipped item.
        exit_code: The last attempt's process exit code, ``None`` when there
            was no process (a skipped item, or an in-process worker).
        output_tail_sha256: Digest of the output tail, ``""`` when none.
        failure_reason: The reason for a failure; ``""`` for any other outcome.
    """

    entity_id: str
    outcome: ItemOutcome
    attempts: int = 1
    exit_code: int | None = None
    output_tail_sha256: str = ""
    failure_reason: str = ""

    @classmethod
    def from_output(
        cls,
        entity_id: str,
        outcome: str,
        *,
        attempts: int | None = None,
        exit_code: int | None = None,
        output_tail: str | bytes = "",
    ) -> BatchItemOutcome:
        """Build an outcome from what the worker left behind.

        The digest and, for a failure, the reason are derived here so a caller
        never states a reason the tail does not end on.

        Args:
            entity_id: The item's identity.
            outcome: ``success``, ``failed`` or ``skipped``.
            attempts: Attempts made; defaults to ``0`` for skipped, else ``1``.
            exit_code: The last attempt's exit code, when there was a process.
            output_tail: The worker's output tail, when there was one.

        Raises:
            ValueError: *outcome* is not one of the three outcomes.
        """
        kind = _as_outcome(outcome)
        tail_text = output_tail.decode("utf-8", errors="replace") if isinstance(output_tail, bytes) else output_tail
        return cls(
            entity_id=entity_id,
            outcome=kind,
            attempts=(0 if kind == "skipped" else 1) if attempts is None else attempts,
            exit_code=exit_code,
            output_tail_sha256=output_tail_digest(output_tail) if output_tail else "",
            failure_reason=failure_reason_from_tail(tail_text) if kind == "failed" else "",
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the item's payload form."""
        return {
            "entity_id": self.entity_id,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "exit_code": self.exit_code,
            "output_tail_sha256": self.output_tail_sha256,
            "failure_reason": self.failure_reason,
        }


def _as_outcome(value: str) -> ItemOutcome:
    """Narrow a string to an item outcome, or raise."""
    if value == "success":
        return "success"
    if value == "failed":
        return "failed"
    if value == "skipped":
        return "skipped"
    msg = f"outcome must be one of {_OUTCOMES}, got {value!r}"
    raise ValueError(msg)


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def build_batch_pass_payload(
    *,
    batch_id: str,
    pass_id: str,
    items: Iterable[BatchItemOutcome],
    ledger_head_before: str,
    ledger_head_after: str,
) -> dict[str, Any]:
    """Return the receipt payload for one pass, ready for ``sign_receipt``.

    Args:
        batch_id: The batch this pass belongs to.
        pass_id: The pass, as the caller names it (a date, a run id).
        items: Every item the pass considered, in processing order.
        ledger_head_before: The ledger's head hash when the pass started.
        ledger_head_after: The ledger's head hash when the pass ended.

    Returns:
        The payload. Sign it with
        :func:`~bernstein.core.receipts.protocol.sign_receipt` under
        :data:`RECEIPT_KIND`.

    Raises:
        ValueError: An entity id appears twice, or a head is not a sha256 hex.
    """
    for name, head in (("ledger_head_before", ledger_head_before), ("ledger_head_after", ledger_head_after)):
        if not _is_sha256_hex(head):
            msg = f"{name} must be 64 hex characters, got {head!r}"
            raise ValueError(msg)

    outcomes = list(items)
    seen: set[str] = set()
    for item in outcomes:
        if item.entity_id in seen:
            msg = f"entity id {item.entity_id!r} appears more than once in the pass"
            raise ValueError(msg)
        seen.add(item.entity_id)

    item_dicts: list[dict[str, Any]] = [item.to_dict() for item in outcomes]
    payload: dict[str, Any] = {
        "schema_version": BATCH_PASS_SCHEMA_VERSION,
        "batch_id": batch_id,
        "pass_id": pass_id,
        "ledger_head_before": ledger_head_before,
        "ledger_head_after": ledger_head_after,
        "items": item_dicts,
    }
    payload.update(_derived_fields(item_dicts))
    return payload


def _derived_fields(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The buckets and the breakdown, recomputed from the item list alone."""
    buckets: dict[str, list[str]] = {outcome: [] for outcome in _OUTCOMES}
    reasons: Counter[str] = Counter()
    for item in items:
        buckets[str(item["outcome"])].append(str(item["entity_id"]))
        if item["outcome"] == "failed":
            reasons[str(item["failure_reason"])] += 1
    return {
        "success": buckets["success"],
        "failed": buckets["failed"],
        "skipped": buckets["skipped"],
        "error_breakdown": dict(sorted(reasons.items())),
    }


# ---------------------------------------------------------------------------
# Kind registration: the payload check the shared verifier calls
# ---------------------------------------------------------------------------


def _item_errors(index: int, raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return [f"items[{index}]: expected object, got {type(raw).__name__}"]
    item = cast("dict[str, Any]", raw)
    label = f"items[{index}] ({item.get('entity_id', '?')})"
    errors: list[str] = []

    entity_id = item.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        errors.append(f"{label}.entity_id: expected non-empty string")

    outcome = item.get("outcome")
    if outcome not in _OUTCOMES:
        errors.append(f"{label}.outcome: expected one of {_OUTCOMES}, got {outcome!r}")
        return errors

    attempts = item.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        errors.append(f"{label}.attempts: expected non-negative integer, got {attempts!r}")
    elif outcome == "skipped" and attempts != 0:
        errors.append(f"{label}.attempts: a skipped item has 0 attempts, got {attempts}")
    elif outcome != "skipped" and attempts == 0:
        errors.append(f"{label}.attempts: a {outcome} item has at least 1 attempt")

    exit_code = item.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        errors.append(f"{label}.exit_code: expected integer or null, got {exit_code!r}")

    digest = item.get("output_tail_sha256", "")
    if digest != "" and not _is_sha256_hex(digest):
        errors.append(f"{label}.output_tail_sha256: expected 64 hex characters or empty, got {digest!r}")

    reason = item.get("failure_reason", "")
    if not isinstance(reason, str):
        errors.append(f"{label}.failure_reason: expected string, got {type(reason).__name__}")
    elif outcome == "failed" and not reason:
        errors.append(f"{label}.failure_reason: a failed item carries its reason")
    elif outcome != "failed" and reason:
        errors.append(f"{label}.failure_reason: only a failed item carries a reason, got {reason!r}")
    elif len(reason) > FAILURE_REASON_LIMIT:
        errors.append(f"{label}.failure_reason: longer than {FAILURE_REASON_LIMIT} characters")

    return errors


def batch_pass_payload_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the pass receipt's semantic errors, empty when well-formed.

    Registered as the ``batch.pass`` payload check, so a pass receipt is
    verified by :func:`bernstein.core.receipts.protocol.verify_receipt` like
    every other kind: schema version, the two ledger heads, every item's
    fields, and - the part that makes the buckets trustworthy - that
    ``success`` / ``failed`` / ``skipped`` and ``error_breakdown`` are exactly
    what the item list produces. An item moved between buckets, or a count
    edited, is named.

    Args:
        payload: Parsed receipt payload.

    Returns:
        Tuple of ``field: message`` errors, empty when the payload is valid.
    """
    errors: list[str] = []

    schema_version = payload.get("schema_version")
    if schema_version != BATCH_PASS_SCHEMA_VERSION:
        errors.append(f"schema_version: expected {BATCH_PASS_SCHEMA_VERSION}, got {schema_version!r}")

    for name in ("batch_id", "pass_id"):
        if not isinstance(payload.get(name), str):
            errors.append(f"{name}: expected string, got {type(payload.get(name)).__name__}")

    for name in ("ledger_head_before", "ledger_head_after"):
        if not _is_sha256_hex(payload.get(name)):
            errors.append(f"{name}: expected 64 hex characters, got {payload.get(name)!r}")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        errors.append(f"items: expected list, got {type(raw_items).__name__}")
        return tuple(errors)
    items = cast("list[Any]", raw_items)

    for index, raw in enumerate(items):
        errors.extend(_item_errors(index, raw))
    if errors:
        return tuple(errors)

    typed = cast("list[dict[str, Any]]", items)
    ids = [str(item["entity_id"]) for item in typed]
    if len(set(ids)) != len(ids):
        errors.append("items: an entity id appears more than once")

    expected = _derived_fields(typed)
    for bucket in _OUTCOMES:
        claimed = payload.get(bucket)
        if claimed != expected[bucket]:
            errors.append(
                f"{bucket}: does not match the items (claimed {claimed!r}, items give {expected[bucket]!r})",
            )
    if payload.get("error_breakdown") != expected["error_breakdown"]:
        errors.append(
            "error_breakdown: does not match the failed items"
            f" (claimed {payload.get('error_breakdown')!r}, items give {expected['error_breakdown']!r})",
        )
    return tuple(errors)


register_receipt_kind(RECEIPT_KIND, payload_check=batch_pass_payload_errors)


# ---------------------------------------------------------------------------
# Ledger anchoring
# ---------------------------------------------------------------------------


def verify_batch_pass_against_ledger(payload: Mapping[str, Any], ledger: BatchLedger) -> tuple[str, ...]:
    """Check that the receipt's successes are the ledger segment the pass appended.

    Re-verifies the ledger's hash chain, then walks it from
    ``ledger_head_before`` to ``ledger_head_after`` and compares the entity
    ids on that segment with the receipt's ``success`` list, in order. Run
    after :func:`~bernstein.core.receipts.protocol.verify_receipt`; a payload
    that failed there is not worth anchoring.

    Args:
        payload: A verified pass receipt payload.
        ledger: The ledger the pass appended to.

    Returns:
        Errors, empty when the segment and the receipt agree.
    """
    try:
        ledger.verify()
    except BatchLedgerError as exc:
        return (f"ledger chain: {exc}",)

    before = str(payload.get("ledger_head_before", ""))
    after = str(payload.get("ledger_head_after", ""))
    claimed = [str(entity_id) for entity_id in cast("list[Any]", payload.get("success") or [])]

    entries = ledger.entries()
    if before == GENESIS_HASH:
        start = 0
    else:
        positions = [index for index, entry in enumerate(entries) if entry.entry_hash == before]
        if not positions:
            return (f"ledger_head_before: no ledger entry has hash {before[:16]}...",)
        start = positions[0] + 1

    segment: list[str] = []
    reached = before == after
    for entry in entries[start:]:
        if reached:
            break
        segment.append(entry.entity_id)
        if entry.entry_hash == after:
            reached = True
    if not reached:
        return (f"ledger_head_after: no ledger entry after ledger_head_before has hash {after[:16]}...",)

    if segment == claimed:
        return ()
    errors: list[str] = []
    for entity_id in sorted(set(claimed) - set(segment)):
        errors.append(f"success: {entity_id!r} is claimed but the ledger segment does not record it")
    for entity_id in sorted(set(segment) - set(claimed)):
        errors.append(f"success: {entity_id!r} is on the ledger segment but the receipt does not claim it")
    if not errors:
        errors.append(f"success: order differs from the ledger segment (receipt {claimed!r}, ledger {segment!r})")
    return tuple(errors)
