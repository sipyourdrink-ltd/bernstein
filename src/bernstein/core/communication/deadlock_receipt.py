"""Signed receipts for mailbox-rendezvous deadlocks (#3450).

The mailbox chain is the source of truth.  This module folds its outstanding
``rendezvous_open`` entries through :func:`wait_graph`, names one cycle with
:func:`find_cycle`, and records the exact opens supporting that cycle in the
shared signed-receipt envelope.  Receipt identity includes both the canonical
task cycle and those open-entry hashes, so repeated ticks and orchestrator
restarts do not duplicate one incident while a later reopened cycle remains a
distinct incident.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.communication.rendezvous import closed_entries, find_cycle, open_entries, wait_graph
from bernstein.core.lineage.identity import load_or_create_signing_identity
from bernstein.core.persistence.atomic_write import write_atomic_json
from bernstein.core.receipts.protocol import (
    ReceiptEnvelope,
    canonical_receipt_bytes,
    register_receipt_kind,
    sign_receipt,
    verify_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from bernstein.core.communication.rendezvous import RendezvousOpen
    from bernstein.core.communication.task_mailbox import MailboxMessage

__all__ = [
    "DEADLOCK_RECEIPT_SCHEMA_VERSION",
    "RECEIPT_KIND",
    "CorruptDeadlockReceiptError",
    "DeadlockReceiptResult",
    "build_deadlock_payload",
    "deadlock_payload_errors",
    "emit_deadlock_receipt",
]

RECEIPT_KIND = "coordination.rendezvous_deadlock"
DEADLOCK_RECEIPT_SCHEMA_VERSION = "rendezvous-deadlock/v1"
_RECEIPT_SUBPATH = ("runtime", "deadlock-receipts")
_PRIVATE_KEY_NAME = "deadlock_receipt_signing.pem"
_PUBLIC_KEY_NAME = "deadlock_receipt_signing.pub"


class CorruptDeadlockReceiptError(ValueError):
    """An existing content-addressed receipt cannot be trusted or reused."""

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.path = path


@dataclass(frozen=True, slots=True)
class DeadlockReceiptResult:
    """Result of inspecting one mailbox snapshot for a wait-cycle receipt."""

    receipt: ReceiptEnvelope | None
    path: Path | None
    emitted: bool


def _cycle_edges(cycle: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return tuple((task_id, cycle[(index + 1) % len(cycle)]) for index, task_id in enumerate(cycle))


def _supporting_opens(
    messages: Sequence[MailboxMessage],
    cycle: Sequence[str],
) -> tuple[RendezvousOpen, ...]:
    """Return one deterministic outstanding open for every edge in ``cycle``.

    A valid cooperative wait has one outstanding open per waiter.  If a
    malformed or concurrent chain contains parallel opens for the same edge,
    the chain-latest one is the evidence selected for this snapshot.  The
    ``(seq, entry_hash)`` ordering matches the rendezvous parser's total-order
    tie break and is independent of input list order.
    """
    resolved = {entry.open_entry_hash for entry in closed_entries(messages)}
    outstanding = [entry for entry in open_entries(messages) if entry.entry_hash not in resolved]
    selected: list[RendezvousOpen] = []
    for waiter, awaited in _cycle_edges(cycle):
        matches = [
            entry for entry in outstanding if entry.waiter_task_id == waiter and entry.awaited_task_id == awaited
        ]
        if not matches:
            raise ValueError(f"cycle edge {waiter!r} -> {awaited!r} has no outstanding open entry")
        selected.append(max(matches, key=lambda entry: (entry.seq, entry.entry_hash)))
    return tuple(selected)


def _cycle_id(task_ids: Sequence[str], open_entry_hashes: Sequence[str]) -> str:
    identity = canonical_receipt_bytes(
        {
            "open_entry_hashes": list(open_entry_hashes),
            "task_ids": list(task_ids),
        }
    )
    return hashlib.sha256(identity).hexdigest()


def build_deadlock_payload(messages: Sequence[MailboxMessage]) -> dict[str, Any] | None:
    """Build the deterministic payload for the first cycle in ``messages``."""
    cycle = find_cycle(wait_graph(messages))
    if cycle is None:
        return None
    supporting = _supporting_opens(messages, cycle)
    latest = max(supporting, key=lambda entry: (entry.seq, entry.entry_hash))
    open_hashes = [entry.entry_hash for entry in supporting]
    payload: dict[str, Any] = {
        "schema_version": DEADLOCK_RECEIPT_SCHEMA_VERSION,
        "cycle_id": _cycle_id(cycle, open_hashes),
        "task_ids": list(cycle),
        "open_entry_hashes": open_hashes,
        "breaking_open_entry_hash": latest.entry_hash,
    }
    errors = deadlock_payload_errors(payload)
    if errors:
        raise ValueError("invalid deadlock receipt payload: " + "; ".join(errors))
    return payload


def deadlock_payload_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return semantic errors in a rendezvous-deadlock receipt payload."""
    errors: list[str] = []
    if payload.get("schema_version") != DEADLOCK_RECEIPT_SCHEMA_VERSION:
        errors.append("unknown schema_version")
    task_ids_raw: object = payload.get("task_ids")
    open_hashes_raw: object = payload.get("open_entry_hashes")
    task_ids = cast("list[object]", task_ids_raw) if isinstance(task_ids_raw, list) else []
    open_hashes = cast("list[object]", open_hashes_raw) if isinstance(open_hashes_raw, list) else []
    if not task_ids or not all(isinstance(item, str) and item for item in task_ids):
        errors.append("task_ids must be a non-empty list of strings")
    if not open_hashes or not all(isinstance(item, str) and item for item in open_hashes):
        errors.append("open_entry_hashes must be a non-empty list of strings")
    if len(task_ids) != len(open_hashes):
        errors.append("task_ids and open_entry_hashes must have equal length")
    breaking: object = payload.get("breaking_open_entry_hash")
    if not isinstance(breaking, str) or not breaking:
        errors.append("breaking_open_entry_hash must be a non-empty string")
    elif breaking not in open_hashes:
        errors.append("breaking_open_entry_hash must name a supporting open")
    cycle_id: object = payload.get("cycle_id")
    if not isinstance(cycle_id, str) or len(cycle_id) != 64 or any(char not in "0123456789abcdef" for char in cycle_id):
        errors.append("cycle_id must be a sha256 hex digest")
    elif all(isinstance(item, str) for item in task_ids + open_hashes):
        expected = _cycle_id(cast("list[str]", task_ids), cast("list[str]", open_hashes))
        if cycle_id != expected:
            errors.append("cycle_id does not match the cycle identity")
    return tuple(errors)


