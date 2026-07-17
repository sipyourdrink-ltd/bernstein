"""SLA contract monitor: gather chain evidence, emit signed violation receipts.

Issue #2549. This is the read-only bridge between the schedule supervisor tick
and the pure SLA machinery. On each tick the monitor, for every registered
contract:

1. gathers the axis evidence it needs from the audit chain, the work ledger, the
   lineage spine, and the spend ledger (read-only),
2. runs the pure evaluators and the deterministic remediation + budget gate via
   :func:`bernstein.core.orchestration.sla_receipt.build_receipt`,
3. on a breach, signs the receipt, persists it, appends one ``sla.violation``
   event to the HMAC audit chain, and normalises the breach into a
   :class:`TriggerEvent` handed to an optional sink.

The only side effects of a breach are the chain event, the receipt, and the
normalised trigger event: the monitor never dispatches a task. Evidence is
gathered as plain data and embedded in the receipt, so the receipt re-derives
its verdict offline (see :mod:`bernstein.core.orchestration.sla_receipt`).

The evidence provider is injectable so tests can drive the monitor without a
disk tree; the default provider reads the on-disk substrate.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.observability.sla_eval import project_error_budget
from bernstein.core.orchestration.sla_receipt import (
    SLAViolationReceipt,
    build_receipt,
    keyid_for,
    sign_receipt,
    write_receipt,
)
from bernstein.core.orchestration.supervisor_receipt import IdentityTokens
from bernstein.core.planning.sla_store import (
    AXIS_DURATION,
    AXIS_FREQUENCY,
    AXIS_FRESHNESS,
    AXIS_LATENESS,
    AXIS_SPEND_RATE,
    SUBJECT_ENVELOPE,
    SUBJECT_SCHEDULE,
    SLAStore,
)
from bernstein.core.trigger_sources.sla import normalize_sla_violation

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from bernstein.core.planning.sla_store import SLAContract
    from bernstein.core.tasks.models import TriggerEvent

logger = logging.getLogger(__name__)

#: Default number of trailing audit entries captured in a receipt's chain slice.
DEFAULT_AUDIT_WINDOW = 16


# ---------------------------------------------------------------------------
# Disk evidence readers (read-only)
# ---------------------------------------------------------------------------


def _row_hash(row: dict[str, Any]) -> str:
    """Return a deterministic content hash for an evidence row lacking one."""
    return "sha256:" + hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_audit_window(sdd_dir: Path, *, window: int = DEFAULT_AUDIT_WINDOW) -> list[dict[str, Any]]:
    """Return the trailing ``window`` audit-chain entries as dicts (read-only)."""
    audit_dir = sdd_dir / "audit"
    if not audit_dir.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(cast("dict[str, Any]", parsed))
    return entries[-window:]


def read_fire_events(sdd_dir: Path, schedule_id: str) -> list[dict[str, Any]]:
    """Return ``schedule.fire`` rows for ``schedule_id`` from the audit chain."""
    audit_dir = sdd_dir / "audit"
    if not audit_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_dict = cast("dict[str, Any]", entry)
            if entry_dict.get("event_type") != "schedule.fire":
                continue
            details_raw = entry_dict.get("details")
            if not isinstance(details_raw, dict):
                continue
            details = cast("dict[str, Any]", details_raw)
            if details.get("schedule_id") != schedule_id:
                continue
            rows.append(
                {
                    "fire_time": int(details.get("fire_time", 0)),
                    "entry_hash": str(entry_dict.get("hmac", "")),
                }
            )
    return rows


def _iter_ledger_runs(sdd_dir: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Yield ``(run_id, entry dicts)`` for every work ledger under ``.sdd``."""
    from bernstein.core.persistence.work_ledger import LedgerReader, default_ledger_root

    root = default_ledger_root(sdd_dir)
    out: list[tuple[str, list[dict[str, Any]]]] = []
    if not root.exists():
        return out
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        reader = LedgerReader(run_dir)
        rows = [e.to_dict() for e in reader.entries()]
        out.append((run_dir.name, rows))
    return out


