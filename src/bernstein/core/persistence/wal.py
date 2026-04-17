"""Write-Ahead Log (WAL) for orchestrator decisions.

Provides crash-safe durability and execution fingerprinting for the
Bernstein orchestrator. Every orchestrator decision is appended to a
hash-chained JSONL file before the action executes.

Storage: .sdd/runtime/wal/<run-id>.wal.jsonl

Features:
- Hash-chained JSONL entries (tamper-evident, integrity-verifiable)
- fsync per entry (crash-safe durability guarantee)
- Execution fingerprinting (determinism proof across runs)
- Crash recovery via uncommitted entry detection
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# Sentinel prev_hash for the first entry in a WAL.
GENESIS_HASH: str = "0" * 64


class WALIntegrityError(Exception):
    """Raised when WAL hash chain integrity is violated."""


@dataclass(frozen=True)
class WALEntry:
    """A single WAL entry representing one orchestrator decision.

    All fields are immutable. ``committed=False`` signals that the
    corresponding action had not yet been confirmed when this entry
    was written — useful for crash-recovery inspection.
    """

    seq: int
    prev_hash: str
    entry_hash: str
    timestamp: float
    decision_type: str
    inputs: dict[str, Any]
    output: dict[str, Any]
    actor: str
    committed: bool = True


def _compute_entry_hash(payload: dict[str, Any]) -> str:
    """Return SHA-256 of the canonical JSON of *payload*.

    *payload* must NOT contain the ``entry_hash`` key — the hash is
    computed over all other fields so it can be stored alongside them.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# WALWriter
# ---------------------------------------------------------------------------