register_receipt_kind(RECEIPT_KIND, payload_check=deadlock_payload_errors)


def _receipt_path(sdd_dir: Path, cycle_id: str) -> Path:
    return sdd_dir.joinpath(*_RECEIPT_SUBPATH, f"{cycle_id}.json")


def _load_existing(path: Path, expected_payload: Mapping[str, Any]) -> ReceiptEnvelope:
    try:
        raw_object: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorruptDeadlockReceiptError(f"existing deadlock receipt is invalid JSON: {path}", path=path) from exc
    except UnicodeDecodeError as exc:
        raise CorruptDeadlockReceiptError(f"existing deadlock receipt is invalid UTF-8: {path}", path=path) from exc
    if not isinstance(raw_object, dict):
        raise CorruptDeadlockReceiptError(f"existing deadlock receipt is not an object: {path}", path=path)
    raw = cast("dict[str, object]", raw_object)
    verification = verify_receipt(raw)
    if not verification.ok:
        raise CorruptDeadlockReceiptError(f"existing deadlock receipt does not verify: {path}", path=path)
    if raw.get("kind") != RECEIPT_KIND:
        raise CorruptDeadlockReceiptError(f"existing deadlock receipt has the wrong kind: {path}", path=path)
    payload: object = raw.get("payload")
    if payload != dict(expected_payload):
        raise CorruptDeadlockReceiptError(
            f"existing deadlock receipt payload disagrees with cycle identity: {path}",
            path=path,
        )
    return ReceiptEnvelope(
        kind=str(raw["kind"]),
        payload=cast("dict[str, Any]", payload),
        payload_digest=str(raw["payload_digest"]),
        canonicalization=str(raw["canonicalization"]),
        signature=str(raw["signature"]),
        public_key_pem=str(raw["public_key_pem"]),
    )


def emit_deadlock_receipt(*, messages: Sequence[MailboxMessage], sdd_dir: Path) -> DeadlockReceiptResult:
    """Persist one signed receipt for the mailbox snapshot's named cycle.

    The content-addressed path is the durable duplicate-suppression record, so
    a restarted orchestrator observing the same open entries returns the prior
    receipt rather than minting another one.
    """
    payload = build_deadlock_payload(messages)
    if payload is None:
        return DeadlockReceiptResult(receipt=None, path=None, emitted=False)
    path = _receipt_path(sdd_dir, str(payload["cycle_id"]))
    if path.is_file():
        return DeadlockReceiptResult(receipt=_load_existing(path, payload), path=path, emitted=False)

    private_pem, public_pem = load_or_create_signing_identity(
        sdd_dir / "identity",
        private_name=_PRIVATE_KEY_NAME,
        public_name=_PUBLIC_KEY_NAME,
    )
    receipt = sign_receipt(
        RECEIPT_KIND,
        payload,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
    )
    write_atomic_json(path, receipt.to_dict(), indent=2, sort_keys=True)
    return DeadlockReceiptResult(receipt=receipt, path=path, emitted=True)