def read_duration_rows(sdd_dir: Path, *, run_filter: str | None = None) -> list[dict[str, Any]]:
    """Return run-span rows (started / ended) paired per task from work ledgers.

    ``run_filter`` restricts to run ids whose name contains the filter (used to
    scope a task-family subject); ``None`` scans every run.
    """
    rows: list[dict[str, Any]] = []
    for run_id, entries in _iter_ledger_runs(sdd_dir):
        if run_filter is not None and run_filter not in run_id:
            continue
        started: dict[str, float] = {}
        for entry in entries:
            kind = str(entry.get("kind", ""))
            task_id = str(entry.get("task_id", ""))
            ts = float(entry.get("ts", 0.0))
            if kind == "task.started":
                started[task_id] = ts
            elif kind in {"task.completed", "task.failed", "task.abandoned"} and task_id in started:
                rows.append(
                    {
                        "task_id": task_id,
                        "started": started[task_id],
                        "ended": ts,
                        "entry_hash": str(entry.get("entry_hash", "")),
                    }
                )
    return rows


def read_freshness_rows(sdd_dir: Path, artifact_path: str) -> list[dict[str, Any]]:
    """Return lineage-spine rows for ``artifact_path`` across all runs (read-only).

    The verdict is computed purely from these spine entries -- their timestamps
    and hashes -- so freshness is checkable offline with no access to the
    artifact bytes.
    """
    from bernstein.core.security.audit import load_or_create_audit_key

    lineage_root = sdd_dir / "lineage"
    if not lineage_root.exists():
        return []
    try:
        key = load_or_create_audit_key()
    except Exception:  # pragma: no cover - defensive
        key = b""
    from bernstein.core.lineage.spine import LineageSpine

    rows: list[dict[str, Any]] = []
    for run_dir in sorted(p for p in lineage_root.iterdir() if p.is_dir()):
        spine = LineageSpine(lineage_root, run_id=run_dir.name, hmac_key=key)
        for entry in spine.iter_entries():
            if entry.artifact_path != artifact_path:
                continue
            rows.append(
                {
                    "artifact_path": entry.artifact_path,
                    "content_hash": entry.content_hash,
                    "timestamp": int(entry.timestamp),
                    "entry_hash": entry.entry_hash,
                }
            )
    return rows


def read_spend_rows(sdd_dir: Path, *, envelope: str | None = None) -> list[dict[str, Any]]:
    """Return spend-ledger rows (cost / timestamp), optionally scoped to an envelope."""
    ledger_path = sdd_dir / "cost" / "ledger.jsonl"
    if not ledger_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = ledger_path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, Any]", raw)
        if envelope is not None and str(entry.get("quota_envelope", "subscription")) != envelope:
            continue
        row: dict[str, Any] = {
            "cost_usd": float(entry.get("cost_usd", 0.0)),
            "timestamp": int(float(entry.get("ts", 0.0))),
        }
        row["entry_hash"] = _row_hash(row)
        rows.append(row)
    return rows


def read_ledger_segment(sdd_dir: Path, *, run_filter: str | None = None) -> list[dict[str, Any]]:
    """Return the concatenated work-ledger entry dicts for the error budget.

    ``run_filter`` scopes to matching run ids; ``None`` reads every run. Rows are
    plain dicts carrying ``kind`` and ``entry_hash`` so
    :func:`project_error_budget` recomputes byte-identically.
    """
    segment: list[dict[str, Any]] = []
    for run_id, entries in _iter_ledger_runs(sdd_dir):
        if run_filter is not None and run_filter not in run_id:
            continue
        segment.extend(entries)
    return segment