class WALWriter:
    """Append-only WAL writer with hash chaining and per-entry fsync.

    Each call to :meth:`append` writes a JSON line, fsyncs the file, and
    returns the completed :class:`WALEntry`. The hash chain starts from
    :data:`GENESIS_HASH` (all zeros) for a new WAL, or resumes from the
    last recorded ``entry_hash`` when continuing an existing WAL.
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._path = sdd_dir / "runtime" / "wal" / f"{run_id}.wal.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._prev_hash = self._load_tail()

    def _load_tail(self) -> tuple[int, str]:
        """Return (last_seq, last_entry_hash) from an existing WAL file.

        Returns (-1, GENESIS_HASH) for a new or empty WAL.
        """
        if not self._path.exists():
            return -1, GENESIS_HASH

        non_empty = [ln for ln in self._path.read_text().splitlines() if ln.strip()]
        if not non_empty:
            return -1, GENESIS_HASH

        try:
            data = json.loads(non_empty[-1])
            return int(data["seq"]), str(data["entry_hash"])
        except (KeyError, ValueError):
            logger.warning("WAL tail unreadable at %s; chain will continue from truncation point", self._path)
            return len(non_empty) - 1, GENESIS_HASH

    def write_entry(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        actor: str,
        committed: bool = True,
    ) -> WALEntry:
        """Convenience alias for :meth:`append`."""
        return self.append(
            decision_type=decision_type,
            inputs=inputs,
            output=output,
            actor=actor,
            committed=committed,
        )

    def append(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        actor: str,
        committed: bool = True,
    ) -> WALEntry:
        """Append a decision entry to the WAL.

        The file is fsynced before returning, guaranteeing durability even
        if the process crashes immediately after this call returns.

        Args:
            decision_type: Short label for the decision (e.g. "task_created").
            inputs: Inputs to the decision (must be JSON-serializable).
            output: Result of the decision (must be JSON-serializable).
            actor: Identity of the orchestrator component writing this entry.
            committed: ``True`` (default) if the action has been executed;
                ``False`` to mark a pre-execution intent for crash recovery.

        Returns:
            The completed, hash-chained :class:`WALEntry`.
        """
        seq = self._seq + 1
        timestamp = time.time()

        payload: dict[str, Any] = {
            "seq": seq,
            "prev_hash": self._prev_hash,
            "timestamp": timestamp,
            "decision_type": decision_type,
            "inputs": inputs,
            "output": output,
            "actor": actor,
            "committed": committed,
        }
        entry_hash = _compute_entry_hash(payload)

        record = {**payload, "entry_hash": entry_hash}
        with self._path.open("a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

        entry = WALEntry(
            seq=seq,
            prev_hash=self._prev_hash,
            entry_hash=entry_hash,
            timestamp=timestamp,
            decision_type=decision_type,
            inputs=inputs,
            output=output,
            actor=actor,
            committed=committed,
        )
        self._seq = seq
        self._prev_hash = entry_hash
        return entry


# ---------------------------------------------------------------------------
# WALReader
# ---------------------------------------------------------------------------


class WALReader:
    """Read and verify a WAL file written by :class:`WALWriter`."""

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._path = sdd_dir / "runtime" / "wal" / f"{run_id}.wal.jsonl"

    def iter_entries(self) -> Iterator[WALEntry]:
        """Yield all :class:`WALEntry` objects in write order.

        Raises:
            FileNotFoundError: If the WAL file does not exist.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"WAL file not found: {self._path}")

        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            yield WALEntry(
                seq=int(data["seq"]),
                prev_hash=str(data["prev_hash"]),
                entry_hash=str(data["entry_hash"]),
                timestamp=float(data["timestamp"]),
                decision_type=str(data["decision_type"]),
                inputs=dict(data["inputs"]),
                output=dict(data["output"]),
                actor=str(data["actor"]),
                committed=bool(data.get("committed", True)),
            )

    def verify_chain(self) -> tuple[bool, list[str]]:
        """Verify hash chain integrity of the entire WAL.

        Checks that:
        1. Each entry's ``prev_hash`` equals the previous entry's ``entry_hash``.
        2. Each entry's ``entry_hash`` matches the SHA-256 of its payload.

        Returns:
            ``(True, [])`` if the chain is intact; ``(False, errors)`` otherwise.

        Raises:
            FileNotFoundError: If the WAL file does not exist.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"WAL file not found: {self._path}")

        errors: list[str] = []
        prev_hash = GENESIS_HASH

        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"Invalid JSON (seq unknown): {exc}")
                continue

            seq = data.get("seq", "?")
            stored_hash = str(data.get("entry_hash", ""))

            # Check prev_hash linkage
            if data.get("prev_hash") != prev_hash:
                errors.append(
                    f"Chain broken at seq {seq}: "
                    f"expected prev_hash {prev_hash[:8]}..., "
                    f"got {str(data.get('prev_hash', ''))[:8]}..."
                )

            # Recompute entry_hash from payload (exclude the stored entry_hash)
            payload = {k: v for k, v in data.items() if k != "entry_hash"}
            expected_hash = _compute_entry_hash(payload)

            if stored_hash != expected_hash:
                errors.append(f"Hash mismatch at seq {seq}: expected {expected_hash[:8]}..., got {stored_hash[:8]}...")

            # Advance prev_hash using stored value to detect cascading errors
            prev_hash = stored_hash

        return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# WALRecovery
# ---------------------------------------------------------------------------


class WALRecovery:
    """Crash recovery helper: find entries not yet committed at crash time.

    Usage pattern for crash-safe orchestration::

        # Before executing action:
        entry = writer.append(..., committed=False)
        # Execute action
        writer.append(..., committed=True)  # or a commit marker

        # On restart:
        recovery = WALRecovery(run_id, sdd_dir)
        for entry in recovery.get_uncommitted_entries():
            # re-execute or quarantine
            ...
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)

    def get_uncommitted_entries(self) -> list[WALEntry]:
        """Return all entries with ``committed=False``.

        Returns an empty list if the WAL file does not exist (fresh start).
        """
        try:
            return [e for e in self._reader.iter_entries() if not e.committed]
        except FileNotFoundError:
            return []

    @staticmethod
    def scan_all_uncommitted(
        sdd_dir: Path,
        *,
        exclude_run_id: str | None = None,
    ) -> list[tuple[str, WALEntry]]:
        """Scan all WAL files for uncommitted entries from previous runs.

        Iterates over every ``*.wal.jsonl`` file in the WAL directory, skipping
        *exclude_run_id* (typically the current run). Returns a flat list of
        ``(run_id, WALEntry)`` pairs for every entry with ``committed=False``.

        Returns an empty list when the WAL directory does not exist (fresh
        project with no prior runs).

        Args:
            sdd_dir: The ``.sdd`` directory root.
            exclude_run_id: Run ID to skip (the in-progress run).

        Returns:
            List of (run_id, uncommitted_entry) tuples.
        """
        wal_dir = sdd_dir / "runtime" / "wal"
        if not wal_dir.is_dir():
            return []

        results: list[tuple[str, WALEntry]] = []
        for wal_file in sorted(wal_dir.glob("*.wal.jsonl")):
            run_id = wal_file.name.removesuffix(".wal.jsonl")
            if run_id == exclude_run_id:
                continue
            recovery = WALRecovery(run_id=run_id, sdd_dir=sdd_dir)
            for entry in recovery.get_uncommitted_entries():
                results.append((run_id, entry))
        return results

    @staticmethod
    def find_orphaned_claims(
        sdd_dir: Path,
        *,
        exclude_run_id: str | None = None,
    ) -> list[tuple[str, WALEntry]]:
        """Return uncommitted ``task_claimed`` entries with no matching spawn.

        Scans each prior run's WAL for ``task_claimed`` entries written with
        ``committed=False`` that do NOT have a subsequent ``task_spawn_confirmed``
        entry for the same ``task_id`` in the same run.  These represent the
        work-loss window where the server moved a task to *claimed* but the
        orchestrator crashed before the agent was spawned -- on restart the
        task would otherwise sit in *claimed* forever (or be abandoned by
        ``_reconcile_claimed_tasks`` without a dedicated retry audit trail).

        Args:
            sdd_dir: The ``.sdd`` directory root.
            exclude_run_id: Run ID to skip (the in-progress run).

        Returns:
            List of ``(run_id, WALEntry)`` tuples for each orphaned claim.
        """
        wal_dir = sdd_dir / "runtime" / "wal"
        if not wal_dir.is_dir():
            return []

        orphans: list[tuple[str, WALEntry]] = []
        for wal_file in sorted(wal_dir.glob("*.wal.jsonl")):
            run_id = wal_file.name.removesuffix(".wal.jsonl")
            if run_id == exclude_run_id:
                continue
            reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)
            try:
                entries = list(reader.iter_entries())
            except FileNotFoundError:
                continue

            confirmed_task_ids: set[str] = {
                str(e.inputs.get("task_id", ""))
                for e in entries
                if e.decision_type == "task_spawn_confirmed" and e.committed
            }
            for entry in entries:
                if entry.decision_type != "task_claimed" or entry.committed:
                    continue
                task_id = str(entry.inputs.get("task_id", ""))
                if not task_id or task_id in confirmed_task_ids:
                    continue
                orphans.append((run_id, entry))
        return orphans


