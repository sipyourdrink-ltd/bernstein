"""HMAC-chained compaction receipts and step-journal registration (#2246).

A compaction mutates a worker's remaining run the way a merge mutates a
branch, so it is receipted like one. The receipt is the primary artifact
of every compaction (proactive or reactive):

``{task_id, worker_id, trigger, pre_sha256, post_sha256, tokens_before,
tokens_after, validators: [{name, pass|fail}], retry_count, ts}``

Three anchors make the receipt load-bearing rather than decorative:

* **Audit chain** - :func:`record_compaction_receipt` appends the
  receipt as a ``compaction.receipt`` event via
  ``AuditChainStore.log_with_prev_digest``, so the previous chain head
  is embedded in the payload before the HMAC is computed.
* **Step journal** - :func:`record_compaction_journal_step` registers
  the compaction as a replay step whose hashed payload carries the
  receipt's pre/post SHA-256. Replaying across the boundary re-verifies
  those hashes: editing them on disk breaks the journal's hash chain.
  No journal schema change is needed - the compaction rides in the
  existing ``tool_call`` slot, so every pre-existing journal verifies
  unchanged.
* **Spend ledger** - :func:`record_ledger_delta` writes a zero-cost
  ledger row tagged with the receipt correlation id;
  :func:`reconcile_with_ledger` proves ``tokens_before/after`` in the
  receipt match the ledger delta recorded for the task.

:func:`verify_compaction_receipts` is the audit-verification hook: a
journaled compaction step without a matching, chain-verifiable receipt
fails the run's audit verification.

Gate outcomes (redactions/refusals) are already chained by the
sensitive-gate lane as ``compaction.sensitive_gate`` events; the receipt
references them via ``gate_action``/``gate_rule_ids`` and never
duplicates their span evidence.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.cost.spend_ledger import LedgerEntry, SpendLedger
    from bernstein.core.persistence.journal import Journal, JournalEntry, JournalReader
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tokens.compaction_validate import ValidatorVerdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ``tool_call.kind`` marker for compaction steps in the replay journal.
COMPACTION_STEP_KIND: Final[str] = "compaction"

#: ``feature_label`` for the zero-cost compaction rows in the spend ledger.
LEDGER_FEATURE_LABEL: Final[str] = "compaction"

#: Allowed values for :attr:`CompactionReceipt.trigger`.
VALID_TRIGGERS: Final[frozenset[str]] = frozenset({"proactive", "reactive"})

#: Ledger tag key carrying the receipt correlation id.
_LEDGER_CORRELATION_TAG: Final[str] = "compaction_correlation_id"


def sha256_hex(text: str) -> str:
    """Return the lower-case SHA-256 hex digest of *text* (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompactionReceipt:
    """One compaction event, in the shape the audit chain records it.

    Attributes:
        task_id: Task whose context was compacted.
        worker_id: Agent session that owned the context.
        trigger: ``"proactive"`` (threshold tick) or ``"reactive"``
            (post-overflow recovery).
        pre_sha256: SHA-256 of the context before compaction.
        post_sha256: SHA-256 of the context after compaction.
        tokens_before: Token count before compaction.
        tokens_after: Token count after compaction.
        validators: ``(name, passed)`` pairs in validator run order.
        retry_count: Fix passes executed before the summary validated.
        ts: Unix epoch seconds when the receipt was built.
        correlation_id: Id shared by the chain event, the journal step,
            and the ledger row for this compaction.
        gate_action: Sensitive-gate outcome reference (``allow`` /
            ``redacted`` / ``refused``). The gate's own events carry the
            evidence; the receipt only points at them.
        gate_rule_ids: Deny-rule ids the gate fired, in span order.
        skills_reinjected: Whether skills were re-injected into the
            worker context after compaction.
    """

    task_id: str
    worker_id: str
    trigger: str
    pre_sha256: str
    post_sha256: str
    tokens_before: int
    tokens_after: int
    validators: tuple[tuple[str, bool], ...]
    retry_count: int
    ts: float
    correlation_id: str
    gate_action: str = "allow"
    gate_rule_ids: tuple[str, ...] = ()
    skills_reinjected: bool = False

    def __post_init__(self) -> None:
        if self.trigger not in VALID_TRIGGERS:
            msg = f"trigger must be one of {sorted(VALID_TRIGGERS)}, got {self.trigger!r}"
            raise ValueError(msg)

    def to_details(self) -> dict[str, Any]:
        """Return the JSON-safe payload recorded in the audit chain."""
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "trigger": self.trigger,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "validators": [{"name": name, "result": "pass" if passed else "fail"} for name, passed in self.validators],
            "retry_count": self.retry_count,
            "ts": self.ts,
            "correlation_id": self.correlation_id,
            "gate_action": self.gate_action,
            "gate_rule_ids": list(self.gate_rule_ids),
            "skills_reinjected": self.skills_reinjected,
        }