def default_evidence_provider(sdd_dir: Path) -> Callable[[SLAContract, int], dict[str, Any]]:
    """Return a disk-backed evidence provider closure for the monitor."""

    def _provider(contract: SLAContract, now: int) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        if contract.subject_type == SUBJECT_SCHEDULE:
            fires = read_fire_events(sdd_dir, contract.subject_id)
            if contract.fire_frequency_s > 0:
                evidence[AXIS_FREQUENCY] = fires
            if contract.start_lateness_s > 0:
                evidence[AXIS_LATENESS] = _lateness_rows(sdd_dir, contract.subject_id, fires)
        if contract.max_run_duration_s > 0:
            run_filter = None if contract.subject_type == SUBJECT_SCHEDULE else contract.subject_id
            evidence[AXIS_DURATION] = read_duration_rows(sdd_dir, run_filter=run_filter)
        if contract.artifact_freshness_s > 0:
            evidence[AXIS_FRESHNESS] = read_freshness_rows(sdd_dir, contract.artifact_path)
        if contract.spend_rate_usd_per_hour > 0:
            envelope = contract.subject_id if contract.subject_type == SUBJECT_ENVELOPE else None
            evidence[AXIS_SPEND_RATE] = read_spend_rows(sdd_dir, envelope=envelope)
        return evidence

    return _provider