# ---------------------------------------------------------------------------
# ExecutionFingerprint
# ---------------------------------------------------------------------------


class ExecutionFingerprint:
    """Determinism fingerprint over an ordered sequence of orchestrator decisions.

    Two runs with the same fingerprint made identical decisions in identical
    order — a verifiable proof of determinism usable as a CI gate.

    The fingerprint is a SHA-256 computed iteratively over the sequence::

        state_0 = b""
        state_i = sha256(state_{i-1} || decision_type || ":" || inputs_hash || ":" || output_hash)
        fingerprint = sha256(state_n).hexdigest()

    where ``inputs_hash`` and ``output_hash`` are each the SHA-256 of the
    canonical JSON of the respective dict.
    """

    def __init__(self) -> None:
        self._state: bytes = b""

    def add_decision(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        """Convenience alias for :meth:`record`."""
        self.record(decision_type, inputs, output)

    def record(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        """Accumulate one decision into the fingerprint state."""
        inputs_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        output_hash = hashlib.sha256(json.dumps(output, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        step = f"{decision_type}:{inputs_hash}:{output_hash}".encode()
        self._state = hashlib.sha256(self._state + step).digest()

    def compute(self) -> str:
        """Return the current fingerprint as a 64-character hex string."""
        return hashlib.sha256(self._state).hexdigest()

    def finalize(self) -> str:
        """Convenience alias for :meth:`compute`."""
        return self.compute()

    @classmethod
    def from_wal(cls, reader: WALReader) -> ExecutionFingerprint:
        """Build a fingerprint from all entries in *reader*.

        Args:
            reader: A :class:`WALReader` positioned at the start of a WAL.

        Returns:
            An :class:`ExecutionFingerprint` reflecting all decisions in the WAL.
        """
        fp = cls()
        for entry in reader.iter_entries():
            fp.record(entry.decision_type, entry.inputs, entry.output)
        return fp