def build_receipt(
    *,
    task_id: str,
    worker_id: str,
    trigger: str,
    pre_text: str,
    post_text: str,
    tokens_before: int,
    tokens_after: int,
    verdicts: Sequence[ValidatorVerdict],
    retry_count: int,
    gate_action: str = "allow",
    gate_rule_ids: Sequence[str] = (),
    skills_reinjected: bool = False,
    correlation_id: str | None = None,
    ts: float | None = None,
) -> CompactionReceipt:
    """Build a receipt from the raw pre/post texts and validator verdicts.

    Args:
        task_id: Task whose context was compacted.
        worker_id: Owning agent session id.
        trigger: ``"proactive"`` or ``"reactive"``.
        pre_text: Context before compaction (hashed, never stored).
        post_text: Context after compaction (hashed, never stored).
        tokens_before: Token count before compaction.
        tokens_after: Token count after compaction.
        verdicts: Validator verdicts for the accepted summary.
        retry_count: Fix passes executed.
        gate_action: Sensitive-gate outcome reference.
        gate_rule_ids: Gate deny-rule ids that fired.
        skills_reinjected: Whether skills were re-injected afterwards.
        correlation_id: Explicit correlation id; generated when omitted.
        ts: Explicit timestamp; ``time.time()`` when omitted.

    Returns:
        The immutable :class:`CompactionReceipt`.
    """
    return CompactionReceipt(
        task_id=task_id,
        worker_id=worker_id,
        trigger=trigger,
        pre_sha256=sha256_hex(pre_text),
        post_sha256=sha256_hex(post_text),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        validators=tuple((v.name, v.passed) for v in verdicts),
        retry_count=retry_count,
        ts=ts if ts is not None else time.time(),
        correlation_id=correlation_id or f"compact-{uuid.uuid4().hex[:8]}",
        gate_action=gate_action,
        gate_rule_ids=tuple(gate_rule_ids),
        skills_reinjected=skills_reinjected,
    )


def receipt_from_details(details: dict[str, Any]) -> CompactionReceipt:
    """Rebuild a receipt from a chain event's details payload.

    Args:
        details: The ``details`` dict of a ``compaction.receipt`` event.

    Returns:
        The reconstructed :class:`CompactionReceipt`.
    """
    raw_validators = details.get("validators") or []
    validators = tuple((str(v.get("name", "")), v.get("result") == "pass") for v in raw_validators)
    return CompactionReceipt(
        task_id=str(details.get("task_id", "")),
        worker_id=str(details.get("worker_id", "")),
        trigger=str(details.get("trigger", "")),
        pre_sha256=str(details.get("pre_sha256", "")),
        post_sha256=str(details.get("post_sha256", "")),
        tokens_before=int(details.get("tokens_before", 0)),
        tokens_after=int(details.get("tokens_after", 0)),
        validators=validators,
        retry_count=int(details.get("retry_count", 0)),
        ts=float(details.get("ts", 0.0)),
        correlation_id=str(details.get("correlation_id", "")),
        gate_action=str(details.get("gate_action", "allow")),
        gate_rule_ids=tuple(str(r) for r in details.get("gate_rule_ids") or ()),
        skills_reinjected=bool(details.get("skills_reinjected", False)),
    )


# ---------------------------------------------------------------------------
# Audit-chain recording
# ---------------------------------------------------------------------------


def record_compaction_receipt(*, chain: AuditChainStore, receipt: CompactionReceipt) -> AuditEvent:
    """Append a ``compaction.receipt`` event into *chain*.

    The previous chain digest is embedded in the payload before the
    HMAC is computed (``log_with_prev_digest``), so the receipt is
    position-locked in the chain, not merely appended to it.

    Args:
        chain: The audit chain store accepting the entry.
        receipt: The receipt to record.

    Returns:
        The recorded ``AuditEvent``.
    """
    from bernstein.core.security.audit_chain import EVENT_COMPACTION_RECEIPT

    return chain.log_with_prev_digest(
        event_type=EVENT_COMPACTION_RECEIPT,
        actor=receipt.worker_id,
        resource_type="compaction",
        resource_id=receipt.task_id,
        details=receipt.to_details(),
    )


