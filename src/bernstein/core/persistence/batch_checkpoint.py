"""Per-item checkpoint ledger for crash-safe batch processing (#5126 slice 1).

Each batch item is identified by an opaque ``entity_id`` string.  When an item
is processed successfully the caller writes one entry via
:meth:`BatchCheckpointLedger.record_success`.  On a crash or restart a new
:class:`BatchCheckpointLedger` over the same file replays the entries to
rebuild the index, and :meth:`BatchCheckpointLedger.is_done` returns ``True``
for every entity that already succeeded — so the caller skips it without
performing the side effect again.

The ledger is an append-only JSONL file.  Each line is a JSON object::

    {"seq": 0, "entity_id": "...", "succeeded_at": "...", "prev_hash": "...", "entry_hash": "..."}

``prev_hash`` chains entries together so tampering with or removing an entry
breaks the chain.  ``seq`` is a monotonically increasing counter (0-based) that
lets :meth:`BatchCheckpointLedger.verify` detect end-truncation and reordering
in addition to hash-chain breaks.  The initial entry's ``prev_hash`` is the
zero-hash sentinel (64 hex zeros).

Single-writer design: one :class:`BatchCheckpointLedger` instance per file.
Multiple instances over the same file, or concurrent processes writing to it,
are not supported and will corrupt the chain.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

#: Sentinel zero hash (64 hex zeros) — the genesis ``prev_hash``.
GENESIS_HASH: str = "0" * 64


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_hash(seq: int, prev_hash: str, entity_id: str, succeeded_at: str) -> str:
    """Derive the chain hash for one ledger entry."""
    payload = f"{seq}\x00{prev_hash}\x00{entity_id}\x00{succeeded_at}".encode()
    return _sha256_hex(payload)


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    """One ledger row, reconstructed from a JSONL line."""

    seq: int
    entity_id: str
    succeeded_at: str
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "entity_id": self.entity_id,
            "succeeded_at": self.succeeded_at,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class BatchCheckpointLedger:
    """Append-only per-item checkpoint ledger for batch processing (#5126).

    Args:
        path: Path to the ``.jsonl`` ledger file.  Created on first write if
            it does not exist.

    Single-writer: create one instance per file and reuse it.  Multiple
    instances or concurrent processes writing to the same file are not
    supported.

    Usage::

        from datetime import UTC, datetime
        ledger = BatchCheckpointLedger(Path(".sdd/batch.jsonl"))
        for entity_id in items:
            if ledger.is_done(entity_id):
                continue          # already succeeded on a previous run
            process(entity_id)    # side-effecting step
            ledger.record_success(entity_id, datetime.now(tz=UTC).isoformat())
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # entity_id → succeeded_at for every entry in the file
        self._done: dict[str, str] = {}
        self._last_hash: str = GENESIS_HASH
        self._seq: int = 0
        self._load()

    # ------------------------------------------------------------------ public

    @property
    def path(self) -> Path:
        """Path to the underlying JSONL ledger file."""
        return self._path

    def is_done(self, entity_id: str) -> bool:
        """Return ``True`` if *entity_id* has a success entry in the ledger."""
        with self._lock:
            return entity_id in self._done

    def record_success(self, entity_id: str, succeeded_at: str) -> None:
        """Append a success entry for *entity_id* and update the in-memory index.

        Args:
            entity_id: Opaque identifier for the batch item (e.g. a resource
                path, a user id, or a secret name).
            succeeded_at: ISO 8601 timestamp of the successful processing.
        """
        with self._lock:
            eh = _entry_hash(self._seq, self._last_hash, entity_id, succeeded_at)
            entry = CheckpointEntry(
                seq=self._seq,
                entity_id=entity_id,
                succeeded_at=succeeded_at,
                prev_hash=self._last_hash,
                entry_hash=eh,
            )
            line = json.dumps(entry.to_dict(), separators=(",", ":"), ensure_ascii=False)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._done[entity_id] = succeeded_at
            self._last_hash = eh
            self._seq += 1

    def verify(self) -> list[str]:
        """Replay the ledger file and return chain-integrity errors.

        An empty list means the file is intact.  Errors name the 1-based line
        number and the problem (hash mismatch, missing field, or non-monotonic seq).
        """
        errors: list[str] = []
        if not self._path.exists():
            return errors
        prev = GENESIS_HASH
        expected_seq = 0
        for lineno, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: JSON decode error: {exc}")
                continue
            for key in ("seq", "entity_id", "succeeded_at", "prev_hash", "entry_hash"):
                if key not in row:
                    errors.append(f"line {lineno}: missing field {key!r}")
                    break
            else:
                if row["seq"] != expected_seq:
                    errors.append(f"line {lineno}: seq mismatch: expected {expected_seq}, got {row['seq']!r}")
                expected = _entry_hash(row["seq"], row["prev_hash"], row["entity_id"], row["succeeded_at"])
                if row["entry_hash"] != expected:
                    errors.append(f"line {lineno}: entry_hash mismatch for entity_id={row['entity_id']!r}")
                if row["prev_hash"] != prev:
                    errors.append(f"line {lineno}: prev_hash mismatch for entity_id={row['entity_id']!r}")
                prev = row["entry_hash"]
                expected_seq += 1
        return errors

    @property
    def done_count(self) -> int:
        """Number of distinct entity ids that have a success entry."""
        with self._lock:
            return len(self._done)

    # ------------------------------------------------------------------ private

    def _load(self) -> None:
        """Read existing entries from disk into the in-memory index.

        Recomputes each entry's hash rather than trusting the stored value, so a
        tampered file cannot inject a false ``_last_hash`` that would allow
        appending undetected entries.
        """
        if not self._path.exists():
            return
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
                if not all(k in row for k in ("seq", "entity_id", "succeeded_at", "prev_hash", "entry_hash")):
                    continue
                computed = _entry_hash(row["seq"], self._last_hash, row["entity_id"], row["succeeded_at"])
                if computed != row["entry_hash"] or row["prev_hash"] != self._last_hash:
                    continue
                self._done[row["entity_id"]] = row["succeeded_at"]
                self._last_hash = computed
                self._seq = row["seq"] + 1
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