def _lateness_rows(sdd_dir: Path, schedule_id: str, fires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each fire with the earliest task start recorded in its fire run."""
    from bernstein.core.orchestration.schedule_fire_record import fire_run_id

    rows: list[dict[str, Any]] = []
    ledger_runs = dict(_iter_ledger_runs(sdd_dir))
    for fire in fires:
        fire_time = int(fire.get("fire_time", 0))
        run_id = fire_run_id(schedule_id, fire_time)
        starts = [
            float(e.get("ts", 0.0)) for e in ledger_runs.get(run_id, []) if str(e.get("kind", "")) == "task.started"
        ]
        if not starts:
            continue
        rows.append(
            {
                "fire_time": fire_time,
                "start_time": min(starts),
                "entry_hash": str(fire.get("entry_hash", "")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Report projection (deterministic error budget over a ledger segment)
# ---------------------------------------------------------------------------


def build_report(sdd_dir: Path, contract: SLAContract) -> dict[str, Any]:
    """Return the deterministic error-budget report JSON for a contract.

    Two independent checkouts holding the same work-ledger segment produce
    byte-identical output: the report contains only the contract identity and
    the pure projection, never a timestamp or host detail.
    """
    run_filter = None if contract.subject_type == SUBJECT_SCHEDULE else contract.subject_id
    segment = read_ledger_segment(sdd_dir, run_filter=run_filter)
    projection = project_error_budget(contract, segment)
    return {
        "contract_id": contract.id,
        "contract_hash": contract.contract_hash,
        "subject_type": contract.subject_type,
        "subject_id": contract.subject_id,
        "error_budget": projection.to_dict(),
    }


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class SLAMonitor:
    """Evaluate registered SLA contracts against chain evidence on each tick.

    The monitor is read-only over evidence and never dispatches a task; a breach
    yields exactly three side effects: a signed receipt on disk, one
    ``sla.violation`` audit-chain event, and a normalised trigger event handed
    to ``trigger_sink``.
    """

    def __init__(
        self,
        *,
        store: SLAStore,
        signing_key: Ed25519PrivateKey,
        public_key: Ed25519PublicKey,
        chain: Any | None = None,
        install_rev: str = "",
        run_id: str = "",
        caps: dict[str, float] | None = None,
        trigger_sink: Callable[[TriggerEvent], None] | None = None,
        evidence_provider: Callable[[SLAContract, int], dict[str, Any]] | None = None,
        audit_window: int = DEFAULT_AUDIT_WINDOW,
    ) -> None:
        self._store = store
        self._signing_key = signing_key
        self._public_key = public_key
        self._chain = chain
        self._identity = IdentityTokens(install_rev=install_rev, keyid=keyid_for(public_key), run_id=run_id)
        self._caps = caps
        self._trigger_sink = trigger_sink
        self._audit_window = audit_window
        self._evidence_provider = evidence_provider or default_evidence_provider(store.sdd_dir)

    def evaluate(self, now: int) -> list[SLAViolationReceipt]:
        """Evaluate every registered contract at ``now``; return emitted receipts.

        Read-only over evidence: the only writes are the receipt, the chain
        event, and the trigger event. Never dispatches a task.
        """
        emitted: list[SLAViolationReceipt] = []
        audit_entries = read_audit_window(self._store.sdd_dir, window=self._audit_window)
        prev = self._chain.prev_chain_digest if self._chain is not None else ""
        for contract in self._store.list():
            try:
                receipt = self._evaluate_one(contract, now, audit_entries, prev)
            except Exception:  # pragma: no cover - defensive: one bad contract must not wedge the tick
                logger.exception("SLA evaluation failed for contract %s", contract.id)
                continue
            if receipt is not None:
                emitted.append(receipt)
        return emitted

    def _evaluate_one(
        self,
        contract: SLAContract,
        now: int,
        audit_entries: list[dict[str, Any]],
        prev: str,
    ) -> SLAViolationReceipt | None:
        evidence = self._evidence_provider(contract, now)
        receipt = build_receipt(
            contract=contract,
            evidence=evidence,
            now=now,
            caps=self._caps,
            audit_entries=audit_entries,
            identity=self._identity,
            public_key=self._public_key,
            prev_chain_digest=prev,
        )
        if receipt is None:
            return None
        signed = sign_receipt(receipt, signing_key=self._signing_key)
        write_receipt(self._store.sdd_dir, signed)
        self._record_chain_event(signed)
        if self._trigger_sink is not None:
            self._trigger_sink(normalize_sla_violation(signed))
        return signed

    def _record_chain_event(self, receipt: SLAViolationReceipt) -> None:
        if self._chain is None:
            return
        from bernstein.core.security.audit_chain import record_sla_violation

        breached = [str(v.get("axis", "")) for v in receipt.verdicts if v.get("breached")]
        remediation = receipt.remediation
        record_sla_violation(
            chain=self._chain,
            contract_id=receipt.contract_id,
            contract_hash=receipt.contract_hash,
            subject_type=str(receipt.contract_body.get("subject_type", "")),
            subject_id=str(receipt.contract_body.get("subject_id", "")),
            tick_instant=receipt.tick_instant,
            breached_axes=breached,
            requested_action=str(remediation.get("requested_action", "")),
            effective_action=str(remediation.get("effective_action", "")),
            remediation_blocked=bool(remediation.get("blocked", False)),
            receipt_digest=receipt.payload_digest,
        )


def build_monitor_from_sdd(
    sdd_dir: Path,
    *,
    chain: Any | None = None,
    caps: dict[str, float] | None = None,
    trigger_sink: Callable[[TriggerEvent], None] | None = None,
) -> SLAMonitor:
    """Build an :class:`SLAMonitor` wired to the on-disk substrate under ``sdd_dir``.

    Resolves (or creates) the install SLA signing identity, the SLA contract
    store, and the disk evidence provider. Used by the supervisor wiring and the
    CLI so both drive the identical monitor.
    """
    from cryptography.hazmat.primitives import serialization

    from bernstein.core.lineage.identity import load_or_create_signing_identity

    priv_pem, pub_pem = load_or_create_signing_identity(
        sdd_dir / "identity",
        private_name="sla_signing.pem",
        public_name="sla_signing.pub",
    )
    signing_key = serialization.load_pem_private_key(priv_pem.encode("ascii"), password=None)
    public_key = serialization.load_pem_public_key(pub_pem.encode("ascii"))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    if not isinstance(signing_key, Ed25519PrivateKey) or not isinstance(public_key, Ed25519PublicKey):
        msg = "SLA signing identity is not an Ed25519 keypair"
        raise TypeError(msg)
    return SLAMonitor(
        store=SLAStore(sdd_dir),
        signing_key=signing_key,
        public_key=public_key,
        chain=chain,
        caps=caps,
        trigger_sink=trigger_sink,
    )


__all__ = [
    "DEFAULT_AUDIT_WINDOW",
    "SLAMonitor",
    "build_monitor_from_sdd",
    "build_report",
    "default_evidence_provider",
    "read_audit_window",
    "read_duration_rows",
    "read_fire_events",
    "read_freshness_rows",
    "read_ledger_segment",
    "read_spend_rows",
]