def load_receipts(chain: AuditChainStore, *, task_id: str | None = None) -> list[CompactionReceipt]:
    """Return receipts recorded in *chain*, optionally filtered by task.

    Malformed receipt events (tampered or truncated payloads) are
    skipped with a warning; :func:`verify_compaction_receipts` is the
    surface that turns them into hard verification errors.

    Args:
        chain: The audit chain store to query.
        task_id: When given, only receipts for this task are returned.

    Returns:
        Receipts in chain order.
    """
    receipts: list[CompactionReceipt] = []
    for event, parse_error in _iter_receipt_events(chain):
        if parse_error is not None:
            from bernstein.core.security.sanitize import sanitize_log

            logger.warning("Skipping malformed compaction receipt event: %s", sanitize_log(parse_error))
            continue
        if task_id is not None and event.task_id != task_id:
            continue
        receipts.append(event)
    return receipts


def _iter_receipt_events(
    chain: AuditChainStore,
) -> list[tuple[CompactionReceipt, None] | tuple[None, str]]:
    """Parse every ``compaction.receipt`` event, capturing parse failures.

    Returns:
        One tuple per event: ``(receipt, None)`` on success or
        ``(None, error)`` for a payload that no longer parses (a
        tampered chain row, for example).
    """
    from bernstein.core.security.audit_chain import EVENT_COMPACTION_RECEIPT

    out: list[tuple[CompactionReceipt, None] | tuple[None, str]] = []
    for event in chain.query(event_type=EVENT_COMPACTION_RECEIPT):
        try:
            out.append((receipt_from_details(event.details), None))
        except (ValueError, TypeError) as exc:
            out.append((None, f"receipt event at {event.timestamp} unparseable: {exc}"))
    return out


# ---------------------------------------------------------------------------
# Step-journal registration
# ---------------------------------------------------------------------------


def record_compaction_journal_step(journal: Journal, receipt: CompactionReceipt) -> JournalEntry:
    """Register the compaction as a step in the replay journal.

    The receipt's pre/post hashes ride inside ``tool_call``, which is
    part of the canonical hashed payload - replay across the boundary
    therefore re-verifies them, and any edit surfaces as hash
    divergence. Uses only the existing ``Journal.append`` surface, so
    no journal schema change (and no old-journal invalidation) occurs.

    Args:
        journal: The open journal for the compacted worker.
        receipt: The receipt whose hashes anchor the step.

    Returns:
        The appended :class:`JournalEntry`.
    """
    return journal.append(
        input_hash=receipt.pre_sha256,
        tool_call={
            "kind": COMPACTION_STEP_KIND,
            "task_id": receipt.task_id,
            "trigger": receipt.trigger,
            "pre_sha256": receipt.pre_sha256,
            "post_sha256": receipt.post_sha256,
            "correlation_id": receipt.correlation_id,
        },
        tool_result={
            "tokens_before": receipt.tokens_before,
            "tokens_after": receipt.tokens_after,
            "retry_count": receipt.retry_count,
            "validators": receipt.to_details()["validators"],
        },
    )


def find_compaction_steps(reader: JournalReader) -> list[JournalEntry]:
    """Return every compaction step registered in *reader*'s journal."""
    steps: list[JournalEntry] = []
    for entry in reader.entries():
        call = entry.tool_call
        if isinstance(call, dict) and call.get("kind") == COMPACTION_STEP_KIND:
            steps.append(entry)
    return steps


# ---------------------------------------------------------------------------
# Verification (AC #2)
# ---------------------------------------------------------------------------


def verify_compaction_receipts(
    chain: AuditChainStore,
    *,
    journal_reader: JournalReader | None = None,
    task_id: str | None = None,
) -> tuple[bool, list[str]]:
    """Verify that every compaction event has a chain-verifiable receipt.

    Three checks, all of which must hold:

    1. The audit chain's HMAC chain verifies end to end.
    2. Every compaction step in the replay journal (when a reader is
       supplied) has a ``compaction.receipt`` event whose correlation id
       matches.
    3. The receipt's pre/post hashes equal the journaled step's hashes.

    Args:
        chain: The audit chain store for the run.
        journal_reader: Optional reader over the worker's replay
            journal; compaction steps found there must be receipted.
        task_id: Restrict receipt lookup to one task.

    Returns:
        ``(ok, errors)``; ``ok`` is False when any compaction event
        lacks a chain-verifiable receipt.
    """
    errors: list[str] = []

    chain_ok, chain_errors = chain.verify()
    if not chain_ok:
        errors.extend(f"audit chain: {err}" for err in chain_errors)

    receipts: dict[str, CompactionReceipt] = {}
    for receipt, parse_error in _iter_receipt_events(chain):
        if parse_error is not None:
            errors.append(f"audit chain: {parse_error}")
            continue
        if task_id is not None and receipt.task_id != task_id:
            continue
        receipts[receipt.correlation_id] = receipt

    if journal_reader is not None:
        journal_result = journal_reader.verify()
        if not journal_result.ok:
            errors.extend(f"journal: {err}" for err in journal_result.errors)
        for step in find_compaction_steps(journal_reader):
            call = step.tool_call
            correlation_id = str(call.get("correlation_id", ""))
            receipt = receipts.get(correlation_id)
            if receipt is None:
                errors.append(
                    f"compaction step seq={step.seq} (correlation={correlation_id or 'missing'}) "
                    f"has no chain receipt; audit verification fails"
                )
                continue
            if receipt.pre_sha256 != call.get("pre_sha256") or receipt.post_sha256 != call.get("post_sha256"):
                errors.append(
                    f"compaction step seq={step.seq} pre/post hash mismatch against chain receipt {correlation_id}"
                )

    return (not errors, errors)


# ---------------------------------------------------------------------------
# Ledger reconciliation (AC #5)
# ---------------------------------------------------------------------------


def record_ledger_delta(ledger: SpendLedger, receipt: CompactionReceipt) -> None:
    """Write the compaction token delta into the spend ledger.

    The row is zero-cost (compaction is not model spend) and carries the
    receipt correlation id as a tag so
    :func:`reconcile_with_ledger` can match it back deterministically.

    Args:
        ledger: The per-run spend ledger.
        receipt: The receipt whose token delta is recorded.
    """
    from bernstein.core.cost.spend_ledger import CallTags

    ledger.record(
        tags=CallTags(
            task_id=receipt.task_id,
            agent_id=receipt.worker_id,
            feature_label=LEDGER_FEATURE_LABEL,
            extra={_LEDGER_CORRELATION_TAG: receipt.correlation_id},
        ),
        model="",
        cost_usd=0.0,
        input_tokens=receipt.tokens_before,
        output_tokens=receipt.tokens_after,
        ts=receipt.ts,
    )


def reconcile_with_ledger(
    receipts: Iterable[CompactionReceipt],
    ledger_entries: Iterable[LedgerEntry],
) -> tuple[bool, list[str]]:
    """Check receipt token counts against the ledger rows for each task.

    Args:
        receipts: Receipts to reconcile.
        ledger_entries: Ledger rows (e.g. ``SpendLedger.load_entries``).

    Returns:
        ``(ok, errors)``; ``ok`` is False when a receipt has no ledger
        row or its ``tokens_before/after`` disagree with the ledger
        delta recorded for the task.
    """
    rows = {
        entry.tags.get(_LEDGER_CORRELATION_TAG, ""): entry
        for entry in ledger_entries
        if entry.feature_label == LEDGER_FEATURE_LABEL
    }
    errors: list[str] = []
    for receipt in receipts:
        row = rows.get(receipt.correlation_id)
        if row is None:
            errors.append(
                f"receipt {receipt.correlation_id} (task {receipt.task_id}): no ledger row with matching correlation id"
            )
            continue
        if row.task_id != receipt.task_id:
            errors.append(
                f"receipt {receipt.correlation_id}: ledger row belongs to task "
                f"{row.task_id!r}, receipt says {receipt.task_id!r}"
            )
            continue
        if row.input_tokens != receipt.tokens_before or row.output_tokens != receipt.tokens_after:
            errors.append(
                f"receipt {receipt.correlation_id} (task {receipt.task_id}): tokens "
                f"{receipt.tokens_before}->{receipt.tokens_after} do not reconcile with "
                f"ledger delta {row.input_tokens}->{row.output_tokens}"
            )
    return (not errors, errors)


__all__ = [
    "COMPACTION_STEP_KIND",
    "LEDGER_FEATURE_LABEL",
    "VALID_TRIGGERS",
    "CompactionReceipt",
    "build_receipt",
    "find_compaction_steps",
    "load_receipts",
    "receipt_from_details",
    "reconcile_with_ledger",
    "record_compaction_journal_step",
    "record_compaction_receipt",
    "record_ledger_delta",
    "sha256_hex",
    "verify_compaction_receipts",
]
